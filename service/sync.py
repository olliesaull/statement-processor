"""Module for getting Xero data and storing it in S3"""

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from botocore.exceptions import ClientError

from config import S3_BUCKET_NAME, logger, s3_client, tenant_data_table
from xero_repository import XeroType, get_contacts_from_xero, get_credit_notes, get_invoices, get_payments
from utils import get_xero_api_client
from xero_python.accounting import AccountingApi
from tenant_data_repository import TenantDataRepository, TenantStatus

STAGE = os.getenv("STAGE")
LOCAL_DATA_DIR = "./tmp/data" if STAGE == "dev" else "/tmp/data"


def _sync_resource(api: AccountingApi, tenant_id: str, fetcher: Callable, resource: XeroType, start_message: str, done_message: str, modified_since: Optional[datetime] = None):
    if not tenant_id:
        logger.error("Missing TenantID")

    logger.info(start_message, tenant_id=tenant_id)

    filename = f"{resource.value}.json"

    try:
        data = fetcher(tenant_id, api=api, modified_since=modified_since)

        local_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{filename}"
        s3_file = f"{tenant_id}/data/{filename}"

        os.makedirs(os.path.dirname(local_file), exist_ok=True)

        with open(local_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)

        s3_client.upload_file(local_file, S3_BUCKET_NAME, s3_file)

        logger.info(done_message, tenant_id=tenant_id)

    except Exception:
        logger.exception("Unexpected error syncing resource", tenant_id=tenant_id, resource=filename)


def _resolve_modified_since(record: Optional[dict[str, Any]]) -> Optional[datetime]:
    """Return LastSyncTime as a timezone-aware datetime if present."""
    if not record:
        return None

    raw_value = record.get("LastSyncTime")
    if raw_value is None:
        return None

    try:
        if isinstance(raw_value, (Decimal, int, float)):
            timestamp = float(raw_value)
        elif isinstance(raw_value, str) and raw_value.strip():
            timestamp = float(raw_value.strip())
        else:
            return None
    except (ValueError, TypeError):
        try:
            normalised = str(raw_value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalised)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    if timestamp > 1e11:
        timestamp /= 1000

    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def sync_contacts(api: AccountingApi, tenant_id: str, modified_since: Optional[datetime] = None):
    _sync_resource(api, tenant_id, get_contacts_from_xero, XeroType.CONTACTS, "Syncing contacts", "Synced contacts", modified_since=modified_since)


def sync_credit_notes(api: AccountingApi, tenant_id: str, modified_since: Optional[datetime] = None):
    _sync_resource(api, tenant_id, get_credit_notes, XeroType.CREDIT_NOTES, "Syncing credit notes", "Synced credit notes", modified_since=modified_since)


def sync_invoices(api: AccountingApi, tenant_id: str, modified_since: Optional[datetime] = None):
    _sync_resource(api, tenant_id, get_invoices, XeroType.INVOICES, "Syncing invoices", "Synced invoices", modified_since=modified_since)


def sync_payments(api: AccountingApi, tenant_id: str, modified_since: Optional[datetime] = None):
    _sync_resource(api, tenant_id, get_payments, XeroType.PAYMENTS, "Syncing payments", "Synced payments", modified_since=modified_since)


def check_load_required(tenant_id: str) -> bool:
    """
    Check if a row for the given tenant_id exists in the TenantData DynamoDB table.
    Returns True if sync is required (row does NOT exist), False otherwise.
    """
    try:
        response = tenant_data_table.get_item(Key={"TenantID": tenant_id})
        item_exists = "Item" in response
        load_required = not item_exists

        if load_required:
            try:
                tenant_data_table.put_item(
                    Item={
                        "TenantID": tenant_id,
                        "TenantStatus": TenantStatus.LOADING.value,
                    },
                    ConditionExpression="attribute_not_exists(TenantID)",
                )
                logger.info("Seeded tenant record with LOADING status", tenant_id=tenant_id)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    logger.exception("Failed to seed tenant status for new tenant", tenant_id=tenant_id)

        logger.info("Checked tenant sync requirement", tenant_id=tenant_id, sync_required=load_required)

        return load_required

    except ClientError:
        logger.exception("DynamoDB get_item failed", tenant_id=tenant_id)
        return True # In case of failure, assume sync is required as a safe fallback


def update_tenant_status(tenant_id: str, tenant_status: TenantStatus = TenantStatus.FREE):
    """Mark Tenant sync state in DynamoDB"""
    if not tenant_id:
        logger.error("Missing TenantID while marking sync state")
        return False

    try:
        update_expression = "SET TenantStatus = :tenant_status"
        expression_values = {":tenant_status": tenant_status.value}

        if tenant_status == TenantStatus.FREE:
            update_expression += ", LastSyncTime = :last_sync_time"
            expression_values[":last_sync_time"] = int(time.time() * 1000)

        tenant_data_table.update_item(
            Key={"TenantID": tenant_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
        )
        logger.info("Updated tenant sync state", tenant_id=tenant_id, tenant_status=tenant_status)
        return True
    except ClientError:
        logger.exception("Failed to update tenant sync state", tenant_id=tenant_id)
        return False

def sync_data(tenant_id: str, operation_type: TenantStatus, oauth_token: Optional[dict] = None):
    """Entry point for syncing all data."""
    tenant_record = TenantDataRepository.get_item(tenant_id)
    modified_since: Optional[datetime] = None
    if operation_type != TenantStatus.LOADING and tenant_record:
        modified_since = _resolve_modified_since(tenant_record)

    update_tenant_status(tenant_id, operation_type)
    api = get_xero_api_client(oauth_token)
    for func in (sync_contacts, sync_credit_notes, sync_invoices, sync_payments):
        func(api, tenant_id, modified_since=modified_since)

    update_tenant_status(tenant_id, TenantStatus.FREE)
