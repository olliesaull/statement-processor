"""
Sync Xero datasets to local cache and S3.

This module:
- fetches contacts, invoices, payments, and credit notes from Xero
- merges incremental results with cached data
- writes datasets locally and uploads them to S3
- updates tenant sync status in DynamoDB and cache
"""

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError
from xero_python.accounting import AccountingApi

import cache_provider
from config import LOCAL_DATA_DIR, S3_BUCKET_NAME, s3_client, tenant_data_table
from logger import logger
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.auth import get_xero_api_client
from xero_repository import XeroType, get_contacts_from_xero, get_credit_notes, get_invoices, get_payments


def _sync_resource(api: AccountingApi, tenant_id: str, fetcher: Callable[..., Any], resource: XeroType, start_message: str, done_message: str, modified_since: datetime | None = None) -> bool:
    """Fetch, cache, and upload a single Xero dataset."""
    if not tenant_id:
        logger.error("Missing TenantID")
        return False

    logger.info(start_message, tenant_id=tenant_id)

    resource_filename = f"{resource}.json"

    try:
        local_dir = os.path.join(LOCAL_DATA_DIR, tenant_id)
        local_path = os.path.join(local_dir, resource_filename)
        s3_key = f"{tenant_id}/data/{resource_filename}"

        # Fetch the latest dataset from Xero.
        data = fetcher(tenant_id, api=api, modified_since=modified_since)

        existing_payload = None
        if os.path.exists(local_path):
            try:
                with open(local_path, encoding="utf-8") as existing_file:
                    existing_payload = json.load(existing_file)
            except (OSError, json.JSONDecodeError, TypeError) as exc:
                logger.warning("Failed to load existing dataset", tenant_id=tenant_id, resource=resource, error=str(exc))

        # Merge incremental results with any cached data so we retain the full dataset.
        payload = _merge_resource_payload(resource, existing_payload, data) if modified_since else data if data is not None else existing_payload

        if payload is None:
            payload = []

        os.makedirs(local_dir, exist_ok=True)

        with open(local_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4, ensure_ascii=False, default=str)

        s3_client.upload_file(local_path, S3_BUCKET_NAME, s3_key)

        record_count = len(payload) if isinstance(payload, (list, dict)) else None
        logger.info(done_message, tenant_id=tenant_id, records=record_count)
        return True

    except Exception:
        logger.exception("Unexpected error syncing resource", tenant_id=tenant_id, resource=resource_filename)
        return False


def _resolve_modified_since(record: dict[str, Any] | None) -> datetime | None:  # pylint: disable=too-many-return-statements
    """Return LastSyncTime as a timezone-aware datetime if present."""
    if not record:
        return None

    raw_value = record.get("LastSyncTime")
    if raw_value is None:
        return None

    try:
        # Support raw epoch seconds/milliseconds or numeric strings.
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
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    if timestamp > 1e11:
        timestamp /= 1000

    try:
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _merge_resource_payload(resource: XeroType, existing: Any, delta: Any) -> Any:
    """
    Combine newly fetched records with any previously cached dataset.
    When we only pull a delta, this keeps the local/S3 files authoritative.
    """
    if delta is None or (isinstance(delta, (list, dict)) and not delta):  # Nothing changed
        return existing
    if existing is None:  # Only new data exists (initial load)
        return delta

    def _as_list(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, dict)]
        return []

    key_fields = {XeroType.CONTACTS: "contact_id", XeroType.CREDIT_NOTES: "credit_note_id", XeroType.PAYMENTS: "payment_id", XeroType.INVOICES: "invoice_id"}
    key = key_fields.get(resource)
    if key is None:
        return delta

    existing_list = _as_list(existing)
    delta_list = _as_list(delta)

    merged: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for source in (existing_list, delta_list):
        for item in source:
            identifier = item.get(key)
            if identifier:
                merged[identifier] = item
            else:
                extras.append(item)

    combined = list(merged.values()) + extras

    sort_keys = {
        XeroType.CONTACTS: lambda c: (c.get("name") or "").casefold(),
        XeroType.CREDIT_NOTES: lambda note: note.get("credit_note_id") or "",
        XeroType.PAYMENTS: lambda payment: payment.get("payment_id") or "",
        XeroType.INVOICES: lambda inv: str(inv.get("number") or "").casefold(),
    }
    sort_key = sort_keys.get(resource)
    if sort_key:
        combined.sort(key=sort_key)

    return combined


def sync_contacts(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync contact data from Xero."""
    return _sync_resource(api, tenant_id, get_contacts_from_xero, XeroType.CONTACTS, "Syncing contacts", "Synced contacts", modified_since=modified_since)


def sync_credit_notes(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync credit note data from Xero."""
    return _sync_resource(api, tenant_id, get_credit_notes, XeroType.CREDIT_NOTES, "Syncing credit notes", "Synced credit notes", modified_since=modified_since)


def sync_invoices(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync invoice data from Xero."""
    return _sync_resource(api, tenant_id, get_invoices, XeroType.INVOICES, "Syncing invoices", "Synced invoices", modified_since=modified_since)


def sync_payments(api: AccountingApi, tenant_id: str, modified_since: datetime | None = None) -> bool:
    """Sync payment data from Xero."""
    return _sync_resource(api, tenant_id, get_payments, XeroType.PAYMENTS, "Syncing payments", "Synced payments", modified_since=modified_since)


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
                tenant_data_table.put_item(Item={"TenantID": tenant_id, "TenantStatus": TenantStatus.LOADING}, ConditionExpression="attribute_not_exists(TenantID)")
                logger.info("Seeded tenant record with LOADING status", tenant_id=tenant_id)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    logger.exception("Failed to seed tenant status for new tenant", tenant_id=tenant_id)

        logger.info("Checked tenant sync requirement", tenant_id=tenant_id, sync_required=load_required)

        return load_required

    except ClientError:
        logger.exception("DynamoDB get_item failed", tenant_id=tenant_id)
        return True  # In case of failure, assume sync is required as a safe fallback


def update_tenant_status(tenant_id: str, tenant_status: TenantStatus = TenantStatus.FREE, last_sync_time: int | None = None) -> bool:
    """Persist the tenant's status in DynamoDB and cache."""
    if not tenant_id:
        logger.error("Missing TenantID while marking sync state")
        return False

    try:
        update_expression = "SET TenantStatus = :tenant_status"
        expression_values = {":tenant_status": tenant_status}

        if last_sync_time is not None:
            update_expression += ", LastSyncTime = :last_sync_time"
            expression_values[":last_sync_time"] = last_sync_time

        tenant_data_table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expression, ExpressionAttributeValues=expression_values)
        logger.info("Updated tenant sync state", tenant_id=tenant_id, tenant_status=tenant_status, last_sync_time=last_sync_time)
        cache_provider.set_tenant_status_cache(tenant_id, tenant_status)
        return True
    except ClientError:
        logger.exception("Failed to update tenant sync state", tenant_id=tenant_id)
        return False


def sync_data(tenant_id: str, operation_type: TenantStatus, oauth_token: dict[str, Any] | None = None) -> None:
    """Sync all datasets for a tenant and update tenant status."""
    tenant_record = TenantDataRepository.get_item(tenant_id)
    modified_since: datetime | None = None
    if operation_type != TenantStatus.LOADING and tenant_record:
        modified_since = _resolve_modified_since(tenant_record)

    start_time_ms = int(time.time() * 1000)
    update_tenant_status(tenant_id, operation_type)
    api = get_xero_api_client(oauth_token)
    all_ok = True
    sync_tasks = (sync_contacts, sync_credit_notes, sync_invoices, sync_payments)
    for sync_func in sync_tasks:
        if not sync_func(api, tenant_id, modified_since=modified_since):
            all_ok = False

    update_tenant_status(tenant_id, TenantStatus.FREE, last_sync_time=start_time_ms if all_ok else None)
