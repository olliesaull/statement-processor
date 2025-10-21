"""Module for getting Xero data and storing it in S3"""

import json
import os
from typing import Callable

from botocore.exceptions import ClientError

from config import S3_BUCKET_NAME, logger, s3_client, tenant_data_table
from xero_repository import get_contacts, get_credit_notes, get_invoices, get_payments

STAGE = os.getenv("STAGE")
LOCAL_DATA_DIR = "./tmp/data" if STAGE == "dev" else "/tmp/data"


def _sync_resource(tenant_id: str, fetcher: Callable, filename: str, start_message: str, done_message: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    logger.info(start_message, tenant_id=tenant_id)

    data = fetcher()

    local_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{filename}"
    s3_file = f"{tenant_id}/data/{filename}"

    os.makedirs(os.path.dirname(local_file), exist_ok=True)

    with open(local_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    s3_client.upload_file(local_file, S3_BUCKET_NAME, s3_file)

    logger.info(done_message, tenant_id=tenant_id)


def sync_contacts(tenant_id: str):
    _sync_resource(tenant_id, get_contacts, "contacts.json", "Syncing contacts", "Synced contacts")


def sync_credit_notes(tenant_id: str):
    _sync_resource(tenant_id, get_credit_notes, "credit_notes.json", "Syncing credit notes", "Synced credit notes")


def sync_invoices(tenant_id: str):
    _sync_resource(tenant_id, get_invoices, "invoices.json", "Syncing invoices", "Synced invoices")


def sync_payments(tenant_id: str):
    _sync_resource(tenant_id, get_payments, "payments.json", "Syncing payments", "Synced payments")


def check_sync_required(tenant_id: str) -> bool:
    """
    Check if a row for the given tenant_id exists in the TenantData DynamoDB table.
    Returns True if sync is required (row does NOT exist), False otherwise.
    """
    try:
        response = tenant_data_table.get_item(Key={"tenant_id": tenant_id})
        item_exists = "Item" in response
        sync_required = not item_exists

        logger.info("Checked tenant sync requirement", tenant_id=tenant_id, sync_required=sync_required)

        return sync_required

    except ClientError:
        logger.exception("DynamoDB get_item failed", tenant_id=tenant_id)
        return True # In case of failure, assume sync is required as a safe fallback


def sync_data(tenant_id: str):
    """Entry point for syncing all data."""
    if check_sync_required(tenant_id):
        for func in (sync_contacts, sync_credit_notes, sync_invoices, sync_payments):
            func(tenant_id)
