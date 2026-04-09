"""Statement processing orchestrator.

Coordinates the extraction pipeline after Step Functions invokes the Lambda:
1. Read PDF from S3
2. Call extract_statement() (Bedrock extraction boundary)
3. Map ExtractionResult → SupplierStatement
4. Persist items to DynamoDB
5. Validate references against PDF text (best effort)
6. Flag anomalies (best effort)
7. Upload JSON to S3
8. Record Bedrock request IDs on statement header
"""

import io
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Key

from config import S3_BUCKET_NAME, s3_client, tenant_statements_table
from core.date_utils import parse_with_format
from core.extraction import extract_statement
from core.models import ExtractionResult, StatementItem, SupplierStatement
from core.processing_progress import update_processing_stage
from core.validation.anomaly_detection import apply_outlier_flags
from logger import logger


def _derive_date_range(items: list[StatementItem], date_format: str) -> tuple[str | None, str | None]:
    """Compute earliest and latest item dates from parsed dates.

    Parses each item's date string using the detected format template,
    then returns (earliest_iso, latest_iso). Returns (None, None) if
    no dates can be parsed.
    """
    parsed_dates = []
    for item in items:
        if not item.date:
            continue
        dt = parse_with_format(str(item.date), date_format)
        if dt:
            parsed_dates.append(dt)

    if not parsed_dates:
        return None, None

    parsed_dates.sort()
    return parsed_dates[0].strftime("%Y-%m-%d"), parsed_dates[-1].strftime("%Y-%m-%d")


def _map_extraction_to_statement(extraction: ExtractionResult, statement_id: str) -> SupplierStatement:
    """Map ExtractionResult to SupplierStatement (self-describing JSON).

    Assigns statement_item_ids, computes date range, copies metadata.
    """
    # Assign stable item IDs.
    for i, item in enumerate(extraction.items):
        item.statement_item_id = f"{statement_id}#item-{i + 1:04d}"

    earliest, latest = _derive_date_range(extraction.items, extraction.date_format)

    return SupplierStatement(
        statement_items=extraction.items,
        earliest_item_date=earliest,
        latest_item_date=latest,
        date_format=extraction.date_format,
        date_confidence=extraction.date_confidence,
        detected_headers=extraction.detected_headers,
        header_mapping=extraction.header_mapping,
        input_tokens=extraction.input_tokens,
        output_tokens=extraction.output_tokens,
    )


def _sanitize_for_dynamodb(value: Any) -> Any:  # pylint: disable=too-many-return-statements
    """Convert extracted values into DynamoDB-friendly types.

    DynamoDB represents numbers as Decimal. This helper:
    - Drops empty strings (treats as missing)
    - Converts numeric-looking strings to Decimal
    - Converts floats to Decimal via str(...)
    - Recurses into lists/dicts
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        try:
            normalized = stripped.replace(",", "")
            return Decimal(normalized)
        except InvalidOperation:
            return stripped
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        sanitized_list = []
        for item in value:
            sanitized = _sanitize_for_dynamodb(item)
            if sanitized is not None:
                sanitized_list.append(sanitized)
        return sanitized_list
    if isinstance(value, dict):
        sanitized_dict: dict[str, Any] = {}
        for k, v in value.items():
            sanitized = _sanitize_for_dynamodb(v)
            if sanitized is not None:
                sanitized_dict[k] = sanitized
        return sanitized_dict
    return value


def _persist_statement_items(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements,too-many-positional-arguments
    tenant_id: str, contact_id: str | None, statement_id: str | None, items: list[dict[str, Any]], *, earliest_item_date: str | None = None, latest_item_date: str | None = None
) -> None:
    """Persist extracted statement line items into DynamoDB.

    Replaces existing item rows (delete + reinsert). Preserves
    per-item completion status across re-processing.
    """
    if not statement_id:
        return

    # Fetch existing item rows to preserve completion state.
    keys_to_delete: list[str] = []
    existing_status: dict[str, bool] = {}
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(f"{statement_id}#item-"),
        "ProjectionExpression": "#sid, #completed",
        "ExpressionAttributeNames": {"#sid": "StatementID", "#completed": "Completed"},
    }

    while True:
        resp = tenant_statements_table.query(**query_kwargs)
        for it in resp.get("Items", []):
            if not isinstance(it, dict):
                continue
            sid = it.get("StatementID")
            if not isinstance(sid, str) or not sid:
                continue
            keys_to_delete.append(sid)
            completed_val = str(it.get("Completed", "false")).strip().lower()
            existing_status[sid] = completed_val == "true"
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        query_kwargs["ExclusiveStartKey"] = lek

    try:
        header_resp = tenant_statements_table.get_item(Key={"TenantID": tenant_id, "StatementID": statement_id})
        header_item = header_resp.get("Item") if isinstance(header_resp, dict) else None
        header_completed = str(header_item.get("Completed", "false")).strip().lower() == "true" if header_item else False
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to fetch statement header completion flag", tenant_id=tenant_id, statement_id=statement_id, error=str(exc), exc_info=True)
        header_completed = False

    if keys_to_delete:
        with tenant_statements_table.batch_writer() as batch:
            for sort_key in keys_to_delete:
                batch.delete_item(Key={"TenantID": tenant_id, "StatementID": sort_key})

    if not items:
        return

    with tenant_statements_table.batch_writer() as batch:
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("statement_item_id")
            if not item_id:
                continue

            sanitized_payload = {key: _sanitize_for_dynamodb(value) for key, value in item.items() if value is not None}
            sanitized_payload["statement_item_id"] = item_id

            record: dict[str, Any] = {
                "TenantID": tenant_id,
                "StatementID": item_id,
                "StatementItemID": item_id,
                "ParentStatementID": statement_id,
                "RecordType": "statement_item",
                "Completed": "true" if existing_status.get(item_id, header_completed) else "false",
            }
            if contact_id:
                record["ContactID"] = contact_id

            record.update(sanitized_payload)
            batch.put_item(Item=record)

    if statement_id and (earliest_item_date or latest_item_date):
        update_parts: list[str] = []
        attr_names: dict[str, str] = {}
        attr_values: dict[str, Any] = {}

        if earliest_item_date:
            attr_names["#earliestItemDate"] = "EarliestItemDate"
            attr_values[":earliestItemDate"] = earliest_item_date
            update_parts.append("#earliestItemDate = :earliestItemDate")
        if latest_item_date:
            attr_names["#latestItemDate"] = "LatestItemDate"
            attr_values[":latestItemDate"] = latest_item_date
            update_parts.append("#latestItemDate = :latestItemDate")

        if update_parts:
            tenant_statements_table.update_item(
                Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression="SET " + ", ".join(update_parts), ExpressionAttributeNames=attr_names, ExpressionAttributeValues=attr_values
            )


def run_extraction(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    bucket: str, pdf_key: str, json_key: str, tenant_id: str, contact_id: str, statement_id: str, page_count: int
) -> dict[str, Any]:
    """End-to-end processing for a single statement.

    Called by the Lambda handler. Reads the PDF from S3, runs extraction
    via Bedrock, persists results, validates, and uploads JSON.
    """
    # Read PDF from S3.
    obj = s3_client.get_object(Bucket=bucket or S3_BUCKET_NAME, Key=pdf_key)
    pdf_bytes = obj["Body"].read()

    # -- Progress: chunking --
    update_processing_stage(tenant_id, statement_id, "chunking")

    logger.info("Starting extraction", tenant_id=tenant_id, statement_id=statement_id, page_count=page_count)

    # -- Progress: extracting (callback fires after chunking and each chunk) --
    def _on_chunk_complete(completed: int, total: int) -> None:
        """Progress callback from extract_statement.

        Called with completed=0 after chunking (extraction starting),
        then once per completed chunk thereafter.
        """
        if total <= 1:
            # Single-chunk PDF: set stage without progress info.
            # Stages still transition but there is only one Bedrock call
            # so chunk-level progress is meaningless.
            update_processing_stage(tenant_id, statement_id, "extracting")
        else:
            # Multi-chunk PDF: set stage with progress and total_sections.
            update_processing_stage(tenant_id, statement_id, "extracting", progress=f"{completed}/{total}", total_sections=total)

    # Run extraction.
    extraction_result = extract_statement(pdf_bytes, page_count, on_chunk_complete=_on_chunk_complete)

    logger.info(
        "Extraction complete",
        tenant_id=tenant_id,
        statement_id=statement_id,
        item_count=len(extraction_result.items),
        header_mapping=extraction_result.header_mapping,
        date_format=extraction_result.date_format,
        date_confidence=extraction_result.date_confidence,
        input_tokens=extraction_result.input_tokens,
        output_tokens=extraction_result.output_tokens,
    )

    # Map to SupplierStatement (self-describing JSON).
    supplier_statement = _map_extraction_to_statement(extraction_result, statement_id)
    statement_dict = supplier_statement.model_dump()

    logger.info("SupplierStatement built", statement_id=statement_id, date_range=f"{supplier_statement.earliest_item_date} to {supplier_statement.latest_item_date}")

    # -- Progress: post-processing --
    update_processing_stage(tenant_id, statement_id, "post_processing")

    # Flag outliers without removing them.
    statement_dict, summary = apply_outlier_flags(statement_dict, remove=False)
    logger.info("Anomaly detection complete", summary=json.dumps(summary, indent=2))

    # Persist items to DynamoDB (best effort).
    try:
        _persist_statement_items(
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            items=statement_dict.get("statement_items", []) or [],
            earliest_item_date=statement_dict.get("earliest_item_date"),
            latest_item_date=statement_dict.get("latest_item_date"),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("Failed to persist statement items", statement_id=statement_id, tenant_id=tenant_id, error=str(exc))

    # Upload JSON to S3.
    buf = io.BytesIO(json.dumps(statement_dict, ensure_ascii=False, indent=2).encode("utf-8"))
    s3_client.put_object(Bucket=bucket or S3_BUCKET_NAME, Key=json_key, Body=buf.getvalue())
    logger.info("Uploaded statement JSON", bucket=bucket, json_key=json_key)

    # Record Bedrock request IDs on statement header for traceability.
    if tenant_statements_table is not None:
        try:
            tenant_statements_table.update_item(
                Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression="SET BedrockRequestIds = :ids", ExpressionAttributeValues={":ids": extraction_result.request_ids}
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to store Bedrock request IDs on statement", statement_id=statement_id, error=str(exc), exc_info=True)

    # -- Progress: complete --
    update_processing_stage(tenant_id, statement_id, "complete")

    filename = f"{Path(pdf_key).stem}.json"
    return {"filename": filename, "statement": statement_dict}
