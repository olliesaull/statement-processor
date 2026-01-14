"""
This module is the "orchestrator" for statement processing once Textract has
finished running.

At a high level, `run_textraction` takes:
- A Textract `job_id` (from StepFunctions / Textract StartDocumentAnalysis)
- The S3 location of the source PDF (`bucket` + `pdf_key`)
- Where to write the resulting JSON (`json_key`)
- Tenant/contact identifiers used to load mapping/config
- A `statement_id` used as the parent record id in DynamoDB

And it produces:
- A structured JSON representation of the statement (line items + metadata), uploaded to S3 at `json_key`
- Per-line-item rows in DynamoDB (under the statement's tenant partition)
- Some best-effort validation and anomaly flagging metadata

## Pipeline overview

1) Fetch Textract TABLE blocks and reconstruct them as simple 2D grids (`core/extraction.get_tables_for_job`).
2) Map those grids into structured statement items using contact-specific config (`core/transform.table_to_json`, which reads config via `core/get_contact_config.py`).
3) Persist the extracted line items into DynamoDB (this module: `_persist_statement_items`).
4) Optionally validate extracted references against the source PDF text (`core/validation/validate_item_count.py`).
5) Flag suspicious rows (e.g. "balance brought forward") without removing them (`core/validation/anomaly_detection.py`).
6) Upload the final JSON to S3 and record the Textract `job_id` on the statement header.

This module deliberately treats some steps as "best effort" (e.g. validation and item persistence are wrapped in try/except)
so that JSON generation/upload can still succeed even if auxiliary steps fail.
"""

import io
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from config import S3_BUCKET_NAME, logger, s3_client, tenant_statements_table
from core.extraction import TableOnPage, get_tables_for_job
from core.transform import table_to_json
from core.validation.anomaly_detection import apply_outlier_flags
from core.validation.validate_item_count import validate_references_roundtrip


def _sanitize_for_dynamodb(value: Any) -> Any:  # pylint: disable=too-many-return-statements
    """
    Convert extracted values into DynamoDB-friendly types.

    DynamoDB represents numbers as `Decimal` (boto3 discourages `float` due to precision issues). This helper:
    - Drops empty strings (treats them as missing)
    - Converts numeric-looking strings (after removing commas) to `Decimal`
    - Converts floats to `Decimal` via `str(...)`
    - Recurses into lists/dicts so nested payloads are also sanitized

    Anything that cannot be interpreted as numeric is left as its original string.
    """
    # Coerce incoming values into DynamoDB-friendly types and drop empty strings
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
        sanitized_dict: Dict[str, Any] = {}
        for k, v in value.items():
            sanitized = _sanitize_for_dynamodb(v)
            if sanitized is not None:
                sanitized_dict[k] = sanitized
        return sanitized_dict
    return value


def _persist_statement_items(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements
    tenant_id: str,
    contact_id: Optional[str],
    statement_id: Optional[str],
    items: List[Dict[str, Any]],
    *,
    earliest_item_date: Optional[str] = None,
    latest_item_date: Optional[str] = None,
) -> None:
    """
    Persist extracted statement line items into the tenant statements DynamoDB table.

    The DynamoDB schema used by this Lambda stores:
    - A "header" row at sort key `StatementID == statement_id`
    - One row per extracted line item at sort key `StatementID == f"{statement_id}#item-0001"`, etc.

    Each run replaces the set of item rows for the statement (delete + reinsert).
    Before deletion, we fetch existing item rows to preserve their "Completed"
    status across re-processing (so user workflow state isn't lost when items are regenerated).

    This function also updates statement-level metadata fields (earliest/latest item dates) on the header record.
    """
    # Persist the per-item rows for a statement; replace any prior rows for this statement (from previous textractions).
    if not statement_id:
        return

    # Discover existing item rows so we can delete-and-replace them while preserving completion state.
    keys_to_delete: List[str] = []
    existing_status: Dict[str, bool] = {}
    query_kwargs: Dict[str, Any] = {
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
        # Read the statement header to use its completion flag as a default for new rows.
        header_resp = tenant_statements_table.get_item(Key={"TenantID": tenant_id, "StatementID": statement_id})
        header_item = header_resp.get("Item") if isinstance(header_resp, dict) else None
        header_completed = str(header_item.get("Completed", "false")).strip().lower() == "true" if header_item else False
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Failed to fetch statement header completion flag",
            tenant_id=tenant_id,
            statement_id=statement_id,
            error=str(exc),
            exc_info=True,
        )
        header_completed = False

    if keys_to_delete:
        # Delete existing item rows first to avoid duplicates and keep the table in sync with the latest extraction.
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

            # Sanitize values so they can be stored safely in DynamoDB (numbers as Decimal, etc.).
            sanitized_payload = {key: _sanitize_for_dynamodb(value) for key, value in item.items() if value is not None}
            sanitized_payload["statement_item_id"] = item_id

            record: Dict[str, Any] = {
                "TenantID": tenant_id,
                "StatementID": item_id,
                "StatementItemID": item_id,
                "ParentStatementID": statement_id,
                "RecordType": "statement_item",
                # Preserve per-item completion flags if they already exist; otherwise inherit from header.
                "Completed": "true" if existing_status.get(item_id, header_completed) else "false",
            }
            if contact_id:
                record["ContactID"] = contact_id

            record.update(sanitized_payload)
            batch.put_item(Item=record)

    if statement_id and (earliest_item_date or latest_item_date):
        # Store derived date range metadata on the statement header for filtering/summaries elsewhere.
        update_parts: List[str] = []
        attr_names: Dict[str, str] = {}
        attr_values: Dict[str, Any] = {}

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
                Key={"TenantID": tenant_id, "StatementID": statement_id},
                UpdateExpression="SET " + ", ".join(update_parts),
                ExpressionAttributeNames=attr_names,
                ExpressionAttributeValues=attr_values,
            )


def run_textraction(
    job_id: str,
    bucket: str,
    pdf_key: str,
    json_key: str,
    tenant_id: str,
    contact_id: str,
    statement_id: str,
) -> Dict[str, Any]:  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    """
    End-to-end processing for a single statement Textract job.

    This function is called by the Lambda handler after StepFunctions determines that Textract has finished successfully. It:
    - Fetches Textract tables for the job and rebuilds them into `TableOnPage` grids
    - Maps those grids into structured statement JSON (line items + metadata)
    - Persists item rows into DynamoDB (best effort)
    - Validates extracted references against the source PDF (best effort)
    - Flags anomalous rows, uploads JSON to S3, and records the job id on the header
    """
    # Fetch Textract tables for the job and rebuild them into 2D grids (see `core/extraction.py`).
    tables_by_key: Dict[str, List[TableOnPage]] = get_tables_for_job(job_id)

    # `get_tables_for_job` returns a dict keyed by job id; we currently only ever fetch one job.
    tables_wp = next(iter(tables_by_key.values())) if tables_by_key else []

    key = pdf_key  # The PDF S3 object key; passed through for provenance (not currently used by `table_to_json` itself).

    logger.info("Textract statement processing", job_id=job_id, key=key)
    # Convert grids into a structured statement using contact-specific mapping/config (see `core/transform.py`).
    statement = table_to_json(tables_wp, tenant_id, contact_id, statement_id=statement_id)
    item_count = len(statement.get("statement_items", []) or [])
    logger.info(
        "Built statement JSON",
        job_id=job_id,
        statement_id=statement_id,
        items=item_count,
    )

    try:
        # Persist items to DynamoDB (delete + rewrite); failures here should not prevent JSON output.
        _persist_statement_items(
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            items=statement.get("statement_items", []) or [],
            earliest_item_date=statement.get("earliest_item_date"),
            latest_item_date=statement.get("latest_item_date"),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception(
            "Failed to persist statement items",
            statement_id=statement_id,
            tenant_id=tenant_id,
            contact_id=contact_id,
            error=str(exc),
        )

    try:
        # Best-effort validation: compare extracted reference strings with text found in the PDF (via pdfplumber).
        obj = s3_client.get_object(Bucket=bucket or S3_BUCKET_NAME, Key=key)
        pdf_bytes = obj["Body"].read()
        statement_items = statement.get("statement_items", []) or []
        validate_references_roundtrip(pdf_bytes, statement_items)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Reference validation skipped",
            key=key,
            tenant_id=tenant_id,
            statement_id=statement_id,
            error=str(exc),
            exc_info=True,
        )

    # Flag outliers without removing them, so downstream consumers can inspect anomalies
    statement, summary = apply_outlier_flags(statement, remove=False, one_based_index=True)
    logger.info("Performed anomaly detection", summary=json.dumps(summary, indent=2))

    # Upload the enriched JSON back to S3 for the caller to consume
    # StepFunctions passes `jsonKey` into this Lambda; writing to that key is how we publish the output artifact.
    buf = io.BytesIO(json.dumps(statement, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)

    s3_client.put_object(Bucket=bucket or S3_BUCKET_NAME, Key=json_key, Body=buf.getvalue())
    logger.info("Uploaded statement JSON", bucket=bucket, json_key=json_key)

    if tenant_statements_table is not None:
        try:
            # Persist the Textract JobId alongside the statement header for traceability
            tenant_statements_table.update_item(
                Key={"TenantID": tenant_id, "StatementID": statement_id},
                UpdateExpression="SET JobId = :jobId",
                ExpressionAttributeValues={":jobId": job_id},
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Failed to store job id on statement",
                statement_id=statement_id,
                tenant_id=tenant_id,
                error=str(exc),
                exc_info=True,
            )

    # Convenience for callers: derive a friendly filename from the PDF key.
    filename = f"{Path(key).stem}.json"
    return {"filename": filename, "statement": statement}
