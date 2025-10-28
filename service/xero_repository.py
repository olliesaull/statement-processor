"""Module for all Xero data API calls"""

import json
import os
from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, Iterable, List, Optional

from flask import session
from xero_python.exceptions import AccountingBadRequestException
from xero_python.accounting import AccountingApi

from config import S3_BUCKET_NAME, logger, s3_client
from utils import fmt_date, fmt_invoice_data, raise_for_unauthorized, get_xero_api_client

PAGE_SIZE = 100  # Xero max
STAGE = os.getenv("STAGE")
LOCAL_DATA_DIR = "./tmp/data" if STAGE == "dev" else "/tmp/data"


class XeroType(StrEnum):
    INVOICES = "invoices"
    CREDIT_NOTES = "credit_notes"
    PAYMENTS = "payments"
    CONTACTS = "contacts"


def load_local_dataset(resource: XeroType, tenant_id: Optional[str] = None) -> Optional[Any]:
    """
    Load a locally cached dataset produced by the sync job. If dataset not found locally download it from S3.

    Args:
        resource: `XeroType` dataset identifier.
        tenant_id: Optional explicit tenant ID; defaults to the active session tenant.

    Returns:
        Parsed JSON payload (list or dict) or None if unavailable.
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping local dataset load; tenant not selected", resource=resource)
        return None

    data_path = os.path.join(LOCAL_DATA_DIR, tenant_id, f"{resource}.json")

    try:
        with open(data_path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        logger.info("Local dataset not found", tenant_id=tenant_id, resource=resource, path=data_path)
        s3_key = f"{tenant_id}/data/{resource}.json"
        try:
            os.makedirs(os.path.dirname(data_path), exist_ok=True)
            s3_client.download_file(S3_BUCKET_NAME, s3_key, data_path)
            logger.info("Downloaded file from S3", tenant_id=tenant_id, resource=resource, path=data_path)
            with open(data_path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            logger.info("Dataset still missing after S3 download attempt", tenant_id=tenant_id, resource=resource, path=data_path)
        except s3_client.exceptions.NoSuchKey:
            logger.info("Dataset not present in S3", tenant_id=tenant_id, resource=resource, s3_key=s3_key)
        except Exception:
            logger.exception("Failed to download dataset from S3", tenant_id=tenant_id, resource=resource, s3_key=s3_key)
    except json.JSONDecodeError:
        logger.exception("Failed to parse local dataset", tenant_id=tenant_id, resource=resource, path=data_path)
    except Exception:
        logger.exception("Failed to load local dataset", tenant_id=tenant_id, resource=resource, path=data_path)

    return None


def get_contacts_from_xero(tenant_id: Optional[str] = None, modified_since: Optional[datetime] = None, api: Optional[AccountingApi] = None) -> List[Dict[str, Any]]:
    """Fetch contacts directly from Xero ordered by name."""
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping contact lookup; tenant not selected")
        return []

    client = api or get_xero_api_client()

    page = 1
    contacts: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        logger.info("Fetching contacts", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        while True:
            kwargs = {"xero_tenant_id": tenant_id, "page": page, "include_archived": False, "page_size": PAGE_SIZE}
            if modified_since:
                kwargs["if_modified_since"] = modified_since

            result = client.get_contacts(**kwargs)
            batch = result.contacts or []
            if not batch:
                break

            for item in batch:
                contact_id = getattr(item, "contact_id", None)
                if not contact_id:
                    continue
                key = str(contact_id)
                if key in seen_ids:
                    continue
                seen_ids.add(key)

                updated_raw = getattr(item, "updated_date_utc", None)
                if isinstance(updated_raw, datetime):
                    updated_iso = updated_raw.isoformat()
                elif updated_raw is not None:
                    updated_iso = str(updated_raw)
                else:
                    updated_iso = None

                contacts.append(
                    {
                        "contact_id": key,
                        "name": getattr(item, "name", None),
                        "updated_at": updated_iso,
                    }
                )

            logger.debug("Fetched contact page", tenant_id=tenant_id, page=page, returned=len(batch))

            if len(batch) < PAGE_SIZE:
                break
            page += 1

        contacts.sort(key=lambda c: (c.get("name") or "").casefold())
        logger.info("Fetched contacts", tenant_id=tenant_id, returned=len(contacts))
        return contacts

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch contacts", tenant_id=tenant_id, error=e)
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Unexpected error fetching contacts", tenant_id=tenant_id, error=e)
    return []


def get_invoices(tenant_id: Optional[str] = None, modified_since: Optional[datetime] = None, api: Optional[AccountingApi] = None) -> Dict[str, Dict[str, Any]]:
    """Get all invoices from Xero, across all pages.

    Args:
        modified_since: If provided, only invoices modified since this datetime are returned.
        statuses: Optional explicit list of statuses to fetch. Defaults to common non-deleted statuses.
        include_archived: Whether to include archived contacts' invoices (content still filtered by statuses).

    Returns:
        Dict keyed by invoice number with normalized invoice records.
    """

    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping invoice lookup; tenant not selected")
        return {}

    client = api or get_xero_api_client()

    page = 1
    total_returned = 0
    by_number: Dict[str, Dict[str, Any]] = {}

    try:
        logger.info("Fetching all invoices (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {
            "xero_tenant_id": tenant_id,
            "order": "UpdatedDateUTC ASC",
            "page_size": PAGE_SIZE,
            "statuses": ["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"], # Excludes DELETED
        }

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_invoices(**kwargs)

            invs = (result.invoices or [])
            batch_count = len(invs)
            total_returned += batch_count

            logger.debug("Fetched invoice page", tenant_id=tenant_id, page=page, returned=batch_count)

            for inv in invs:
                rec = fmt_invoice_data(inv)
                n = rec.get("number")
                if n:
                    # Last one wins if duplicates appear
                    by_number[n] = rec

            # Stop when the final page returns less than PAGE_SIZE
            if batch_count < PAGE_SIZE:
                break

            page += 1

        logger.info("Fetched all invoices", tenant_id=tenant_id, pages=page, returned=total_returned, unique_numbers=len(by_number))
        return by_number

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices", tenant_id=tenant_id, error=e)
        return {}
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices", tenant_id=tenant_id, error=e)
        return {}


def get_credit_notes(tenant_id: Optional[str] = None, modified_since: Optional[datetime] = None, api: Optional[AccountingApi] = None) -> List[Dict[str, Any]]:
    """
    Get all credit notes across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only credit notes modified since this timestamp.
        page_size: Page size to request (Xero max is 100).

    Returns:
        A list of credit note dicts (same shape as previous per-contact function).
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        return []

    client = api or get_xero_api_client()

    page = 1
    credit_notes: List[Dict[str, Any]] = []

    try:
        logger.info("Fetching all credit notes (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {"xero_tenant_id": tenant_id, "order": "UpdatedDateUTC ASC", "page_size": PAGE_SIZE}

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_credit_notes(**kwargs)
            batch = result.credit_notes or []
            if not batch:
                break

            for note in batch:
                contact = getattr(note, "contact", None)

                if contact:
                    _contact_id = getattr(contact, "contact_id", None)
                    contact_name = getattr(contact, "name", None)
                else:
                    _contact_id = contact_name = None

                credit_notes.append(
                    {
                        "credit_note_id": getattr(note, "credit_note_id", None),
                        "number": getattr(note, "credit_note_number", None),
                        "type": getattr(note, "type", None),
                        "status": getattr(note, "status", None),
                        "date": fmt_date(getattr(note, "date", None)),
                        "due_date": fmt_date(getattr(note, "due_date", None)),
                        "reference": getattr(note, "reference", None),
                        "total": getattr(note, "total", None),
                        "amount_credited": getattr(note, "amount_credited", None),
                        "remaining_credit": getattr(note, "remaining_credit", None),
                        "contact_id": _contact_id,
                        "contact_name": contact_name,
                    }
                )

            logger.debug("Fetched credit note page", tenant_id=tenant_id, page=page, returned=len(batch))

            if len(batch) < PAGE_SIZE:
                break
            page += 1

        logger.info("Fetched all credit notes", tenant_id=tenant_id, pages=page, returned=len(credit_notes))
        return credit_notes

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch credit notes", tenant_id=tenant_id, error=e)
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Unexpected error fetching credit notes", tenant_id=tenant_id, error=e)
    return []


def get_payments(tenant_id: Optional[str] = None, modified_since: Optional[datetime] = None, api: Optional[AccountingApi] = None) -> List[Dict[str, Any]]:
    """
    Get all payments across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only payments modified since this timestamp.
        page_size: Page size to request (Xero max is 100).
        order: Field to order by for stable pagination.

    Returns:
        A list of payment dicts (same shape as previous per-contact function).
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        return []

    client = api or get_xero_api_client()

    page = 1
    payments: List[Dict[str, Any]] = []

    try:
        logger.info("Fetching all payments (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {"xero_tenant_id": tenant_id, "order": "UpdatedDateUTC ASC", "page_size": PAGE_SIZE}

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_payments(**kwargs)
            batch = result.payments or []
            if not batch:
                break

            for payment in batch:
                invoice_obj = getattr(payment, "invoice", None)
                if invoice_obj:
                    invoice_id = getattr(invoice_obj, "invoice_id", None)
                    contact = getattr(invoice_obj, "contact", None)
                else:
                    invoice_id = None
                    contact = None

                payments.append(
                    {
                        "payment_id": getattr(payment, "payment_id", None),
                        "invoice_id": invoice_id,
                        "reference": getattr(payment, "reference", None),
                        "amount": getattr(payment, "amount", None),
                        "date": fmt_date(getattr(payment, "date", None)),
                        "status": getattr(payment, "status", None),
                        "contact_id": getattr(contact, "contact_id", None) if contact else None,
                        "contact_name": getattr(contact, "name", None) if contact else None,
                    }
                )

            logger.debug("Fetched payment page", tenant_id=tenant_id, page=page, returned=len(batch))

            if len(batch) < PAGE_SIZE:
                break
            page += 1

        logger.info("Fetched all payments", tenant_id=tenant_id, pages=page, returned=len(payments))
        return payments

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch payments", tenant_id=tenant_id, error=e)
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Unexpected error fetching payments", tenant_id=tenant_id, error=e)
    return []


def get_contacts(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return cached contacts for the active tenant."""
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping contact lookup; tenant not selected")
        return []

    try:
        cached = load_local_dataset(XeroType.CONTACTS, tenant_id=tenant_id) or []
        if not cached:
            logger.info("No cached contacts available", tenant_id=tenant_id)
            return []

        contacts = list(cached)
        contacts.sort(key=lambda c: (c.get("name") or "").casefold())
        logger.info("Loaded contacts from cache", tenant_id=tenant_id, returned=len(contacts))
        return contacts

    except Exception:
        logger.exception("Failed to load contacts from cache", tenant_id=tenant_id)
        return []


def get_invoices_by_numbers(invoice_numbers: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch invoices for a list of invoice numbers from the locally cached dataset.
    Returns a dict keyed by invoice number: { "INV-001": {...}, ... }
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping invoice number lookup; tenant not selected")
        return {}

    if not invoice_numbers:
        return {}

    # normalize & de-dupe while preserving order (helps batching)
    normalized = []
    seen = set()
    for n in (str(x).strip() for x in invoice_numbers if str(x).strip()):
        if n not in seen:
            seen.add(n)
            normalized.append(n)

    try:
        cached = load_local_dataset(XeroType.INVOICES, tenant_id=tenant_id) or {}
        if not cached:
            logger.info("No cached invoices available", tenant_id=tenant_id)
            return {}

        result: Dict[str, Dict[str, Any]] = {}
        for number in normalized:
            if (invoice := cached.get(number)):
                result[number] = invoice

        logger.info("Fetched invoices by numbers", tenant_id=tenant_id, requested=len(normalized), returned=len(result))
        return result

    except Exception:
        logger.exception("Failed to load invoices by numbers from cache", tenant_id=tenant_id)
        return {}


def get_invoices_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    """Return cached invoices for the specified contact."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping invoice lookup; tenant not selected", contact_id=contact_id)
        return []

    try:
        cached = load_local_dataset(XeroType.INVOICES, tenant_id=tenant_id) or {}
        if not cached:
            logger.info("No cached invoices available", tenant_id=tenant_id)
            return []

        invoices = [inv for inv in cached.values() if inv.get("contact_id") == contact_id]
        logger.info("Fetched invoices for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(invoices))
        return invoices

    except Exception:
        logger.exception("Failed to load invoices for contact from cache", tenant_id=tenant_id, contact_id=contact_id)
        return []


def get_credit_notes_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    """Return cached credit notes for the specified contact."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id or not contact_id:
        return []

    try:
        cached = load_local_dataset(XeroType.CREDIT_NOTES, tenant_id=tenant_id) or []
        if not cached:
            logger.info("No cached credit notes available", tenant_id=tenant_id)
            return []

        credit_notes = [note for note in cached if note.get("contact_id") == contact_id]
        logger.info("Fetched credit notes for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(credit_notes))
        return credit_notes

    except Exception:
        logger.exception("Failed to load credit notes for contact from cache", tenant_id=tenant_id, contact_id=contact_id)
        return []


def get_payments_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    """Return cached payments for the specified contact."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id or not contact_id:
        return []

    try:
        cached = load_local_dataset(XeroType.PAYMENTS, tenant_id=tenant_id) or []
        if not cached:
            logger.info("No cached payments available", tenant_id=tenant_id)
            return []

        payments = [payment for payment in cached if payment.get("contact_id") == contact_id]
        logger.info("Fetched payments for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(payments))
        return payments

    except Exception:
        logger.exception("Failed to load payments for contact from cache", tenant_id=tenant_id, contact_id=contact_id)
        return []
