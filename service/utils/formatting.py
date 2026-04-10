"""Formatting and numeric helpers.

Provides:
- Separator-agnostic numeric parsing (_normalize_separators, _to_decimal).
- Money formatting (format_money).
- Date and invoice dict formatting utilities (fmt_date, fmt_invoice_data).
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from logger import logger

# region Constants

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")

# endregion

# region Numeric parsing


def _normalize_separators(value: Any) -> str | None:
    """Normalize a raw numeric string to standard dot-decimal format.

    Uses a heuristic based on digit count after the last separator:
    - 2 digits → decimal separator
    - 3+ digits → thousands separator
    - 1 digit → decimal separator
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return str(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = _NON_NUMERIC_RE.sub("", text)
    if not cleaned or cleaned in ("-", ".", "-.", ".-"):
        return None

    # Handle trailing minus (e.g. "126.50-" → "-126.50").
    if cleaned.endswith("-") and not cleaned.startswith("-"):
        cleaned = "-" + cleaned[:-1]

    # Handle parenthetical negatives (e.g. "(126.50)" → "-126.50").
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]

    # Find last separator to determine its role.
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    last_sep_pos = max(last_dot, last_comma)

    if last_sep_pos >= 0:
        digits_after = len(cleaned) - last_sep_pos - 1
        last_sep = cleaned[last_sep_pos]
        other_sep = "," if last_sep == "." else "."

        if digits_after <= 2:
            # Last separator is decimal.
            cleaned = cleaned.replace(other_sep, "")
            if last_sep != ".":
                cleaned = cleaned.replace(last_sep, ".")
        else:
            # Last separator is thousands (3+ digits after).
            cleaned = cleaned.replace(",", "").replace(".", "")
    else:
        cleaned = cleaned.replace(",", "")

    return cleaned


# endregion

# region Public formatting helpers


def _to_decimal(x: Any, **_kwargs: Any) -> Decimal | None:
    """Normalize and parse a value into a Decimal.

    Accepts and ignores legacy separator kwargs for call-site compatibility.
    """
    if x is None or x == "":
        return None
    normalized = _normalize_separators(x)
    if normalized is None:
        if isinstance(x, str) and x.strip():
            logger.warning("Unable to normalize numeric value", raw_value=x)
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        logger.warning("Unable to parse numeric value", raw_value=x, normalized_value=normalized)
        return None


def format_money(x: Any, **_kwargs: Any) -> str:
    """Format a number with thousands separators and 2 decimals.

    Returns empty string for empty input; returns original string if not numeric.
    Accepts and ignores legacy separator kwargs for call-site compatibility.
    """
    d = _to_decimal(x)
    if d is None:
        return "" if x in (None, "") else str(x)
    return f"{d:,.2f}"


def fmt_date(d: Any) -> str | None:
    """Format datetime/date to ISO date string, else None."""
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    return None


def fmt_invoice_data(inv: Any) -> dict[str, Any]:
    """Return a normalized dict of invoice fields for rendering.

    Accepts a Xero SDK Invoice object and extracts the fields used by the
    statement detail view. Uses getattr so this is safe to call on mocked objects.

    Args:
        inv: Xero SDK Invoice or credit note object.

    Returns:
        Dict with keys: invoice_id, number, type, status, date, due_date,
        reference, total, contact_id, contact_name.
    """
    contact = getattr(inv, "contact", None)

    return {
        "invoice_id": getattr(inv, "invoice_id", None),
        "number": getattr(inv, "invoice_number", None),
        "type": getattr(inv, "type", None),
        "status": getattr(inv, "status", None),
        "date": fmt_date(getattr(inv, "date", None)),
        "due_date": fmt_date(getattr(inv, "due_date", None)),
        "reference": getattr(inv, "reference", None),
        "total": getattr(inv, "total", None),
        "contact_id": getattr(contact, "contact_id", None),
        "contact_name": getattr(contact, "name", None),
    }


# endregion
