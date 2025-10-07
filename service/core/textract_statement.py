import io
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from werkzeug.datastructures import FileStorage

from boto3.dynamodb.conditions import Key

from config import logger, s3_client, tenant_statements_table
from core.extraction import TableOnPage, get_tables
from core.transform import table_to_json
from core.validation.validate_item_count import validate_references_roundtrip


def _sanitize_for_dynamodb(value: Any) -> Any:
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


def _persist_statement_items(
    tenant_id: str,
    contact_id: Optional[str],
    statement_id: Optional[str],
    items: List[Dict[str, Any]],
) -> None:
    if not statement_id:
        return

    keys_to_delete: List[str] = []
    existing_status: Dict[str, bool] = {}
    query_kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(
            f"{statement_id}#item-"
        ),
        "ProjectionExpression": "#sid, #completed",
        "ExpressionAttributeNames": {"#sid": "StatementID", "#completed": "Completed"},
    }

    while True:
        resp = tenant_statements_table.query(**query_kwargs)
        for it in resp.get("Items", []):
            if not isinstance(it, dict):
                continue
            sid = it.get("StatementID")
            if not sid:
                continue
            keys_to_delete.append(sid)
            completed_val = str(it.get("Completed", "false")).strip().lower()
            existing_status[sid] = completed_val == "true"
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        query_kwargs["ExclusiveStartKey"] = lek

    try:
        header_resp = tenant_statements_table.get_item(
            Key={"TenantID": tenant_id, "StatementID": statement_id}
        )
        header_item = header_resp.get("Item") if isinstance(header_resp, dict) else None
        header_completed = (
            str(header_item.get("Completed", "false")).strip().lower() == "true"
            if header_item
            else False
        )
    except Exception as exc:
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

            sanitized_payload = {
                key: _sanitize_for_dynamodb(value)
                for key, value in item.items()
                if value is not None
            }
            sanitized_payload["statement_item_id"] = item_id

            record: Dict[str, Any] = {
                "TenantID": tenant_id,
                "StatementID": item_id,
                "StatementItemID": item_id,
                "ParentStatementID": statement_id,
                "RecordType": "statement_item",
                "Completed": "true"
                if existing_status.get(item_id, header_completed)
                else "false",
            }
            if contact_id:
                record["ContactID"] = contact_id

            record.update(sanitized_payload)
            batch.put_item(Item=record)


def run_textraction(bucket: str, pdf_key: str, tenant_id: str, contact_id: str) -> FileStorage:
    """Run Textract, transform to canonical JSON, validate, and return as FileStorage."""
    statement_id = Path(pdf_key).stem
    tables_by_key: Dict[str, List[TableOnPage]] = get_tables(bucket, pdf_key)

    # get_tables returns a mapping with the input key; handle robustly
    if tables_by_key:
        key = next(iter(tables_by_key.keys()))
        tables_wp = tables_by_key[key]
    else:
        key = pdf_key
        tables_wp = []

    logger.info("Textract statement processing", key=key)
    statement = table_to_json(key, tables_wp, tenant_id, contact_id, statement_id=statement_id)

    try:
        _persist_statement_items(
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            items=statement.get("statement_items", []) or [],
        )
    except Exception as exc:
        logger.exception("Failed to persist statement items", statement_id=statement_id, tenant_id=tenant_id, contact_id=contact_id, error=str(exc))

    # Fetch PDF bytes from S3 and validate against extracted JSON
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        pdf_bytes = obj["Body"].read()
        statement_items = statement.get("statement_items", []) or []
        validate_references_roundtrip(pdf_bytes, statement_items)
    except Exception as exc:
        logger.warning("Reference validation skipped", key=key, tenant_id=tenant_id, statement_id=statement_id, error=str(exc), exc_info=True)

    # optional: ML outlier pass (kept commented; requires sklearn and data volume)
    from core.validation.anomaly_detection import apply_outlier_flags
    statement, summary = apply_outlier_flags(statement, remove=False, one_based_index=True, threshold_method="iqr")

    logger.info("Performed anomaly detection", summary=json.dumps(summary, indent=2))

    # Serialize to bytes in memory for upload/response
    buf = io.BytesIO(json.dumps(statement, ensure_ascii=False, indent=2).encode("utf-8"))
    buf.seek(0)

    filename = f"{Path(key).stem}.json"
    return FileStorage(stream=buf, filename=filename, content_type="application/json")
