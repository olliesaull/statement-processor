"""Module for getting Xero data and storing it in S3"""

import json
import os
import time
from typing import Callable, Optional

from botocore.exceptions import ClientError

from config import S3_BUCKET_NAME, logger, s3_client, tenant_data_table
from xero_repository import XeroType, get_contacts_from_xero, get_credit_notes, get_invoices, get_payments
from utils import get_xero_api_client
from xero_python.accounting import AccountingApi

STAGE = os.getenv("STAGE")
LOCAL_DATA_DIR = "./tmp/data" if STAGE == "dev" else "/tmp/data"


def _sync_resource(api: AccountingApi, tenant_id: str, fetcher: Callable, resource: XeroType, start_message: str, done_message: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    logger.info(start_message, tenant_id=tenant_id)

    filename = f"{resource.value}.json"

    try:
        data = fetcher(tenant_id, api=api)

        local_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{filename}"
        s3_file = f"{tenant_id}/data/{filename}"

        os.makedirs(os.path.dirname(local_file), exist_ok=True)

        with open(local_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False, default=str)

        s3_client.upload_file(local_file, S3_BUCKET_NAME, s3_file)

        logger.info(done_message, tenant_id=tenant_id)
    
    except Exception:
        logger.exception("Unexpected error syncing resource", tenant_id=tenant_id, resource=filename)


def sync_contacts(api: AccountingApi ,tenant_id: str):
    _sync_resource(api, tenant_id, get_contacts_from_xero, XeroType.CONTACTS, "Syncing contacts", "Synced contacts")


def sync_credit_notes(api: AccountingApi, tenant_id: str):
    _sync_resource(api, tenant_id, get_credit_notes, XeroType.CREDIT_NOTES, "Syncing credit notes", "Synced credit notes")


def sync_invoices(api: AccountingApi, tenant_id: str):
    _sync_resource(api, tenant_id, get_invoices, XeroType.INVOICES, "Syncing invoices", "Synced invoices")


def sync_payments(api: AccountingApi, tenant_id: str):
    _sync_resource(api, tenant_id, get_payments, XeroType.PAYMENTS, "Syncing payments", "Synced payments")


def check_sync_required(tenant_id: str) -> bool:
    """
    Check if a row for the given tenant_id exists in the TenantData DynamoDB table.
    Returns True if sync is required (row does NOT exist), False otherwise.
    """
    try:
        response = tenant_data_table.get_item(Key={"TenantID": tenant_id})
        item_exists = "Item" in response
        sync_required = not item_exists

        logger.info("Checked tenant sync requirement", tenant_id=tenant_id, sync_required=sync_required)

        return sync_required

    except ClientError:
        logger.exception("DynamoDB get_item failed", tenant_id=tenant_id)
        return True # In case of failure, assume sync is required as a safe fallback


def mark_tenant_syncing(tenant_id: str, syncing: bool = True):
    """Mark Tenant sync state in DynamoDB"""
    if not tenant_id:
        logger.error("Missing TenantID while marking sync state")
        return False

    try:
        update_expression = "SET Syncing = :syncing"
        expression_values = {":syncing": syncing}

        if syncing:
            update_expression += ", LastSyncTime = :last_sync_time"
            expression_values[":last_sync_time"] = int(time.time() * 1000)

        tenant_data_table.update_item(
            Key={"TenantID": tenant_id},
            UpdateExpression=update_expression,
            ExpressionAttributeValues=expression_values,
        )
        logger.info("Updated tenant sync state", tenant_id=tenant_id, syncing=syncing)
        return True
    except ClientError:
        logger.exception("Failed to update tenant sync state", tenant_id=tenant_id)
        return False

def sync_data(tenant_id: str, oauth_token: Optional[dict] = None):
    """Entry point for syncing all data."""
    mark_tenant_syncing(tenant_id, syncing=True)
    api = get_xero_api_client(oauth_token)
    for func in (sync_contacts, sync_credit_notes, sync_invoices, sync_payments):
        func(api, tenant_id)

    mark_tenant_syncing(tenant_id, syncing=False)
