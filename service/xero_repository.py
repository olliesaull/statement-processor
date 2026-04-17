"""
Xero data accessors and local/S3 cache helpers.

This module:
- fetches data from the Xero Accounting API
- normalizes objects into dicts suitable for storage
- loads cached datasets from local disk or S3
"""

import json
import os
from datetime import datetime
from enum import StrEnum
from typing import Any

from flask import session
from xero_python.accounting import AccountingApi
from xero_python.exceptions import AccountingBadRequestException

from config import LOCAL_DATA_DIR, S3_BUCKET_NAME, s3_client
from logger import logger
from utils.auth import get_xero_api_client, raise_for_unauthorized
from utils.formatting import fmt_date, fmt_invoice_data

# Per-endpoint page sizes. The Accounting API supports page_size up to 1000 for
# invoices, credit notes, and payments. Contacts historically capped at 100;
# re-verify before bumping. `result.pagination.item_count` is only returned
# when an explicit page_size is passed, so bumping these also unlocks real
# progress totals for the sync UI (Step 1 of the contacts-first unlock plan).
INVOICES_PAGE_SIZE: int = 1000
CREDIT_NOTES_PAGE_SIZE: int = 1000
PAYMENTS_PAGE_SIZE: int = 1000
CONTACTS_PAGE_SIZE: int = 100


class XeroType(StrEnum):
    """Dataset identifiers used for cache keys and S3 paths."""

    INVOICES = "invoices"
    CREDIT_NOTES = "credit_notes"
    PAYMENTS = "payments"
    CONTACTS = "contacts"


def load_local_dataset(resource: XeroType, tenant_id: str | None = None) -> Any | None:
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

    resource_filename = f"{resource}.json"
    local_path = os.path.join(LOCAL_DATA_DIR, tenant_id, resource_filename)
    local_dir = os.path.dirname(local_path)

    try:
        with open(local_path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        logger.info("Local dataset not found", tenant_id=tenant_id, resource=resource, path=local_path)
        s3_key = f"{tenant_id}/data/{resource_filename}"
        try:
            os.makedirs(local_dir, exist_ok=True)
            s3_client.download_file(S3_BUCKET_NAME, s3_key, local_path)
            logger.info("Downloaded file from S3", tenant_id=tenant_id, resource=resource, path=local_path)
            with open(local_path, encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            logger.info("Dataset still missing after S3 download attempt", tenant_id=tenant_id, resource=resource, path=local_path)
        except s3_client.exceptions.NoSuchKey:
            logger.info("Dataset not present in S3", tenant_id=tenant_id, resource=resource, s3_key=s3_key)
        except Exception:
            logger.exception("Failed to download dataset from S3", tenant_id=tenant_id, resource=resource, s3_key=s3_key)
    except json.JSONDecodeError:
        logger.exception("Failed to parse local dataset", tenant_id=tenant_id, resource=resource, path=local_path)
    except Exception:
        logger.exception("Failed to load local dataset", tenant_id=tenant_id, resource=resource, path=local_path)

    return None


def get_contacts_from_xero(tenant_id: str | None = None, modified_since: datetime | None = None, api: AccountingApi | None = None) -> list[dict[str, Any]]:
    """Fetch contacts directly from Xero ordered by name."""
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping contact lookup; tenant not selected")
        return []

    client = api or get_xero_api_client()

    page = 1
    contacts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()  # De-dupe contacts across pages.

    try:
        logger.info("Fetching contacts", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        while True:
            kwargs = {"xero_tenant_id": tenant_id, "page": page, "include_archived": True, "page_size": CONTACTS_PAGE_SIZE}
            if modified_since:
                kwargs["if_modified_since"] = modified_since

            result = client.get_contacts(**kwargs)
            batch = result.contacts or []
            if page == 1:
                pagination = getattr(result, "pagination", None)
                logger.info(
                    "Xero pagination metadata",
                    resource="contacts",
                    has_pagination=pagination is not None,
                    item_count=getattr(pagination, "item_count", None) if pagination else None,
                    page_count=getattr(pagination, "page_count", None) if pagination else None,
                )
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

                contacts.append({"contact_id": key, "name": getattr(item, "name", None), "updated_at": updated_iso, "contact_status": getattr(item, "contact_status", None)})

            logger.debug("Fetched contact page", tenant_id=tenant_id, page=page, returned=len(batch))

            if len(batch) < CONTACTS_PAGE_SIZE:
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


def get_invoices(tenant_id: str | None = None, modified_since: datetime | None = None, api: AccountingApi | None = None) -> list[dict[str, Any]]:
    """Get all supplier bills (ACCPAY) from Xero, across all pages."""

    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping invoice lookup; tenant not selected")
        return []

    client = api or get_xero_api_client()

    page = 1
    total_returned = 0
    by_id: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []

    try:
        logger.info("Fetching all invoices (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {
            "xero_tenant_id": tenant_id,
            "order": "UpdatedDateUTC ASC",
            "page_size": INVOICES_PAGE_SIZE,
            "statuses": ["DRAFT", "SUBMITTED", "AUTHORISED", "PAID"],  # Excludes DELETED and VOIDED
            # Only fetch supplier bills (exclude ACCREC)
            "where": 'Type=="ACCPAY"',
        }

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_invoices(**kwargs)

            invs = result.invoices or []
            batch_count = len(invs)
            total_returned += batch_count

            if page == 1:
                pagination = getattr(result, "pagination", None)
                logger.info(
                    "Xero pagination metadata",
                    resource="invoices",
                    has_pagination=pagination is not None,
                    item_count=getattr(pagination, "item_count", None) if pagination else None,
                    page_count=getattr(pagination, "page_count", None) if pagination else None,
                )

            logger.debug("Fetched invoice page", tenant_id=tenant_id, page=page, returned=batch_count)

            for inv in invs:
                rec = fmt_invoice_data(inv)
                inv_id = rec.get("invoice_id")
                if inv_id:
                    by_id[str(inv_id)] = rec
                else:
                    extras.append(rec)

            # Stop when the final page returns less than the per-endpoint page size
            if batch_count < INVOICES_PAGE_SIZE:
                break

            page += 1

        invoices: list[dict[str, Any]] = list(by_id.values()) + extras
        invoices.sort(key=lambda inv: str(inv.get("number") or "").casefold())
        logger.info("Fetched all invoices", tenant_id=tenant_id, pages=page, returned=total_returned, unique_ids=len(by_id) if by_id else len(invoices))
        return invoices

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices", tenant_id=tenant_id, error=e)
        return []
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices", tenant_id=tenant_id, error=e)
        return []


def get_credit_notes(tenant_id: str | None = None, modified_since: datetime | None = None, api: AccountingApi | None = None) -> list[dict[str, Any]]:
    """
    Get all supplier credit notes (ACCPAYCREDIT) across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only credit notes modified since this timestamp.

    Returns:
        A list of credit note dicts (same shape as previous per-contact function).
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        return []

    client = api or get_xero_api_client()

    page = 1
    credit_notes: list[dict[str, Any]] = []

    try:
        logger.info("Fetching all credit notes (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {
            "xero_tenant_id": tenant_id,
            "order": "UpdatedDateUTC ASC",
            "page_size": CREDIT_NOTES_PAGE_SIZE,
            # Only fetch supplier credit notes (exclude ACCRECCREDIT)
            "where": 'Type=="ACCPAYCREDIT"',
        }

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_credit_notes(**kwargs)
            batch = result.credit_notes or []
            if page == 1:
                pagination = getattr(result, "pagination", None)
                logger.info(
                    "Xero pagination metadata",
                    resource="credit_notes",
                    has_pagination=pagination is not None,
                    item_count=getattr(pagination, "item_count", None) if pagination else None,
                    page_count=getattr(pagination, "page_count", None) if pagination else None,
                )
            if not batch:
                break

            for note in batch:
                contact = getattr(note, "contact", None)

                if contact:
                    contact_id = getattr(contact, "contact_id", None)
                    contact_name = getattr(contact, "name", None)
                else:
                    contact_id = contact_name = None

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
                        "contact_id": contact_id,
                        "contact_name": contact_name,
                    }
                )

            logger.debug("Fetched credit note page", tenant_id=tenant_id, page=page, returned=len(batch))

            if len(batch) < CREDIT_NOTES_PAGE_SIZE:
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


def get_payments(tenant_id: str | None = None, modified_since: datetime | None = None, api: AccountingApi | None = None) -> list[dict[str, Any]]:
    """
    Get all payments across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only payments modified since this timestamp.

    Returns:
        A list of payment dicts (same shape as previous per-contact function).
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id:
        return []

    client = api or get_xero_api_client()

    page = 1
    payments: list[dict[str, Any]] = []

    try:
        logger.info("Fetching all payments (paged)", tenant_id=tenant_id, modified_since=str(modified_since) if modified_since else None)

        kwargs = {"xero_tenant_id": tenant_id, "order": "UpdatedDateUTC ASC", "page_size": PAYMENTS_PAGE_SIZE}

        if modified_since:
            kwargs["if_modified_since"] = modified_since

        while True:
            kwargs["page"] = page
            result = client.get_payments(**kwargs)
            batch = result.payments or []
            if page == 1:
                pagination = getattr(result, "pagination", None)
                logger.info(
                    "Xero pagination metadata",
                    resource="payments",
                    has_pagination=pagination is not None,
                    item_count=getattr(pagination, "item_count", None) if pagination else None,
                    page_count=getattr(pagination, "page_count", None) if pagination else None,
                )
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

            if len(batch) < PAYMENTS_PAGE_SIZE:
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


def get_contacts(tenant_id: str | None = None) -> list[dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# Per-contact combined Xero data (invoices + credit notes + payments).
# ---------------------------------------------------------------------------

# Document types stored in per-contact index files.  Shared with
# sync.build_per_contact_index (the writer) so adding a new type is
# a single-constant change.
CONTACT_DOC_TYPES: tuple[str, ...] = ("invoices", "credit_notes", "payments")


def _empty_contact_data() -> dict[str, list[dict[str, Any]]]:
    """Return a fresh empty contact data structure.

    Uses a factory function (not a module-level constant) to avoid the
    shallow-copy mutation trap — callers get independent list objects.
    """
    return {key: [] for key in CONTACT_DOC_TYPES}


def _filter_by_contact(docs: list[Any] | None, contact_id: str) -> list[dict[str, Any]]:
    """Return only the docs belonging to the given contact_id."""
    return [d for d in (docs or []) if isinstance(d, dict) and d.get("contact_id") == contact_id]


def get_xero_data_by_contact(contact_id: str | None, tenant_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Load combined Xero data for a single contact.

    Tries the pre-indexed per-contact file first
    (S3 key: ``{tenant_id}/data/xero_by_contact/{contact_id}.json``,
    written by ``sync.build_per_contact_index``).
    Falls back to loading the full datasets and filtering by contact_id if the
    per-contact file doesn't exist (backward compatibility for tenants that
    synced before per-contact indexing was deployed).

    Args:
        contact_id: Xero contact ID to load data for.
        tenant_id: Optional explicit tenant ID; defaults to the active session tenant.

    Returns:
        Dict with keys ``invoices``, ``credit_notes``, ``payments`` — each a
        list of dicts for the requested contact.
    """
    tenant_id = tenant_id or session.get("xero_tenant_id")
    if not tenant_id or not contact_id:
        return _empty_contact_data()

    # Try the per-contact index file first (fast path).
    per_contact_data = _load_per_contact_file(tenant_id, contact_id)
    if per_contact_data is not None:
        return per_contact_data

    # Fallback: load full datasets and filter by contact_id.  This path
    # loads three full tenant files in the request thread (~150-200 ms),
    # which is significantly slower than the per-contact index (~10-30 ms).
    logger.warning("Per-contact file not found, falling back to full dataset load", tenant_id=tenant_id, contact_id=contact_id)
    return {
        "invoices": _filter_by_contact(load_local_dataset(XeroType.INVOICES, tenant_id=tenant_id), contact_id),
        "credit_notes": _filter_by_contact(load_local_dataset(XeroType.CREDIT_NOTES, tenant_id=tenant_id), contact_id),
        "payments": _filter_by_contact(load_local_dataset(XeroType.PAYMENTS, tenant_id=tenant_id), contact_id),
    }


def _load_per_contact_file(tenant_id: str, contact_id: str) -> dict[str, Any] | None:
    """Load the per-contact JSON file from local cache or S3.

    Returns the parsed dict on success, or None if the file doesn't exist
    anywhere (triggers the fallback to full datasets).
    """
    local_path = os.path.join(LOCAL_DATA_DIR, tenant_id, "xero_by_contact", f"{contact_id}.json")
    local_dir = os.path.dirname(local_path)

    try:
        with open(local_path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        # Not cached locally — try S3.
        s3_key = f"{tenant_id}/data/xero_by_contact/{contact_id}.json"
        try:
            os.makedirs(local_dir, exist_ok=True)
            s3_client.download_file(S3_BUCKET_NAME, s3_key, local_path)
            logger.info("Downloaded per-contact file from S3", tenant_id=tenant_id, contact_id=contact_id)
            with open(local_path, encoding="utf-8") as handle:
                return json.load(handle)
        except s3_client.exceptions.NoSuchKey:
            return None
        except Exception:
            logger.exception("Failed to download per-contact file from S3", tenant_id=tenant_id, contact_id=contact_id, s3_key=s3_key)
            return None
    except json.JSONDecodeError:
        logger.exception("Failed to parse per-contact file", tenant_id=tenant_id, contact_id=contact_id, path=local_path)
        return None
    except Exception:
        logger.exception("Failed to load per-contact file", tenant_id=tenant_id, contact_id=contact_id, path=local_path)
        return None
