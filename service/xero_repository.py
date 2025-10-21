"""Module for all Xero data API calls"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from flask import session
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore
from xero_python.exceptions import AccountingBadRequestException

from config import CLIENT_ID, CLIENT_SECRET, logger
from utils import fmt_date, fmt_invoice_data, raise_for_unauthorized

PAGE_SIZE = 100  # Xero max

def get_xero_oauth2_token() -> Optional[dict]:
    """Return the token dict the SDK expects, or None if not set."""
    return session.get("xero_oauth2_token")

def save_xero_oauth2_token(token: dict) -> None:
    """Persist the whole token dict in the session (or your DB)."""
    session["xero_oauth2_token"] = token

api_client = ApiClient(
    Configuration(
        # debug=app.config["DEBUG"],
        oauth2_token=OAuth2Token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET
        ),
    ),
    pool_threads=1,
    oauth2_token_getter=get_xero_oauth2_token,
    oauth2_token_saver=save_xero_oauth2_token,
)
api = AccountingApi(api_client)

def get_contacts() -> List[Dict[str, Any]]:
    """Fetch contacts directly from Xero ordered by name."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        logger.info("Skipping contact lookup; tenant not selected")
        return []

    page = 1
    page_size = 100
    contacts: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    try:
        while True:
            result = api.get_contacts(
                xero_tenant_id=tenant_id,
                page=page,
                include_archived=False,
                page_size=page_size,
            )
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

            if len(batch) < page_size:
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


def get_invoices(*, modified_since: Optional[datetime] = None, statuses: Optional[List[str]] = None, include_archived: bool = False) -> Dict[str, Dict[str, Any]]:
    """Get all invoices from Xero, across all pages.

    Args:
        modified_since: If provided, only invoices modified since this datetime are returned.
        statuses: Optional explicit list of statuses to fetch. Defaults to common non-deleted statuses.
        include_archived: Whether to include archived contacts' invoices (content still filtered by statuses).

    Returns:
        Dict keyed by invoice number with normalized invoice records.
    """
    tenant_id = session["xero_tenant_id"]

    page = 1
    total_returned = 0
    by_number: Dict[str, Dict[str, Any]] = {}

    # Default statuses (excludes DELETED)
    statuses = statuses or ["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"]

    try:
        logger.info(
            "Fetching all invoices (paged)",
            tenant_id=tenant_id,
            page_size=PAGE_SIZE,
            include_archived=include_archived,
            modified_since=str(modified_since) if modified_since else None,
            statuses=statuses,
        )

        while True:
            result = api.get_invoices(
                tenant_id,
                order="UpdatedDateUTC ASC",
                page=page,
                include_archived=include_archived,
                created_by_my_app=False,
                unitdp=2,
                summary_only=False,
                page_size=PAGE_SIZE,
                statuses=statuses,
                modified_since=modified_since,   # safe to pass None
                types=["ACCREC", "ACCPAY"], # NOTE: Removing the one we don't want is more efficient
            )

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


def get_credit_notes(*, modified_since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Get all credit notes across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only credit notes modified since this timestamp.
        page_size: Page size to request (Xero max is 100).

    Returns:
        A list of credit note dicts (same shape as previous per-contact function).
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        return []

    page = 1
    credit_notes: List[Dict[str, Any]] = []

    try:
        logger.info(
            "Fetching all credit notes (paged)",
            tenant_id=tenant_id,
            page_size=PAGE_SIZE,
            modified_since=str(modified_since) if modified_since else None,
        )

        while True:
            result = api.get_credit_notes(
                tenant_id,
                order="UpdatedDateUTC ASC",
                page=page,
                unitdp=2,
                page_size=PAGE_SIZE,
                modified_since=modified_since,
            )
            batch = result.credit_notes or []
            if not batch:
                break

            for note in batch:
                contact = getattr(note, "contact", None)

                total = getattr(note, "total", None)
                remaining_credit = getattr(note, "remaining_credit", None)

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
                        "total": remaining_credit if remaining_credit is not None else total,
                        "amount_credited": getattr(note, "amount_credited", None),
                        "remaining_credit": remaining_credit,
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


def get_payments(*, modified_since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    Get all payments across all pages (no contact filter).

    Args:
        modified_since: Optional datetime to fetch only payments modified since this timestamp.
        page_size: Page size to request (Xero max is 100).
        order: Field to order by for stable pagination.

    Returns:
        A list of payment dicts (same shape as previous per-contact function).
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        return []

    page = 1
    payments: List[Dict[str, Any]] = []

    try:
        logger.info(
            "Fetching all payments (paged)",
            tenant_id=tenant_id,
            page_size=PAGE_SIZE,
            modified_since=str(modified_since) if modified_since else None,
        )

        while True:
            result = api.get_payments(
                tenant_id,
                order="UpdatedDateUTC ASC",
                page=page,
                page_size=PAGE_SIZE,
                modified_since=modified_since,
            )
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


def get_invoices_by_numbers(invoice_numbers: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """
    Fetch invoices for a list of invoice numbers.
    Returns a dict keyed by invoice number: { "INV-001": {...}, ... }

    - Batches requests to avoid URL length limits
    - Handles paging
    - Normalizes invoice numbers to strings and strips whitespace
    - If duplicates arrive from the API, the *last* one wins (simple + predictable)
    """
    tenant_id = session["xero_tenant_id"]
    if not invoice_numbers:
        return {}

    # normalize & de-dupe while preserving order (helps batching)
    normalized = []
    seen = set()
    for n in (str(x).strip() for x in invoice_numbers if str(x).strip()):
        if n not in seen:
            seen.add(n)
            normalized.append(n)

    by_number = {}
    BATCH = 40
    PAGE_SIZE = 100  # Xero cap is 100; fetch full pages to reduce calls
    total_requested = 0

    try:
        logger.info("Fetching invoices by numbers", tenant_id=tenant_id, requested=len(normalized), batch_size=BATCH)
        for i in range(0, len(normalized), BATCH):
            batch = normalized[i:i+BATCH]
            page = 1
            total_requested += len(batch)
            while True:
                # Exclude deleted invoices explicitly via Status filter
                result = api.get_invoices(
                    tenant_id,
                    invoice_numbers=batch,
                    order="InvoiceNumber ASC",
                    page=page,
                    include_archived=False,
                    created_by_my_app=False,
                    unitdp=2,
                    summary_only=False,
                    page_size=PAGE_SIZE,
                    statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"],
                )
                invs = result.invoices or []
                logger.debug("Fetched invoice page", tenant_id=tenant_id, batch=len(batch), page=page, returned=len(invs))
                for inv in invs:
                    rec = fmt_invoice_data(inv)
                    n = rec.get("number")
                    if n:
                        # last one wins if duplicates appear
                        by_number[n] = rec

                if len(invs) < PAGE_SIZE:
                    break
                page += 1

        logger.info("Fetched invoices by numbers", tenant_id=tenant_id, requested=total_requested, returned=len(by_number))
        return by_number

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices by numbers", tenant_id=tenant_id, error=e)
        return {}
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices by numbers", tenant_id=tenant_id, error=e)
        return {}


def get_invoices_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    tenant_id = session["xero_tenant_id"]
    PAGE_SIZE = 100  # Xero cap is 100; fetch full pages to reduce calls

    try:
        logger.info("Fetching invoices for contact", tenant_id=tenant_id, contact_id=contact_id)
        invoices: List[Dict[str, Any]] = []
        page = 1

        while True:
            # Restrict to the contact and exclude deleted invoices
            result = api.get_invoices(
                tenant_id,
                where=f'Contact.ContactID==Guid("{contact_id}") AND Status!="DELETED"',
                order="InvoiceNumber ASC",
                page=page,
                include_archived=False,
                created_by_my_app=False,
                unitdp=2,
                summary_only=False,
                page_size=PAGE_SIZE,
            )

            invs = result.invoices or []
            logger.debug("Fetched invoice contact page", tenant_id=tenant_id, contact_id=contact_id, page=page, returned=len(invs))
            invoices.append(fmt_invoice_data(inv) for inv in invs)

            if len(invs) < PAGE_SIZE:
                break

            page += 1

        logger.info("Fetched invoices for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(invoices))
        return invoices

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices for contact", tenant_id=tenant_id, contact_id=contact_id, error=e)
        return []
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch invoices for contact", tenant_id=tenant_id, contact_id=contact_id, error=e)
        return []


def get_credit_notes_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id or not contact_id:
        return []

    page = 1
    page_size = 100
    credit_notes: List[Dict[str, Any]] = []

    try:
        while True:
            result = api.get_credit_notes(
                tenant_id,
                where=f'Contact.ContactID==Guid("{contact_id}")',
                order="CreditNoteNumber ASC",
                page=page,
                unitdp=2,
                page_size=page_size,
            )
            batch = result.credit_notes or []
            if not batch:
                break

            for note in batch:
                contact = getattr(note, "contact", None)

                total = getattr(note, "total", None)
                remaining_credit = getattr(note, "remaining_credit", None)

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
                        "total": remaining_credit if remaining_credit is not None else total, # NOTE: Confirm this is correct
                        "amount_credited": getattr(note, "amount_credited", None),
                        "remaining_credit": remaining_credit,
                        "contact_id": contact_id,
                        "contact_name": contact_name,
                    }
                )

            logger.debug("Fetched credit note page", tenant_id=tenant_id, contact_id=contact_id, page=page, returned=len(batch))

            if len(batch) < page_size:
                break
            page += 1

        logger.info("Fetched credit notes for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(credit_notes))
        return credit_notes

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch credit notes for contact", tenant_id=tenant_id, contact_id=contact_id, error=e)
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Unexpected error fetching credit notes", tenant_id=tenant_id, contact_id=contact_id, error=e)
    return []


def get_payments_by_contact(contact_id: str) -> List[Dict[str, Any]]:
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id or not contact_id:
        return []

    page = 1
    page_size = 100
    payments: List[Dict[str, Any]] = []

    try:
        while True:
            result = api.get_payments(
                tenant_id,
                where=f'Invoice.Contact.ContactID==Guid("{contact_id}")',
                order="Date DESC",
                page=page,
                page_size=page_size,
            )
            batch = result.payments or []
            if not batch:
                break

            for payment in batch:
                if (invoice_obj := getattr(payment, "invoice", None)):
                    invoice_id = getattr(invoice_obj, "invoice_id", None)
                    contact = getattr(invoice_obj, "contact", None)
                else:
                    invoice_id = contact = None

                payments.append(
                    {
                        "payment_id": getattr(payment, "payment_id", None),
                        "invoice_id": invoice_id,
                        "reference": getattr(payment, "reference", None),
                        "amount": getattr(payment, "amount", None),
                        "date": fmt_date(getattr(payment, "date", None)),
                        "status": getattr(payment, "status", None),
                        "contact_id": getattr(contact, "contact_id", None),
                        "contact_name": getattr(contact, "name", None),
                    }
                )

            logger.debug("Fetched payment page", tenant_id=tenant_id, contact_id=contact_id, page=page, returned=len(batch))

            if len(batch) < page_size:
                break
            page += 1

        logger.info("Fetched payments for contact", tenant_id=tenant_id, contact_id=contact_id, returned=len(payments))
        return payments

    except AccountingBadRequestException as e:
        raise_for_unauthorized(e)
        logger.exception("Failed to fetch payments for contact", tenant_id=tenant_id, contact_id=contact_id, error=e)
    except Exception as e:
        raise_for_unauthorized(e)
        logger.exception("Unexpected error fetching payments", tenant_id=tenant_id, contact_id=contact_id, error=e)
    return []
