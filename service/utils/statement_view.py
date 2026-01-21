"""Statement view helpers for formatting and matching."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from config import logger
from core.date_utils import coerce_datetime_with_template, format_iso_with
from core.models import CellComparison
from utils.formatting import _to_decimal, format_money

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")

_ALLOWED_DECIMAL_SEPARATORS = {".", ","}
_ALLOWED_THOUSANDS_SEPARATORS = {",", ".", " ", "'", ""}
_DEFAULT_DECIMAL_SEPARATOR = "."
_DEFAULT_THOUSANDS_SEPARATOR = ","


def _norm_number(x: Any) -> Decimal | None:
    """Return Decimal if x looks numeric (incl. currency/commas); else None."""
    if x is None:
        return None
    if isinstance(x, (int, float, Decimal)):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return None
    s = str(x).strip()
    if not s:
        return None
    # strip currency symbols/letters, keep digits . , -
    s = _NON_NUMERIC_RE.sub("", s).replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _equal(a: Any, b: Any) -> bool:
    """Numeric-aware equality; otherwise trimmed string equality."""
    da, db = _norm_number(a), _norm_number(b)
    if da is not None or db is not None:
        return da == db
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    return sa.casefold() == sb.casefold()


def _normalize_header_name(value: Any) -> str:
    """Normalize a header label for matching."""
    return " ".join(str(value or "").split()).strip().lower()


def _header_mapping_from_template(items_template: dict[str, Any]) -> dict[str, str]:
    """Build normalized header -> canonical field mappings from config."""
    header_to_field_norm: dict[str, str] = {}
    for canonical_field, mapped in (items_template or {}).items():
        if canonical_field in {"raw", "date_format", "item_type", "reference"}:
            continue
        if isinstance(mapped, str) and mapped.strip():
            header_to_field_norm[_normalize_header_name(mapped)] = canonical_field

    mapped_total = (items_template or {}).get("total")
    if isinstance(mapped_total, list) and mapped_total:
        for header in mapped_total:
            if isinstance(header, str) and header.strip():
                header_to_field_norm[_normalize_header_name(header)] = "total"
    return header_to_field_norm


def _filter_display_headers(
    raw_headers: list[str],
    header_to_field_norm: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    """Filter raw headers to mapped headers and return header->field mapping."""
    header_to_field: dict[str, str] = {}
    display_headers: list[str] = []
    for header in raw_headers:
        canon = header_to_field_norm.get(_normalize_header_name(header))
        if not canon:
            continue
        header_to_field[header] = canon
        display_headers.append(header)
    return display_headers, header_to_field


def _order_display_headers(display_headers: list[str], header_to_field: dict[str, str]) -> list[str]:
    """Order display headers with preferred fields first."""
    preferred_field_order = ["date", "due_date", "number", "total"]
    ordered_headers: list[str] = []
    for canonical_field in preferred_field_order:
        header_match = next(
            (hdr for hdr in display_headers if header_to_field.get(hdr) == canonical_field),
            None,
        )
        if header_match:
            ordered_headers.append(header_match)
    for header in display_headers:
        if header not in ordered_headers:
            ordered_headers.append(header)
    return ordered_headers


def _format_statement_value(
    value: Any,
    canonical_field: str | None,
    date_fmt: str | None,
    dec_sep: str,
    thou_sep: str,
) -> Any:
    """Normalize a statement cell value based on the canonical field."""
    if canonical_field in {"date", "due_date"}:
        dt = coerce_datetime_with_template(value, date_fmt)
        if dt is not None:
            return format_iso_with(dt, date_fmt) if date_fmt else dt.strftime("%Y-%m-%d")
    elif canonical_field == "total":
        return format_money(value, decimal_separator=dec_sep, thousands_separator=thou_sep)
    return value


def _build_rows_by_header(
    items: list[dict],
    display_headers: list[str],
    header_to_field: dict[str, str],
    date_fmt: str | None,
    dec_sep: str,
    thou_sep: str,
) -> list[dict[str, str]]:
    """Build normalized row dicts for the display headers."""
    rows_by_header: list[dict[str, str]] = []
    for item in items:
        raw = item.get("raw", {}) if isinstance(item, dict) else {}
        row: dict[str, str] = {}
        for header in display_headers:
            value = raw.get(header, "")
            canon = header_to_field.get(header)
            row[header] = _format_statement_value(value, canon, date_fmt, dec_sep, thou_sep)
        rows_by_header.append(row)
    return rows_by_header


def _find_item_number_header(display_headers: list[str], header_to_field: dict[str, str]) -> str | None:
    """Return the header mapped to the canonical number field, if any."""
    for header in display_headers:
        if header_to_field.get(header) == "number":
            return header
    return None


def get_items_template_from_config(contact_config: dict[str, Any]) -> dict[str, Any]:
    """
    Return the items template mapping from a contact config.

    Supports three shapes for backward/forward compatibility:
      - Legacy:   contact_config["statement_items"] is a 1-item list of dict.
      - Nested:   contact_config["statement_items"] is a dict.
      - Flattened: the template keys live directly at the root of contact_config.
    """
    if not isinstance(contact_config, dict):
        return {}

    cfg = contact_config.get("statement_items")
    if isinstance(cfg, dict):
        return cfg
    if isinstance(cfg, list) and cfg:
        first = cfg[0]
        return first if isinstance(first, dict) else {}

    # Flattened/root form: assume the root dict itself is the template mapping
    return contact_config


def get_date_format_from_config(contact_config: dict[str, Any]) -> str | None:
    """Extract the configured date format from a contact configuration."""
    if not isinstance(contact_config, dict):
        return None

    fmt = contact_config.get("date_format")
    return str(fmt) if fmt else None


def get_number_separators_from_config(
    contact_config: dict[str, Any],
) -> tuple[str, str]:
    """Return (decimal_separator, thousands_separator) with sensible defaults."""
    if not isinstance(contact_config, dict):
        return _DEFAULT_DECIMAL_SEPARATOR, _DEFAULT_THOUSANDS_SEPARATOR

    dec_raw = contact_config.get("decimal_separator")
    thou_raw = contact_config.get("thousands_separator")

    dec = str(dec_raw).strip() if isinstance(dec_raw, str) else dec_raw
    thou = str(thou_raw) if isinstance(thou_raw, str) else thou_raw

    if dec not in _ALLOWED_DECIMAL_SEPARATORS:
        dec = _DEFAULT_DECIMAL_SEPARATOR
    if thou not in _ALLOWED_THOUSANDS_SEPARATORS:
        thou = _DEFAULT_THOUSANDS_SEPARATOR

    return (
        dec or _DEFAULT_DECIMAL_SEPARATOR,
        thou if thou is not None else _DEFAULT_THOUSANDS_SEPARATOR,
    )


def prepare_display_mappings(items: list[dict], contact_config: dict[str, Any]) -> tuple[list[str], list[dict[str, str]], dict[str, str], str | None]:
    """
    Build the display headers, filtered left rows, header->invoice_field map,
    and detect which header corresponds to the invoice "number".

    Returns: (display_headers, rows_by_header, header_to_field, item_number_header)
    """
    # Derive raw headers from the JSON statement (order preserved)
    raw_headers = list(items[0].get("raw", {}).keys()) if items else []

    items_template = get_items_template_from_config(contact_config)
    header_to_field_norm = _header_mapping_from_template(items_template)
    display_headers, header_to_field = _filter_display_headers(raw_headers, header_to_field_norm)
    display_headers = _order_display_headers(display_headers, header_to_field)

    # Convert raw rows into dicts filtered by display headers, normalizing date fields for display
    date_fmt = get_date_format_from_config(contact_config)
    dec_sep, thou_sep = get_number_separators_from_config(contact_config)
    rows_by_header = _build_rows_by_header(items, display_headers, header_to_field, date_fmt, dec_sep, thou_sep)

    # Identify which header maps to the canonical "number" field
    item_number_header = _find_item_number_header(display_headers, header_to_field)

    return display_headers, rows_by_header, header_to_field, item_number_header


def match_invoices_to_statement_items(
    items: list[dict],
    rows_by_header: list[dict[str, str]],
    item_number_header: str | None,
    invoices: list[dict],
) -> dict[str, dict]:
    """
    Build mapping from statement invoice number -> { invoice, statement_item, match_type, match_score, matched_invoice_number }.

    Strategy:
      1) Exact string match on the displayed value.
      2) Substring match on a normalized form (alphanumeric only, case-insensitive),
         e.g. "Invoice # INV-12345" contains "INV12345".
      No generic fuzzy similarity to avoid near-number false positives.
    """
    matched: dict[str, dict] = {}
    if not item_number_header:
        return matched

    stmt_by_number = _statement_items_by_number(items, item_number_header)
    matched, used_invoice_ids, used_invoice_numbers = _record_exact_matches(stmt_by_number, invoices)
    candidates = _candidate_invoices(invoices, used_invoice_ids, used_invoice_numbers)
    missing = _missing_statement_numbers(rows_by_header, item_number_header, matched)

    for key in missing:
        stmt_item = stmt_by_number.get(key)
        if stmt_item is None:
            continue
        if _is_payment_reference(key):
            logger.info("Skipping substring match due to payment keywords", statement_number=key)
            continue

        target_norm = _normalize_invoice_number(key)
        hits = _candidate_hits(target_norm, candidates, used_invoice_ids, used_invoice_numbers)
        if hits:
            inv_no_best, inv_obj, _ = max(hits, key=lambda item: item[2])
            _record_substring_match(matched, key, stmt_item, inv_no_best, inv_obj)
            _mark_invoice_used(inv_obj, inv_no_best, used_invoice_ids, used_invoice_numbers)
        else:
            logger.info("No match for statement number", statement_number=key)

    return matched


def _normalize_invoice_number(value: Any) -> str:
    """Normalize invoice numbers for matching."""
    return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum())


def _statement_items_by_number(items: list[dict], item_number_header: str) -> dict[str, dict]:
    """Build lookup of statement items keyed by their displayed invoice number."""
    stmt_by_number: dict[str, dict] = {}
    for item in items:
        raw = item.get("raw", {}) if isinstance(item, dict) else {}
        number = raw.get(item_number_header, "")
        if not number:
            continue
        key = str(number).strip()
        if key:
            stmt_by_number[key] = item
    return stmt_by_number


def _record_exact_matches(
    stmt_by_number: dict[str, dict],
    invoices: list[dict],
) -> tuple[dict[str, dict], set, set]:
    """Record exact matches and return updated match state."""
    matched: dict[str, dict] = {}
    used_invoice_ids: set = set()
    used_invoice_numbers: set = set()
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        key = str(inv_no).strip()
        if not key:
            continue
        stmt_item = stmt_by_number.get(key)
        if stmt_item is not None and key not in matched:
            matched[key] = {
                "invoice": inv,
                "statement_item": stmt_item,
                "match_type": "exact",
                "match_score": 1.0,
                "matched_invoice_number": key,
            }
            logger.info(
                "Exact match",
                statement_number=key,
                invoice_number=key,
                statement_item=stmt_item,
                xero_item=inv,
            )
            inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
            if inv_id:
                used_invoice_ids.add(inv_id)
            used_invoice_numbers.add(key)
    return matched, used_invoice_ids, used_invoice_numbers


def _candidate_invoices(
    invoices: list[dict],
    used_invoice_ids: set,
    used_invoice_numbers: set,
) -> list[tuple[str, dict, str]]:
    """Collect candidate invoices for substring matching."""
    candidates: list[tuple[str, dict, str]] = []
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        inv_no_str = str(inv_no).strip()
        if not inv_no_str:
            continue
        if (inv.get("invoice_id") if isinstance(inv, dict) else None) in used_invoice_ids:
            continue
        if inv_no_str in used_invoice_numbers:
            continue
        candidates.append((inv_no_str, inv, _normalize_invoice_number(inv_no_str)))
    return candidates


def _missing_statement_numbers(
    rows_by_header: list[dict[str, str]],
    item_number_header: str,
    matched: dict[str, dict],
) -> list[str]:
    """Return missing statement numbers needing substring matching."""
    numbers_in_rows = [(r.get(item_number_header) or "").strip() for r in rows_by_header if r.get(item_number_header)]
    return [number for number in numbers_in_rows if number and number not in matched]


def _is_payment_reference(value: str) -> bool:
    """Return True when the text clearly references a payment."""
    payment_keywords = ("payment", "paid", "remittance", "receipt")
    lowered = str(value).casefold()
    return any(keyword in lowered for keyword in payment_keywords)


def _candidate_hits(
    target_norm: str,
    candidates: list[tuple[str, dict, str]],
    used_invoice_ids: set,
    used_invoice_numbers: set,
) -> list[tuple[str, dict, int]]:
    """Collect candidate hits for a target invoice number."""
    hits: list[tuple[str, dict, int]] = []
    for cand_no, inv, cand_norm in candidates:
        inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
        if inv_id in used_invoice_ids or cand_no in used_invoice_numbers:
            continue
        if not target_norm or not cand_norm:
            continue
        if cand_norm == target_norm or cand_norm in target_norm or target_norm in cand_norm:
            hits.append((cand_no, inv, len(cand_norm)))
    return hits


def _record_substring_match(
    matched: dict[str, dict],
    statement_number: str,
    statement_item: dict,
    invoice_number: str,
    invoice_obj: dict,
) -> None:
    """Record a substring match and emit logging."""
    matched[statement_number] = {
        "invoice": invoice_obj,
        "statement_item": statement_item,
        "match_type": "substring" if invoice_number != statement_number else "exact",
        "match_score": 1.0,
        "matched_invoice_number": invoice_number,
    }
    kind = "Exact" if invoice_number == statement_number else "Substring"
    logger.info(
        "Statement match",
        match_type=kind,
        statement_number=statement_number,
        invoice_number=invoice_number,
        statement_item=statement_item,
        xero_item=invoice_obj,
    )


def _mark_invoice_used(
    invoice_obj: dict,
    invoice_number: str,
    used_invoice_ids: set,
    used_invoice_numbers: set,
) -> None:
    """Mark an invoice as used to avoid reuse in later matching."""
    inv_id = invoice_obj.get("invoice_id") if isinstance(invoice_obj, dict) else None
    if inv_id:
        used_invoice_ids.add(inv_id)
    used_invoice_numbers.add(invoice_number)


def build_right_rows(
    rows_by_header: list[dict[str, str]],
    display_headers: list[str],
    header_to_field: dict[str, str],
    matched_map: dict[str, dict],
    item_number_header: str | None,
    date_format: str | None = None,
    decimal_separator: str | None = None,
    thousands_separator: str | None = None,
) -> list[dict[str, str]]:
    """
    Using the matched map, build the right-hand table rows with values from
    the invoice, aligned to the same display headers and row order as the left.
    """
    right_rows = []
    numeric_fields = {"total"}

    for r in rows_by_header:
        inv_no = (r.get(item_number_header) or "").strip() if item_number_header else ""
        rec = matched_map.get(inv_no, {}) or {}
        inv = rec.get("invoice", {}) if isinstance(rec, dict) else {}

        inv_total = inv.get("total")

        row_right = {}
        for h in display_headers:
            invoice_field = header_to_field.get(h)
            if not invoice_field:
                row_right[h] = ""
                continue

            if invoice_field == "total":
                # Only populate the headers that have a value on the statement side
                left_val = r.get(h)
                if left_val is not None and str(left_val).strip():
                    left_dec = _to_decimal(
                        left_val,
                        decimal_separator=decimal_separator,
                        thousands_separator=thousands_separator,
                    )
                    if left_dec is not None and left_dec == Decimal(0):
                        row_right[h] = format_money(0)
                    else:
                        row_right[h] = format_money(inv_total) if inv_total is not None else ""
                else:
                    row_right[h] = ""
            elif invoice_field in {"due_date", "date"}:
                v = inv.get(invoice_field)
                if v is None:
                    row_right[h] = ""
                else:
                    fmt = date_format or "YYYY-MM-DD"
                    row_right[h] = format_iso_with(v, fmt)
            else:
                val = inv.get(invoice_field, "")
                if invoice_field in numeric_fields:
                    row_right[h] = format_money(val)
                else:
                    row_right[h] = val

        right_rows.append(row_right)

    return right_rows


def build_row_comparisons(
    left_rows: list[dict[str, str]],
    right_rows: list[dict[str, str]],
    display_headers: list[str],
    header_to_field: dict[str, str] | None = None,
) -> list[list[CellComparison]]:
    """
    Build per-cell comparison objects for each row.
    """
    comparisons: list[list[CellComparison]] = []
    for left, right in zip(left_rows, right_rows, strict=False):
        row_cells: list[CellComparison] = []
        for header in display_headers:
            left_val = left.get(header, "") if isinstance(left, dict) else ""
            right_val = right.get(header, "") if isinstance(right, dict) else ""
            # For the canonical invoice number column, treat values as IDs and
            # consider them matching if one normalized string contains the other.
            if header_to_field and header_to_field.get(header) == "number":

                def _norm_id_text(x: Any) -> str:
                    s = "" if x is None else str(x).strip()
                    return "".join(ch for ch in s.upper() if ch.isalnum())

                a, b = _norm_id_text(left_val), _norm_id_text(right_val)
                matches = bool(a and b and (a == b or a in b or b in a))
            else:
                matches = _equal(left_val, right_val)
            canonical = (header_to_field or {}).get(header)
            row_cells.append(
                CellComparison(
                    header=header,
                    statement_value="" if left_val is None else str(left_val),
                    xero_value="" if right_val is None else str(right_val),
                    matches=matches,
                    canonical_field=canonical,
                )
            )
        comparisons.append(row_cells)
    return comparisons
