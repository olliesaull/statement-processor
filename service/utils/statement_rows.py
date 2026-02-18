"""Shared helpers for statement row labels and Xero link lookups.

These helpers are used by both the statement UI row builder and the Excel export path,
so keeping them here avoids duplicating row-level formatting/link logic.
"""

from typing import Any

_ITEM_TYPE_LABELS: dict[str, str] = {"credit_note": "CRN", "invoice": "INV", "payment": "PMT"}


def format_item_type_label(item_type: str | None) -> str:
    """Format a statement item type for display.

    Args:
        item_type: Raw statement item type value.

    Returns:
        Display label for the item type.
    """
    normalized = str(item_type or "").strip().lower()
    if not normalized:
        return ""
    if normalized in _ITEM_TYPE_LABELS:
        return _ITEM_TYPE_LABELS[normalized]
    return normalized.replace("_", " ").upper()


def xero_ids_for_row(item_number_header: str | None, left_row: dict[str, Any], matched_invoice_to_statement_item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return matched Xero invoice/credit note IDs for a row.

    Args:
        item_number_header: Statement header containing the row number reference.
        left_row: Statement-side row values.
        matched_invoice_to_statement_item: Mapping of statement number to Xero match payload.

    Returns:
        Tuple of (xero_invoice_id, xero_credit_note_id). Values are None when unmatched.
    """
    if not item_number_header:
        return None, None
    row_number = str(left_row.get(item_number_header) or "").strip()
    if not row_number:
        return None, None
    match = matched_invoice_to_statement_item.get(row_number)
    if not isinstance(match, dict):
        return None, None
    invoice_payload = match.get("invoice")
    if not isinstance(invoice_payload, dict):
        return None, None
    credit_note_id = invoice_payload.get("credit_note_id")
    xero_credit_note_id = credit_note_id.strip() if isinstance(credit_note_id, str) and credit_note_id.strip() else None
    invoice_id = invoice_payload.get("invoice_id")
    xero_invoice_id = invoice_id.strip() if isinstance(invoice_id, str) and invoice_id.strip() else None
    return xero_invoice_id, xero_credit_note_id
