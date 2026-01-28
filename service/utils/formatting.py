"""Formatting and numeric helpers."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from logger import logger

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")


def _normalize_separators(value: Any, decimal_separator: str | None = None, thousands_separator: str | None = None) -> str | None:
    """Normalize a raw numeric string using configured separators."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return str(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = _NON_NUMERIC_RE.sub("", text)

    dec = decimal_separator or "."
    thou = thousands_separator if thousands_separator is not None else ","

    if thou and thou != dec:
        cleaned = cleaned.replace(thou, "")

    if dec and dec != ".":
        cleaned = cleaned.replace(dec, ".")

    return cleaned


def _to_decimal(x: Any, *, decimal_separator: str | None = None, thousands_separator: str | None = None) -> Decimal | None:
    """Normalize and parse a value into a Decimal."""
    if x is None or x == "":
        return None
    normalized = _normalize_separators(x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
    if normalized is None:
        if isinstance(x, str) and x.strip():
            logger.warning("Unable to normalize numeric value", raw_value=x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        logger.warning("Unable to parse numeric value", raw_value=x, normalized_value=normalized, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
        return None


def format_money(x: Any, *, decimal_separator: str | None = None, thousands_separator: str | None = None) -> str:
    """Format a number with thousands separators and 2 decimals.

    Returns empty string for empty input; returns original string if not numeric.
    """
    d = _to_decimal(x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
    if d is None:
        return "" if x in (None, "") else str(x)
    return f"{d:,.2f}"


def fmt_date(d: Any) -> str | None:
    """Format datetime/date to ISO date string, else None."""
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    return None


def fmt_invoice_data(inv):
    """Return a normalized dict of invoice fields for rendering."""
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
