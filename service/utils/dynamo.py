"""DynamoDB helpers for statement records."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from flask import session

from config import S3_BUCKET_NAME, logger, s3_client, tenant_statements_table
from utils.storage import statement_json_s3_key, statement_pdf_s3_key

_DDB_UPDATE_MAX_WORKERS = max(4, min(16, (os.cpu_count() or 4)))


def _query_statements_by_completed(tenant_id: str | None, completed_value: str) -> list[dict[str, Any]]:
    """Query statements for a tenant filtered by the Completed flag via GSI."""
    if not tenant_id:
        logger.info("Skipping statement query; tenant missing", completed=completed_value)
        return []

    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "IndexName": "TenantIDCompletedIndex",
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("Completed").eq(completed_value),
        "FilterExpression": Attr("RecordType").not_exists() | Attr("RecordType").eq("statement"),
    }
    logger.info("Querying statements by completion", tenant_id=tenant_id, completed=completed_value)

    while True:
        resp = tenant_statements_table.query(**kwargs)
        batch = resp.get("Items", [])
        items.extend(batch)
        lek = resp.get("LastEvaluatedKey")
        logger.debug("Fetched statement batch", tenant_id=tenant_id, completed=completed_value, batch=len(batch), has_more=bool(lek))
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    logger.info("Collected statements by completion", tenant_id=tenant_id, completed=completed_value, count=len(items))
    return items


def get_statement_record(tenant_id: str, statement_id: str) -> dict[str, Any] | None:
    """Return the full DynamoDB record for a tenant/statement pair."""
    logger.info("Fetching statement record", tenant_id=tenant_id, statement_id=statement_id)
    response = tenant_statements_table.get_item(Key={"TenantID": tenant_id, "StatementID": statement_id})
    item = response.get("Item")
    logger.debug("Statement record fetched", tenant_id=tenant_id, statement_id=statement_id, found=bool(item))
    return item


def persist_item_types_to_dynamo(tenant_id: str | None, classification_updates: dict[str, str], *, max_workers: int | None = None) -> None:
    """Update DynamoDB item types using a thread pool to hide network latency."""
    if not tenant_id or not classification_updates:
        return

    items = list(classification_updates.items())
    worker_count = max_workers or _DDB_UPDATE_MAX_WORKERS
    worker_count = min(worker_count, len(items)) or 1

    def _update(entry: tuple[str, str]) -> None:
        statement_item_id, new_type = entry
        try:
            tenant_statements_table.update_item(
                Key={"TenantID": tenant_id, "StatementID": statement_item_id}, UpdateExpression="SET item_type = :item_type", ExpressionAttributeValues={":item_type": new_type}
            )
        except ClientError as exc:
            logger.exception("Failed to persist item type to DynamoDB", tenant_id=tenant_id, statement_id=statement_item_id, item_type=new_type, error=str(exc))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        executor.map(_update, items)


def get_incomplete_statements() -> list[dict[str, Any]]:
    """Return statements for the active tenant that are not completed."""
    tenant_id = session.get("xero_tenant_id")
    logger.info("Fetching incomplete statements", tenant_id=tenant_id)
    return _query_statements_by_completed(tenant_id, "false")


def get_completed_statements() -> list[dict[str, Any]]:
    """Return statements for the active tenant that are marked completed."""
    tenant_id = session.get("xero_tenant_id")
    logger.info("Fetching completed statements", tenant_id=tenant_id)
    return _query_statements_by_completed(tenant_id, "true")


def mark_statement_completed(tenant_id: str, statement_id: str, completed: bool) -> None:
    """Persist a completion flag on the statement record in DynamoDB."""
    tenant_statements_table.update_item(
        Key={"TenantID": tenant_id, "StatementID": statement_id},
        UpdateExpression="SET #completed = :completed",
        ExpressionAttributeNames={"#completed": "Completed"},
        ExpressionAttributeValues={":completed": "true" if completed else "false"},
        ConditionExpression=Attr("StatementID").exists(),
    )


def get_statement_item_status_map(tenant_id: str, statement_id: str) -> dict[str, bool]:
    """Return completion status for each statement item keyed by statement_item_id."""
    if not tenant_id or not statement_id:
        return {}

    logger.info("Fetching statement item statuses", tenant_id=tenant_id, statement_id=statement_id)
    statuses: dict[str, bool] = {}
    prefix = f"{statement_id}#item-"
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(prefix),
        "ProjectionExpression": "#sid, #completed",
        "ExpressionAttributeNames": {"#sid": "StatementID", "#completed": "Completed"},
    }

    while True:
        resp = tenant_statements_table.query(**kwargs)
        for item in resp.get("Items", []):
            statement_item_id = item.get("StatementID")
            if not statement_item_id:
                continue
            completed_val = str(item.get("Completed", "false")).strip().lower()
            statuses[statement_item_id] = completed_val == "true"

        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    logger.info("Fetched statement item statuses", tenant_id=tenant_id, statement_id=statement_id, count=len(statuses))
    return statuses


def set_statement_item_completed(tenant_id: str, statement_item_id: str, completed: bool) -> None:
    """Toggle completion flag for a single statement item."""
    if not tenant_id or not statement_item_id:
        return

    tenant_statements_table.update_item(
        Key={"TenantID": tenant_id, "StatementID": statement_item_id},
        UpdateExpression="SET #completed = :completed",
        ExpressionAttributeNames={"#completed": "Completed"},
        ExpressionAttributeValues={":completed": "true" if completed else "false"},
    )


def set_all_statement_items_completed(tenant_id: str, statement_id: str, completed: bool) -> None:
    """Set completion flag for all statement items tied to a statement."""
    statuses = get_statement_item_status_map(tenant_id, statement_id)
    if not statuses:
        return

    for statement_item_id in statuses:
        set_statement_item_completed(tenant_id, statement_item_id, completed)


def delete_statement_data(tenant_id: str, statement_id: str) -> None:
    """Delete statement header, items, and associated S3 artifacts."""
    if not tenant_id or not statement_id:
        return

    logger.info("Deleting statement data", tenant_id=tenant_id, statement_id=statement_id)

    # Delete statement header and statement items linked to this statement
    item_prefix = f"{statement_id}"
    query_kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(item_prefix),
        "ProjectionExpression": "#sid",
        "ExpressionAttributeNames": {"#sid": "StatementID"},
    }

    deleted_items = 0
    while True:
        resp = tenant_statements_table.query(**query_kwargs)
        items = resp.get("Items", []) or []
        if not items:
            break
        with tenant_statements_table.batch_writer() as batch:
            for item in items:
                sort_key = item.get("StatementID")
                if not sort_key:
                    continue
                batch.delete_item(Key={"TenantID": tenant_id, "StatementID": sort_key})
                deleted_items += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        query_kwargs["ExclusiveStartKey"] = lek

    # Remove S3 artifacts
    s3_keys = [statement_pdf_s3_key(tenant_id, statement_id), statement_json_s3_key(tenant_id, statement_id)]
    for key in s3_keys:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
            logger.info("Deleted statement S3 object", tenant_id=tenant_id, statement_id=statement_id, s3_key=key)
        except s3_client.exceptions.NoSuchKey:
            logger.info("Statement S3 object already missing", tenant_id=tenant_id, statement_id=statement_id, s3_key=key)
        except Exception as exc:
            logger.exception("Failed to delete statement S3 object", tenant_id=tenant_id, statement_id=statement_id, s3_key=key, error=exc)
            raise

    logger.info("Statement deletion complete", tenant_id=tenant_id, statement_id=statement_id, items_deleted=deleted_items, s3_objects=len(s3_keys))


def add_statement_to_table(tenant_id: str, entry: dict[str, str]) -> None:
    """Persist a new statement record in DynamoDB."""
    item = {
        "TenantID": tenant_id,
        "StatementID": entry["statement_id"],
        "OriginalStatementFilename": entry["statement_name"],
        "ContactID": entry["contact_id"],
        "ContactName": entry["contact_name"],
        # Store upload time in UTC for sorting/filtering
        "UploadedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "Completed": "false",
        "RecordType": "statement",
    }
    try:
        # Ensure we don't overwrite an existing statement for this tenant.
        # NOTE: Table key schema is (TenantID, StatementID). Using StatementID here is intentional.
        tenant_statements_table.put_item(Item=item, ConditionExpression=Attr("StatementID").not_exists())
        logger.info("Statement added to table", tenant_id=tenant_id, statement_id=entry["statement_id"], contact_id=entry.get("contact_id"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Using single-quotes to simplify nested quotes in f-string
            raise ValueError(f"Statement {entry['statement_name']} already exists") from e
        raise
