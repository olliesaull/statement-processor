"""Module for getting Xero data and storing it in S3"""

import json
import os

from config import S3_BUCKET_NAME, logger, s3_client
from xero_repository import get_contacts

STAGE = os.getenv("STAGE")
LOCAL_DATA_DIR = "./tmp/data" if STAGE == "dev" else "/tmp/data"


def sync_contacts(tenant_id: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    contacts = get_contacts()

    contacts_filename = "contacts.json"
    local_contacts_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{contacts_filename}"
    s3_contacts_file = tenant_id + "data" + contacts_filename
    s3_contacts_file = f"{tenant_id}/data/{contacts_filename}"

    os.makedirs(os.path.dirname(local_contacts_file), exist_ok=True)

    with open(local_contacts_file, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=4, ensure_ascii=False)

    s3_client.upload_file(local_contacts_file, S3_BUCKET_NAME, s3_contacts_file)

    logger.info("Synced contacts", tenant_id=tenant_id)


def sync_credit_notes(tenant_id: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    contacts = get_contacts()

    contacts_filename = "contacts.json"
    local_contacts_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{contacts_filename}"
    s3_contacts_file = tenant_id + "data" + contacts_filename
    s3_contacts_file = f"{tenant_id}/data/{contacts_filename}"

    os.makedirs(os.path.dirname(local_contacts_file), exist_ok=True)

    with open(local_contacts_file, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=4, ensure_ascii=False)

    s3_client.upload_file(local_contacts_file, S3_BUCKET_NAME, s3_contacts_file)

    logger.info("Synced contacts", tenant_id=tenant_id)


def sync_invoices(tenant_id: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    contacts = get_contacts()

    contacts_filename = "contacts.json"
    local_contacts_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{contacts_filename}"
    s3_contacts_file = tenant_id + "data" + contacts_filename
    s3_contacts_file = f"{tenant_id}/data/{contacts_filename}"

    os.makedirs(os.path.dirname(local_contacts_file), exist_ok=True)

    with open(local_contacts_file, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=4, ensure_ascii=False)

    s3_client.upload_file(local_contacts_file, S3_BUCKET_NAME, s3_contacts_file)

    logger.info("Synced contacts", tenant_id=tenant_id)


def sync_payments(tenant_id: str):
    if not tenant_id:
        logger.error("Missing TenantID")

    contacts = get_contacts()

    contacts_filename = "contacts.json"
    local_contacts_file = f"{LOCAL_DATA_DIR}/{tenant_id}/{contacts_filename}"
    s3_contacts_file = tenant_id + "data" + contacts_filename
    s3_contacts_file = f"{tenant_id}/data/{contacts_filename}"

    os.makedirs(os.path.dirname(local_contacts_file), exist_ok=True)

    with open(local_contacts_file, "w", encoding="utf-8") as f:
        json.dump(contacts, f, indent=4, ensure_ascii=False)

    s3_client.upload_file(local_contacts_file, S3_BUCKET_NAME, s3_contacts_file)

    logger.info("Synced contacts", tenant_id=tenant_id)


def sync_data(tenant_id: str):
    "Entry point for syncing all data"
    sync_contacts()
