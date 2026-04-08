"""Lambda entry point for scheduled tenant data erasure.

This handler:
- Scans TenantDataTable for tenants past their EraseTenantDataTime
- Claims each tenant with a conditional write (marks ERASED) BEFORE
  deleting data, so a concurrent reconnection cannot leave the tenant
  in a state where data has been deleted but status is still FREE
- Deletes S3 objects and TenantStatementsTable rows for claimed tenants
"""

import time
from typing import Any

from botocore.exceptions import ClientError

from config import S3_BUCKET_NAME, s3_client, tenant_data_table, tenant_statements_table
from logger import logger

# Statuses that indicate an active operation — skip erasure.
_ACTIVE_STATUSES = {"LOADING", "SYNCING"}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Scan for and erase tenant data past its scheduled erasure time."""
    now_ms = int(time.time() * 1000)
    erased_count = 0
    skipped_count = 0
    failed_count = 0

    tenants = _scan_for_erasable_tenants(now_ms)
    logger.info("Found tenants pending erasure", count=len(tenants))

    for tenant in tenants:
        tenant_id = tenant["TenantID"]
        status = tenant.get("TenantStatus", "")

        if status in _ACTIVE_STATUSES:
            logger.warning("Skipping tenant with active operation", tenant_id=tenant_id, status=status)
            skipped_count += 1
            continue

        # Claim the tenant FIRST with a conditional write. This marks it as
        # ERASED and removes EraseTenantDataTime atomically. If a concurrent
        # reconnection already cleared EraseTenantDataTime, the condition
        # fails and we skip — no data is touched.
        try:
            _mark_as_erased(tenant_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                logger.warning("Tenant reconnected during erasure, skipping", tenant_id=tenant_id)
                skipped_count += 1
            else:
                logger.error("tenant_erasure_failed", tenant_id=tenant_id, error=str(exc))
                failed_count += 1
            continue
        except Exception as exc:
            logger.error("tenant_erasure_failed", tenant_id=tenant_id, error=str(exc))
            failed_count += 1
            continue

        # Tenant is now claimed (ERASED). Delete the actual data.
        # If the Lambda crashes here, the tenant is already ERASED so a
        # reconnection triggers a fresh LOADING that overwrites orphaned data.
        try:
            s3_deleted = _delete_s3_objects(tenant_id)
            statements_deleted = _delete_statement_rows(tenant_id)
            logger.info("tenant_data_erased", tenant_id=tenant_id, s3_objects_deleted=s3_deleted, statements_deleted=statements_deleted)
            erased_count += 1
        except Exception as exc:
            logger.error("tenant_erasure_failed", tenant_id=tenant_id, error=str(exc))
            failed_count += 1

    logger.info("Erasure run complete", erased=erased_count, skipped=skipped_count, failed=failed_count)
    return {"erased": erased_count, "skipped": skipped_count, "failed": failed_count}


def _scan_for_erasable_tenants(now_ms: int) -> list[dict[str, Any]]:
    """Scan TenantDataTable for tenants with EraseTenantDataTime in the past."""
    results: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {"FilterExpression": "attribute_exists(EraseTenantDataTime) AND EraseTenantDataTime <= :now", "ExpressionAttributeValues": {":now": now_ms}}

    while True:
        response = tenant_data_table.scan(**scan_kwargs)
        results.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    return results


def _delete_s3_objects(tenant_id: str) -> int:
    """Delete all S3 objects under the tenant's prefix."""
    prefix = f"{tenant_id}/"
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=prefix):
        contents = page.get("Contents", [])
        if not contents:
            continue
        keys = [{"Key": obj["Key"]} for obj in contents]
        s3_client.delete_objects(Bucket=S3_BUCKET_NAME, Delete={"Objects": keys, "Quiet": True})
        deleted += len(keys)

    return deleted


def _delete_statement_rows(tenant_id: str) -> int:
    """Delete all TenantStatementsTable rows for the tenant."""
    deleted = 0
    query_kwargs: dict[str, Any] = {"KeyConditionExpression": "TenantID = :tid", "ExpressionAttributeValues": {":tid": tenant_id}, "ProjectionExpression": "TenantID, StatementID"}

    while True:
        response = tenant_statements_table.query(**query_kwargs)
        items = response.get("Items", [])
        if not items:
            break

        with tenant_statements_table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"TenantID": item["TenantID"], "StatementID": item["StatementID"]})
                deleted += 1

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key

    return deleted


def _mark_as_erased(tenant_id: str) -> None:
    """Set status to ERASED and clean up attributes.

    Uses a conditional write to prevent race conditions — if the tenant
    reconnected and cleared EraseTenantDataTime, this raises
    ConditionalCheckFailedException (handled by the caller).
    """
    tenant_data_table.update_item(
        Key={"TenantID": tenant_id},
        UpdateExpression="SET TenantStatus = :erased REMOVE EraseTenantDataTime, LastSyncTime",
        ExpressionAttributeValues={":erased": "ERASED"},
        ConditionExpression="attribute_exists(EraseTenantDataTime)",
    )
