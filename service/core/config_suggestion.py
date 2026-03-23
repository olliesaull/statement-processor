"""Orchestrator for auto-suggesting contact config from statement PDFs.

Runs Textract sync API on page 1, sends extracted headers and rows to
Bedrock Haiku 4.5, applies date disambiguation, saves the suggestion
to S3, and updates statement status in DynamoDB.
"""

import json
from io import BytesIO

from pypdf import PdfReader, PdfWriter

from config import S3_BUCKET_NAME, s3_client, tenant_statements_table, textract_client
from core.bedrock_client import suggest_column_mapping
from core.date_disambiguation import disambiguate_date_format
from core.models import ConfigSuggestion
from logger import logger


def suggest_config_for_statement(tenant_id: str, contact_id: str, contact_name: str, statement_id: str, pdf_s3_key: str, filename: str, page_count: int = 0) -> None:
    """Run the full config suggestion pipeline for a single statement.

    This is the entry point called from the ThreadPoolExecutor in the
    upload handler. It must not raise — failures are captured as status
    updates in DynamoDB.
    """
    try:
        # 1. Textract sync on page 1
        headers, rows, date_values = _extract_page_one(pdf_s3_key)

        if not headers:
            logger.warning("No table headers found on page 1", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)
            _set_statement_status(tenant_id, statement_id, "config_suggestion_failed")
            return

        # 2. LLM suggestion
        suggested_config, confidence_notes = suggest_column_mapping(headers, rows)

        # 3. Date disambiguation — confirm or reject the LLM's date format.
        date_format = suggested_config.get("date_format", "")
        if date_format:
            confirmed = disambiguate_date_format(date_values, date_format)
            suggested_config["date_format"] = confirmed

        # 4. Save suggestion to S3
        suggestion = ConfigSuggestion(
            contact_id=contact_id,
            contact_name=contact_name,
            statement_id=statement_id,
            filename=filename,
            page_count=page_count,
            suggested_config=suggested_config,
            detected_headers=headers,
            confidence_notes=confidence_notes,
        )
        suggestion_key = f"{tenant_id}/config-suggestions/{statement_id}.json"
        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=suggestion_key, Body=suggestion.model_dump_json(), ContentType="application/json")

        # 5. Update statement status
        _set_statement_status(tenant_id, statement_id, "pending_config_review")

        logger.info(
            "Config suggestion saved",
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            detected_headers=headers,
            suggested_number=suggested_config.get("number"),
            suggested_date_format=suggested_config.get("date_format"),
        )

    except Exception:
        logger.exception("Config suggestion failed", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)
        _set_statement_status(tenant_id, statement_id, "config_suggestion_failed")


def _extract_page_one(pdf_s3_key: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Download PDF from S3, extract page 1, run Textract sync on it.

    The Textract sync AnalyzeDocument API rejects multi-page PDFs passed
    via S3Object reference with UnsupportedDocumentException. Extracting
    page 1 as bytes avoids this limitation.

    Returns:
        Tuple of (headers, data_rows, date_column_values).
    """
    # Download the full PDF from S3.
    pdf_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=pdf_s3_key)
    pdf_bytes = pdf_obj["Body"].read()

    # Extract just page 1 as a single-page PDF.
    reader = PdfReader(BytesIO(pdf_bytes))
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    page_one_buf = BytesIO()
    writer.write(page_one_buf)
    page_one_bytes = page_one_buf.getvalue()

    # Call Textract sync API with raw bytes.
    response = textract_client.analyze_document(Document={"Bytes": page_one_bytes}, FeatureTypes=["TABLES"])

    return _parse_textract_table(response)


def _parse_textract_table(response: dict) -> tuple[list[str], list[list[str]], list[str]]:
    """Parse Textract AnalyzeDocument response to extract table data.

    Returns:
        Tuple of (headers, data_rows, date_column_values).
        Headers are row 1 cells, data rows are subsequent rows.
        date_column_values are the values from the column identified as
        having date-like content (first column with "/" or "-" in values).
    """
    cells = [b for b in response.get("Blocks", []) if b.get("BlockType") == "CELL"]

    if not cells:
        return [], [], []

    # Group cells by row index.
    rows_dict: dict[int, dict[int, str]] = {}
    for cell in cells:
        row_idx = cell.get("RowIndex", 0)
        col_idx = cell.get("ColumnIndex", 0)
        text = cell.get("Text", "").strip()
        rows_dict.setdefault(row_idx, {})[col_idx] = text

    sorted_row_indices = sorted(rows_dict.keys())
    if not sorted_row_indices:
        return [], [], []

    # Row 1 = headers.
    header_row = rows_dict[sorted_row_indices[0]]
    max_col = max(header_row.keys()) if header_row else 0
    headers = [header_row.get(c, "") for c in range(1, max_col + 1)]

    # Remaining rows = data.
    data_rows: list[list[str]] = []
    for row_idx in sorted_row_indices[1:]:
        row_cells = rows_dict[row_idx]
        row = [row_cells.get(c, "") for c in range(1, max_col + 1)]
        data_rows.append(row)

    # Identify date column — first column whose values contain "/" or "-".
    date_col_idx = _find_date_column(headers, data_rows)
    date_values: list[str] = []
    if date_col_idx is not None:
        date_values = [row[date_col_idx] for row in data_rows if date_col_idx < len(row) and row[date_col_idx]]

    return headers, data_rows, date_values


def _find_date_column(headers: list[str], rows: list[list[str]]) -> int | None:
    """Find the column index most likely to contain dates.

    Looks for columns whose header contains 'date' (case-insensitive)
    or whose values contain date separators.
    """
    # Prefer columns with "date" in the header name.
    for idx, header in enumerate(headers):
        if "date" in header.lower():
            return idx

    # Fall back to first column with "/" or "-" separators in values.
    for col_idx in range(len(headers)):
        for row in rows:
            if col_idx < len(row):
                val = row[col_idx]
                if "/" in val or ("-" in val and any(c.isdigit() for c in val)):
                    return col_idx

    return None


def _set_statement_status(tenant_id: str, statement_id: str, status: str) -> None:
    """Update the Status field on a statement header row in DynamoDB."""
    tenant_statements_table.update_item(
        Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "Status"}, ExpressionAttributeValues={":s": status}
    )


def get_pending_suggestions(tenant_id: str) -> list[ConfigSuggestion]:
    """List all pending config suggestions for a tenant from S3.

    Loads each suggestion JSON file under the config-suggestions/ prefix.
    """
    prefix = f"{tenant_id}/config-suggestions/"
    suggestions: list[ConfigSuggestion] = []

    try:
        response = s3_client.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=prefix)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            body = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)["Body"].read()
            suggestion = ConfigSuggestion.model_validate_json(body)
            suggestions.append(suggestion)
    except Exception:
        logger.exception("Failed to load pending suggestions", tenant_id=tenant_id)

    return suggestions


def get_pending_suggestion_count(tenant_id: str) -> int:
    """Count pending config suggestion files without loading them.

    Uses S3 list_objects_v2 to count objects under the prefix.
    """
    prefix = f"{tenant_id}/config-suggestions/"
    try:
        response = s3_client.list_objects_v2(Bucket=S3_BUCKET_NAME, Prefix=prefix)
        return sum(1 for obj in response.get("Contents", []) if obj["Key"].endswith(".json"))
    except Exception:
        logger.exception("Failed to count pending suggestions", tenant_id=tenant_id)
        return 0


def get_suggestion(tenant_id: str, statement_id: str) -> ConfigSuggestion | None:
    """Load a single config suggestion from S3."""
    key = f"{tenant_id}/config-suggestions/{statement_id}.json"
    try:
        body = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=key)["Body"].read()
        return ConfigSuggestion.model_validate_json(body)
    except Exception:
        logger.warning("Config suggestion not found", tenant_id=tenant_id, statement_id=statement_id)
        return None


def delete_suggestion(tenant_id: str, statement_id: str) -> None:
    """Delete a config suggestion file from S3."""
    key = f"{tenant_id}/config-suggestions/{statement_id}.json"
    try:
        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
    except Exception:
        logger.exception("Failed to delete suggestion", tenant_id=tenant_id, statement_id=statement_id)
