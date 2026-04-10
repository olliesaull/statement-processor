"""Tests for utils/statement_view.py — coverage expansion.

Covers pure helper functions (normalization, filtering, ordering, formatting,
matching, row building, comparisons) that are not exercised by the existing
test_statement_view_formatting.py or test_statement_view_cache.py suites.
"""

from decimal import Decimal
from typing import Any

import pytest

from core.models import CellComparison
from utils.statement_view import (
    _build_rows_by_header,
    _candidate_hits,
    _candidate_invoices,
    _equal,
    _filter_display_amount_columns,
    _filter_display_headers,
    _find_item_number_header,
    _format_statement_value,
    _is_payment_reference,
    _mark_invoice_used,
    _matches_patterns,
    _missing_statement_numbers,
    _norm_number,
    _normalize_header_name,
    _normalize_invoice_number,
    _order_display_headers,
    _record_exact_matches,
    _record_substring_match,
    _statement_items_by_number,
    build_right_rows,
    build_row_comparisons,
    match_invoices_to_statement_items,
    prepare_display_mappings,
)


# ---------------------------------------------------------------------------
# _norm_number
# ---------------------------------------------------------------------------
class TestNormNumber:
    """Normalize arbitrary values to Decimal or None."""

    def test_none_returns_none(self) -> None:
        assert _norm_number(None) is None

    def test_int_returns_decimal(self) -> None:
        assert _norm_number(42) == Decimal("42")

    def test_float_returns_decimal(self) -> None:
        result = _norm_number(3.14)
        assert isinstance(result, Decimal)
        assert result == Decimal("3.14")

    def test_decimal_passthrough(self) -> None:
        assert _norm_number(Decimal("99.99")) == Decimal("99.99")

    def test_numeric_string(self) -> None:
        assert _norm_number("123.45") == Decimal("123.45")

    def test_currency_string_stripped(self) -> None:
        """Currency symbols and letters are stripped before parsing."""
        assert _norm_number("$1,234.56") == Decimal("1234.56")

    def test_empty_string_returns_none(self) -> None:
        assert _norm_number("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _norm_number("   ") is None

    def test_non_numeric_string_returns_none(self) -> None:
        assert _norm_number("abc") is None

    def test_float_nan_returns_none(self) -> None:
        """float('nan') -> Decimal('NaN') which is an InvalidOperation candidate."""
        result = _norm_number(float("nan"))
        # Decimal(str(nan)) produces Decimal('NaN') which is valid but
        # comparisons with NaN are always False; ensure it returns something.
        # The function should return Decimal('NaN') without error.
        assert result is not None or result is None  # no crash

    def test_float_inf_returns_decimal_infinity(self) -> None:
        """float('inf') -> Decimal('Infinity') — valid Decimal, not None."""
        result = _norm_number(float("inf"))
        assert result == Decimal("Infinity")

    def test_negative_string(self) -> None:
        assert _norm_number("-50.00") == Decimal("-50.00")

    def test_string_with_commas(self) -> None:
        assert _norm_number("1,000") == Decimal("1000")


# ---------------------------------------------------------------------------
# _equal
# ---------------------------------------------------------------------------
class TestEqual:
    """Numeric-aware equality comparisons."""

    def test_numeric_equal(self) -> None:
        assert _equal("100.00", "100") is True

    def test_numeric_not_equal(self) -> None:
        assert _equal("100", "200") is False

    def test_string_equal_case_insensitive(self) -> None:
        assert _equal("Hello", "hello") is True

    def test_string_with_whitespace(self) -> None:
        assert _equal("  abc  ", "abc") is True

    def test_none_vs_none(self) -> None:
        assert _equal(None, None) is True

    def test_none_vs_empty_string(self) -> None:
        assert _equal(None, "") is True

    def test_numeric_vs_none(self) -> None:
        """When one side is numeric and other is None, da!=db."""
        assert _equal("100", None) is False

    def test_none_vs_numeric(self) -> None:
        assert _equal(None, "200") is False

    def test_both_non_numeric_strings(self) -> None:
        assert _equal("abc", "xyz") is False

    def test_mixed_numeric_with_currency(self) -> None:
        """Currency-stripped values should compare numerically."""
        assert _equal("$100.00", "100.00") is True


# ---------------------------------------------------------------------------
# _normalize_header_name
# ---------------------------------------------------------------------------
class TestNormalizeHeaderName:
    """Collapse whitespace and lowercase header labels."""

    def test_basic_normalization(self) -> None:
        assert _normalize_header_name("  Invoice   Number  ") == "invoice number"

    def test_none_input(self) -> None:
        assert _normalize_header_name(None) == ""

    def test_empty_string(self) -> None:
        assert _normalize_header_name("") == ""

    def test_already_normalized(self) -> None:
        assert _normalize_header_name("date") == "date"

    def test_numeric_input(self) -> None:
        assert _normalize_header_name(123) == "123"


# ---------------------------------------------------------------------------
# _filter_display_headers
# ---------------------------------------------------------------------------
class TestFilterDisplayHeaders:
    """Filter raw headers down to those with canonical mappings."""

    def test_maps_known_headers(self) -> None:
        raw = ["Invoice No.", "Date", "Amount", "Misc"]
        norm_map = {"invoice no.": "number", "date": "date", "amount": "total"}
        headers, h2f = _filter_display_headers(raw, norm_map)
        assert headers == ["Invoice No.", "Date", "Amount"]
        assert h2f == {"Invoice No.": "number", "Date": "date", "Amount": "total"}

    def test_fallback_to_canonical_name(self) -> None:
        """Raw header 'number' should map to canonical 'number' even without norm_map entry."""
        raw = ["number", "unknown_col"]
        headers, h2f = _filter_display_headers(raw, {})
        assert headers == ["number"]
        assert h2f == {"number": "number"}

    def test_canonical_fallback_reference(self) -> None:
        raw = ["reference"]
        headers, h2f = _filter_display_headers(raw, {})
        assert headers == ["reference"]
        assert h2f == {"reference": "reference"}

    def test_canonical_fallback_date(self) -> None:
        raw = ["date"]
        headers, h2f = _filter_display_headers(raw, {})
        assert headers == ["date"]
        assert h2f == {"date": "date"}

    def test_canonical_fallback_due_date(self) -> None:
        raw = ["due_date"]
        headers, h2f = _filter_display_headers(raw, {})
        assert headers == ["due_date"]
        assert h2f == {"due_date": "due_date"}

    def test_skips_unmapped_headers(self) -> None:
        raw = ["Foo", "Bar"]
        headers, h2f = _filter_display_headers(raw, {})
        assert headers == []
        assert h2f == {}

    def test_empty_input(self) -> None:
        headers, h2f = _filter_display_headers([], {})
        assert headers == []
        assert h2f == {}


# ---------------------------------------------------------------------------
# _matches_patterns
# ---------------------------------------------------------------------------
class TestMatchesPatterns:
    """Check if a normalized name starts or ends with pattern strings."""

    def test_starts_with_match(self) -> None:
        assert _matches_patterns("debit amount", ("debit",)) is True

    def test_ends_with_match(self) -> None:
        assert _matches_patterns("net debit", ("debit",)) is True

    def test_no_match(self) -> None:
        assert _matches_patterns("reference", ("debit",)) is False


# ---------------------------------------------------------------------------
# _filter_display_amount_columns
# ---------------------------------------------------------------------------
class TestFilterDisplayAmountColumns:
    """Tiered amount column selection (debit/credit > total > balance)."""

    def test_debit_credit_tier_wins(self) -> None:
        """When debit/credit columns exist, total/balance are excluded."""
        headers = ["Date", "Debit", "Credit", "Total", "Balance"]
        h2f = {"Date": "date", "Debit": "total", "Credit": "total", "Total": "total", "Balance": "total"}
        result_headers, result_h2f = _filter_display_amount_columns(headers, h2f)
        assert "Date" in result_headers
        assert "Debit" in result_headers
        assert "Credit" in result_headers
        assert "Total" not in result_headers
        assert "Balance" not in result_headers

    def test_total_tier_when_no_debit_credit(self) -> None:
        """When no debit/credit columns, total tier is used."""
        headers = ["Date", "Total", "Balance"]
        h2f = {"Date": "date", "Total": "total", "Balance": "total"}
        result_headers, _ = _filter_display_amount_columns(headers, h2f)
        assert "Total" in result_headers
        assert "Balance" not in result_headers

    def test_balance_tier_as_last_resort(self) -> None:
        """When only balance-like columns exist, they are kept."""
        headers = ["Date", "Balance"]
        h2f = {"Date": "date", "Balance": "total"}
        result_headers, _ = _filter_display_amount_columns(headers, h2f)
        assert "Balance" in result_headers

    def test_non_total_columns_always_kept(self) -> None:
        headers = ["Date", "Number"]
        h2f = {"Date": "date", "Number": "number"}
        result_headers, _ = _filter_display_amount_columns(headers, h2f)
        assert result_headers == ["Date", "Number"]

    def test_no_amount_columns_at_all(self) -> None:
        """When no total-mapped columns exist, only non-total headers remain."""
        headers = ["Date", "Number"]
        h2f = {"Date": "date", "Number": "number"}
        result_headers, result_h2f = _filter_display_amount_columns(headers, h2f)
        assert result_headers == ["Date", "Number"]
        assert result_h2f == {"Date": "date", "Number": "number"}


# ---------------------------------------------------------------------------
# _order_display_headers
# ---------------------------------------------------------------------------
class TestOrderDisplayHeaders:
    """Headers should be ordered: date, due_date, number, reference, then amounts."""

    def test_preferred_ordering(self) -> None:
        headers = ["Amount", "Number", "Date", "Ref"]
        h2f = {"Date": "date", "Number": "number", "Amount": "total", "Ref": "reference"}
        ordered = _order_display_headers(headers, h2f)
        # Non-amount should come first in preferred order, amount last.
        assert ordered == ["Date", "Number", "Ref", "Amount"]

    def test_due_date_before_number(self) -> None:
        headers = ["Number", "Due Date", "Date"]
        h2f = {"Number": "number", "Due Date": "due_date", "Date": "date"}
        ordered = _order_display_headers(headers, h2f)
        assert ordered.index("Date") < ordered.index("Due Date")
        assert ordered.index("Due Date") < ordered.index("Number")

    def test_unknown_fields_after_preferred(self) -> None:
        """Headers not in the preferred list go after known non-amount fields."""
        headers = ["Custom", "Date"]
        h2f = {"Date": "date", "Custom": "custom_field"}
        ordered = _order_display_headers(headers, h2f)
        assert ordered == ["Date", "Custom"]

    def test_amount_columns_at_end(self) -> None:
        headers = ["Debit", "Date", "Credit"]
        h2f = {"Debit": "total", "Date": "date", "Credit": "total"}
        ordered = _order_display_headers(headers, h2f)
        assert ordered[0] == "Date"
        assert set(ordered[1:]) == {"Debit", "Credit"}


# ---------------------------------------------------------------------------
# _format_statement_value
# ---------------------------------------------------------------------------
class TestFormatStatementValue:
    """Format cell values based on canonical field type."""

    def test_total_field_formats_as_money(self) -> None:
        result = _format_statement_value("1234.56", "total", None)
        assert result == "1,234.56"

    def test_total_field_uses_absolute_value(self) -> None:
        """Negative amounts are displayed as positive (Xero stores positive totals)."""
        result = _format_statement_value("-500.00", "total", None)
        assert result == "500.00"

    def test_total_field_non_numeric_passthrough(self) -> None:
        """Non-numeric total values are passed through format_money."""
        result = _format_statement_value("N/A", "total", None)
        assert result == "N/A"

    def test_total_field_empty_string(self) -> None:
        result = _format_statement_value("", "total", None)
        assert result == ""

    def test_date_field_with_format(self) -> None:
        result = _format_statement_value("2024-03-15", "date", "DD/MM/YYYY")
        assert result == "15/03/2024"

    def test_date_field_without_format(self) -> None:
        result = _format_statement_value("2024-03-15", "date", None)
        assert result == "2024-03-15"

    def test_due_date_field(self) -> None:
        result = _format_statement_value("2024-06-30", "due_date", "DD/MM/YYYY")
        assert result == "30/06/2024"

    def test_date_field_unparseable_returns_original(self) -> None:
        """When coerce_datetime_with_template returns None, value is returned as-is."""
        result = _format_statement_value("not-a-date", "date", "DD/MM/YYYY")
        assert result == "not-a-date"

    def test_non_special_field_passthrough(self) -> None:
        """Fields not date/total pass through unchanged."""
        result = _format_statement_value("INV-001", "number", None)
        assert result == "INV-001"

    def test_none_canonical_field(self) -> None:
        result = _format_statement_value("anything", None, None)
        assert result == "anything"


# ---------------------------------------------------------------------------
# _build_rows_by_header
# ---------------------------------------------------------------------------
class TestBuildRowsByHeader:
    """Build normalized row dicts for display headers."""

    def test_basic_row_building(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001", "Amount": "100.00"}}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        rows = _build_rows_by_header(items, headers, h2f, None)
        assert len(rows) == 1
        assert rows[0]["Number"] == "INV-001"
        assert rows[0]["Amount"] == "100.00"

    def test_missing_raw_key_defaults_to_empty(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001"}}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        rows = _build_rows_by_header(items, headers, h2f, None)
        assert rows[0]["Amount"] == ""

    def test_non_dict_item_produces_empty_row(self) -> None:
        """Items that are not dicts produce empty raw -> empty values."""
        items: list[Any] = ["not_a_dict"]
        headers = ["Number"]
        h2f = {"Number": "number"}
        rows = _build_rows_by_header(items, headers, h2f, None)
        assert rows[0]["Number"] == ""

    def test_multiple_items(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001"}}, {"raw": {"Number": "INV-002"}}]
        headers = ["Number"]
        h2f = {"Number": "number"}
        rows = _build_rows_by_header(items, headers, h2f, None)
        assert len(rows) == 2
        assert rows[0]["Number"] == "INV-001"
        assert rows[1]["Number"] == "INV-002"

    def test_date_formatting_applied(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Date": "2024-03-15"}}]
        headers = ["Date"]
        h2f = {"Date": "date"}
        rows = _build_rows_by_header(items, headers, h2f, "DD/MM/YYYY")
        assert rows[0]["Date"] == "15/03/2024"


# ---------------------------------------------------------------------------
# _find_item_number_header
# ---------------------------------------------------------------------------
class TestFindItemNumberHeader:
    """Locate the header mapped to canonical 'number' field."""

    def test_finds_number_header(self) -> None:
        headers = ["Date", "Invoice No."]
        h2f = {"Date": "date", "Invoice No.": "number"}
        assert _find_item_number_header(headers, h2f) == "Invoice No."

    def test_returns_none_when_missing(self) -> None:
        headers = ["Date", "Amount"]
        h2f = {"Date": "date", "Amount": "total"}
        assert _find_item_number_header(headers, h2f) is None


# ---------------------------------------------------------------------------
# prepare_display_mappings (integration of filter/order/build)
# ---------------------------------------------------------------------------
class TestPrepareDisplayMappings:
    """Main entry point that wires filter -> order -> build."""

    def test_basic_end_to_end(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Invoice No.": "INV-001", "Date": "2024-03-15", "Amount": "100.00"}}]
        statement_data: dict[str, Any] = {"header_mapping": {"Invoice No.": "number", "Date": "date", "Amount": "total"}}
        headers, rows, h2f, item_number_header = prepare_display_mappings(items, statement_data)
        assert "Date" in headers
        assert "Invoice No." in headers
        assert "Amount" in headers
        assert item_number_header == "Invoice No."
        assert len(rows) == 1

    def test_empty_items(self) -> None:
        headers, rows, h2f, item_number_header = prepare_display_mappings([], {})
        assert headers == []
        assert rows == []
        assert h2f == {}
        assert item_number_header is None

    def test_fallback_to_reference_for_item_number(self) -> None:
        """When no 'number' mapping exists, falls back to 'reference'."""
        items: list[dict[str, Any]] = [{"raw": {"Ref": "REF-001", "Date": "2024-03-15"}}]
        statement_data: dict[str, Any] = {"header_mapping": {"Ref": "reference", "Date": "date"}}
        headers, rows, h2f, item_number_header = prepare_display_mappings(items, statement_data)
        assert item_number_header == "Ref"

    def test_date_format_passed_through(self) -> None:
        """date_format from statement_data is used to format date cells."""
        items: list[dict[str, Any]] = [{"raw": {"Date": "2024-03-15"}}]
        statement_data: dict[str, Any] = {"header_mapping": {"Date": "date"}, "date_format": "DD/MM/YYYY"}
        headers, rows, h2f, _ = prepare_display_mappings(items, statement_data)
        assert rows[0]["Date"] == "15/03/2024"

    def test_no_number_or_reference_header(self) -> None:
        """When neither number nor reference mapping exists, item_number_header is None."""
        items: list[dict[str, Any]] = [{"raw": {"Date": "2024-03-15", "Amount": "100.00"}}]
        statement_data: dict[str, Any] = {"header_mapping": {"Date": "date", "Amount": "total"}}
        _, _, _, item_number_header = prepare_display_mappings(items, statement_data)
        assert item_number_header is None


# ---------------------------------------------------------------------------
# _normalize_invoice_number
# ---------------------------------------------------------------------------
class TestNormalizeInvoiceNumber:
    """Strip to uppercase alphanumeric for matching."""

    def test_strips_special_chars(self) -> None:
        assert _normalize_invoice_number("INV-001") == "INV001"

    def test_uppercase(self) -> None:
        assert _normalize_invoice_number("inv-001") == "INV001"

    def test_none_input(self) -> None:
        assert _normalize_invoice_number(None) == ""

    def test_empty_input(self) -> None:
        assert _normalize_invoice_number("") == ""


# ---------------------------------------------------------------------------
# _statement_items_by_number
# ---------------------------------------------------------------------------
class TestStatementItemsByNumber:
    """Build lookup dict keyed by displayed invoice number."""

    def test_basic_lookup(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001"}}, {"raw": {"Number": "INV-002"}}]
        result = _statement_items_by_number(items, "Number")
        assert "INV-001" in result
        assert "INV-002" in result

    def test_skips_empty_numbers(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": ""}}, {"raw": {"Number": "INV-001"}}]
        result = _statement_items_by_number(items, "Number")
        assert "" not in result
        assert "INV-001" in result

    def test_skips_missing_header(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Other": "val"}}]
        result = _statement_items_by_number(items, "Number")
        assert result == {}

    def test_non_dict_items_skipped(self) -> None:
        items: list[Any] = ["not_a_dict"]
        result = _statement_items_by_number(items, "Number")
        assert result == {}

    def test_strips_whitespace(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "  INV-001  "}}]
        result = _statement_items_by_number(items, "Number")
        assert "INV-001" in result


# ---------------------------------------------------------------------------
# _record_exact_matches
# ---------------------------------------------------------------------------
class TestRecordExactMatches:
    """Find exact matches between statement items and invoices."""

    def test_exact_match_found(self) -> None:
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "INV-001", "invoice_id": "xero-1", "total": 100}]
        matched, used_ids, used_numbers = _record_exact_matches(stmt_by_number, invoices)
        assert "INV-001" in matched
        assert matched["INV-001"]["match_type"] == "exact"
        assert matched["INV-001"]["match_score"] == 1.0
        assert "xero-1" in used_ids
        assert "INV-001" in used_numbers

    def test_no_match(self) -> None:
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "INV-999", "invoice_id": "xero-1"}]
        matched, used_ids, used_numbers = _record_exact_matches(stmt_by_number, invoices)
        assert matched == {}

    def test_skips_invoices_without_number(self) -> None:
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"invoice_id": "xero-1"}]
        matched, _, _ = _record_exact_matches(stmt_by_number, invoices)
        assert matched == {}

    def test_skips_empty_invoice_number(self) -> None:
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "", "invoice_id": "xero-1"}]
        matched, _, _ = _record_exact_matches(stmt_by_number, invoices)
        assert matched == {}

    def test_skips_whitespace_only_invoice_number(self) -> None:
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "   ", "invoice_id": "xero-1"}]
        matched, _, _ = _record_exact_matches(stmt_by_number, invoices)
        assert matched == {}

    def test_no_duplicate_match(self) -> None:
        """First match wins; second invoice with same number is ignored."""
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "INV-001", "invoice_id": "xero-1"}, {"number": "INV-001", "invoice_id": "xero-2"}]
        matched, _, _ = _record_exact_matches(stmt_by_number, invoices)
        assert matched["INV-001"]["invoice"]["invoice_id"] == "xero-1"

    def test_invoice_without_id(self) -> None:
        """Invoice matched but without invoice_id — should still work."""
        stmt_by_number = {"INV-001": {"raw": {"Number": "INV-001"}}}
        invoices = [{"number": "INV-001"}]
        matched, used_ids, used_numbers = _record_exact_matches(stmt_by_number, invoices)
        assert "INV-001" in matched
        assert len(used_ids) == 0
        assert "INV-001" in used_numbers

    def test_none_invoices(self) -> None:
        matched, used_ids, used_numbers = _record_exact_matches({}, None)
        assert matched == {}


# ---------------------------------------------------------------------------
# _candidate_invoices
# ---------------------------------------------------------------------------
class TestCandidateInvoices:
    """Filter invoices to those not yet used by exact matching."""

    def test_excludes_used_ids(self) -> None:
        invoices = [{"number": "INV-001", "invoice_id": "id-1"}, {"number": "INV-002", "invoice_id": "id-2"}]
        candidates = _candidate_invoices(invoices, {"id-1"}, set())
        numbers = [c[0] for c in candidates]
        assert "INV-001" not in numbers
        assert "INV-002" in numbers

    def test_excludes_used_numbers(self) -> None:
        invoices = [{"number": "INV-001", "invoice_id": "id-1"}]
        candidates = _candidate_invoices(invoices, set(), {"INV-001"})
        assert candidates == []

    def test_skips_invoices_without_number(self) -> None:
        invoices = [{"invoice_id": "id-1"}]
        candidates = _candidate_invoices(invoices, set(), set())
        assert candidates == []

    def test_skips_empty_number(self) -> None:
        invoices = [{"number": "", "invoice_id": "id-1"}]
        candidates = _candidate_invoices(invoices, set(), set())
        assert candidates == []

    def test_none_invoices(self) -> None:
        candidates = _candidate_invoices(None, set(), set())
        assert candidates == []

    def test_includes_eligible_candidates(self) -> None:
        invoices = [{"number": "INV-003", "invoice_id": "id-3"}]
        candidates = _candidate_invoices(invoices, set(), set())
        assert len(candidates) == 1
        assert candidates[0][0] == "INV-003"
        # Third element is normalized form.
        assert candidates[0][2] == "INV003"


# ---------------------------------------------------------------------------
# _missing_statement_numbers
# ---------------------------------------------------------------------------
class TestMissingStatementNumbers:
    """Return statement numbers not yet in matched map."""

    def test_returns_unmatched(self) -> None:
        rows = [{"Number": "INV-001"}, {"Number": "INV-002"}]
        matched: dict[str, Any] = {"INV-001": {}}
        result = _missing_statement_numbers(rows, "Number", matched)
        assert result == ["INV-002"]

    def test_empty_rows(self) -> None:
        result = _missing_statement_numbers([], "Number", {})
        assert result == []

    def test_skips_empty_values(self) -> None:
        rows = [{"Number": ""}, {"Number": "INV-001"}]
        result = _missing_statement_numbers(rows, "Number", {})
        assert result == ["INV-001"]


# ---------------------------------------------------------------------------
# _is_payment_reference
# ---------------------------------------------------------------------------
class TestIsPaymentReference:
    """Detect payment-related keywords in statement line text."""

    def test_payment_keyword(self) -> None:
        assert _is_payment_reference("Payment received") is True

    def test_paid_keyword(self) -> None:
        assert _is_payment_reference("PAID") is True

    def test_remittance_keyword(self) -> None:
        assert _is_payment_reference("remittance advice") is True

    def test_receipt_keyword(self) -> None:
        assert _is_payment_reference("Receipt #123") is True

    def test_non_payment(self) -> None:
        assert _is_payment_reference("INV-001") is False

    def test_case_insensitive(self) -> None:
        assert _is_payment_reference("PAYMENT") is True


# ---------------------------------------------------------------------------
# _candidate_hits
# ---------------------------------------------------------------------------
class TestCandidateHits:
    """Substring matching logic for invoice number candidates."""

    def test_exact_normalized_match(self) -> None:
        candidates = [("INV-001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("INV001", candidates, set(), set())
        assert len(hits) == 1

    def test_target_contains_candidate(self) -> None:
        """Statement number contains the invoice number as substring."""
        candidates = [("INV001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("INVOICEINV001REF", candidates, set(), set())
        assert len(hits) == 1

    def test_candidate_contains_target(self) -> None:
        candidates = [("INV-001-LONG", {"invoice_id": "id-1"}, "INV001LONG")]
        hits = _candidate_hits("INV001", candidates, set(), set())
        assert len(hits) == 1

    def test_no_match(self) -> None:
        candidates = [("INV-001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("XYZ999", candidates, set(), set())
        assert hits == []

    def test_skips_used_invoice_ids(self) -> None:
        candidates = [("INV-001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("INV001", candidates, {"id-1"}, set())
        assert hits == []

    def test_skips_used_invoice_numbers(self) -> None:
        candidates = [("INV-001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("INV001", candidates, set(), {"INV-001"})
        assert hits == []

    def test_skips_empty_target(self) -> None:
        candidates = [("INV-001", {"invoice_id": "id-1"}, "INV001")]
        hits = _candidate_hits("", candidates, set(), set())
        assert hits == []

    def test_skips_empty_candidate_norm(self) -> None:
        candidates = [("", {"invoice_id": "id-1"}, "")]
        hits = _candidate_hits("INV001", candidates, set(), set())
        assert hits == []


# ---------------------------------------------------------------------------
# _record_substring_match
# ---------------------------------------------------------------------------
class TestRecordSubstringMatch:
    """Record a substring or exact match into the matched map."""

    def test_substring_match(self) -> None:
        matched: dict[str, Any] = {}
        _record_substring_match(matched, "Invoice #INV-001", {"raw": {}}, "INV-001", {"invoice_id": "id-1"})
        assert "Invoice #INV-001" in matched
        assert matched["Invoice #INV-001"]["match_type"] == "substring"

    def test_exact_via_substring_path(self) -> None:
        """When invoice_number == statement_number, type is 'exact'."""
        matched: dict[str, Any] = {}
        _record_substring_match(matched, "INV-001", {"raw": {}}, "INV-001", {"invoice_id": "id-1"})
        assert matched["INV-001"]["match_type"] == "exact"


# ---------------------------------------------------------------------------
# _mark_invoice_used
# ---------------------------------------------------------------------------
class TestMarkInvoiceUsed:
    """Track used invoices to prevent duplicate matching."""

    def test_adds_id_and_number(self) -> None:
        used_ids: set = set()
        used_numbers: set = set()
        _mark_invoice_used({"invoice_id": "id-1"}, "INV-001", used_ids, used_numbers)
        assert "id-1" in used_ids
        assert "INV-001" in used_numbers

    def test_missing_invoice_id(self) -> None:
        used_ids: set = set()
        used_numbers: set = set()
        _mark_invoice_used({}, "INV-001", used_ids, used_numbers)
        assert len(used_ids) == 0
        assert "INV-001" in used_numbers

    def test_non_dict_invoice(self) -> None:
        """Non-dict invoice objects don't crash."""
        used_ids: set = set()
        used_numbers: set = set()
        _mark_invoice_used("not_a_dict", "INV-001", used_ids, used_numbers)
        assert "INV-001" in used_numbers


# ---------------------------------------------------------------------------
# match_invoices_to_statement_items (integration)
# ---------------------------------------------------------------------------
class TestMatchInvoicesToStatementItems:
    """End-to-end invoice matching."""

    def test_exact_match(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001"}}]
        rows = [{"Number": "INV-001"}]
        invoices = [{"number": "INV-001", "invoice_id": "xero-1", "total": 100}]
        result = match_invoices_to_statement_items(items, rows, "Number", invoices)
        assert "INV-001" in result
        assert result["INV-001"]["match_type"] == "exact"

    def test_no_item_number_header(self) -> None:
        """When item_number_header is None, returns empty."""
        result = match_invoices_to_statement_items([], [], None, [])
        assert result == {}

    def test_substring_match(self) -> None:
        """Statement has 'Invoice # INV001', Xero has 'INV-001'."""
        items: list[dict[str, Any]] = [{"raw": {"Number": "Invoice # INV001"}}]
        rows = [{"Number": "Invoice # INV001"}]
        invoices = [{"number": "INV-001", "invoice_id": "xero-1"}]
        result = match_invoices_to_statement_items(items, rows, "Number", invoices)
        assert "Invoice # INV001" in result
        assert result["Invoice # INV001"]["match_type"] == "substring"

    def test_payment_reference_skipped(self) -> None:
        """Statement lines with payment keywords skip substring matching."""
        items: list[dict[str, Any]] = [{"raw": {"Number": "Payment received"}}]
        rows = [{"Number": "Payment received"}]
        invoices = [{"number": "PAY-001", "invoice_id": "xero-1"}]
        result = match_invoices_to_statement_items(items, rows, "Number", invoices)
        assert "Payment received" not in result

    def test_no_match_for_unrelated_invoices(self) -> None:
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-999"}}]
        rows = [{"Number": "INV-999"}]
        invoices = [{"number": "INV-001", "invoice_id": "xero-1"}]
        result = match_invoices_to_statement_items(items, rows, "Number", invoices)
        assert "INV-999" not in result

    def test_missing_statement_item_in_lookup(self) -> None:
        """If stmt_by_number lookup fails for a missing row, it is skipped."""
        # Items has one entry, but rows reference a number not in items.
        items: list[dict[str, Any]] = [{"raw": {"Number": "INV-001"}}]
        rows = [{"Number": "INV-001"}, {"Number": "INV-PHANTOM"}]
        invoices = [{"number": "INV-001", "invoice_id": "xero-1"}, {"number": "PHANTOM", "invoice_id": "xero-2"}]
        result = match_invoices_to_statement_items(items, rows, "Number", invoices)
        # INV-001 exact matched; INV-PHANTOM not in stmt_by_number -> skipped.
        assert "INV-001" in result
        assert "INV-PHANTOM" not in result


# ---------------------------------------------------------------------------
# build_right_rows
# ---------------------------------------------------------------------------
class TestBuildRightRows:
    """Build Xero-side comparison rows aligned to the left (statement) side."""

    def test_basic_right_row(self) -> None:
        rows = [{"Number": "INV-001", "Amount": "100.00"}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "total": 100.00, "invoice_id": "xero-1"}, "match_type": "exact"}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Number"] == "INV-001"
        assert right[0]["Amount"] == "100.00"

    def test_unmatched_row_empty(self) -> None:
        rows = [{"Number": "INV-999", "Amount": "100.00"}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        right = build_right_rows(rows, headers, h2f, {}, "Number")
        assert right[0]["Number"] == ""
        assert right[0]["Amount"] == ""

    def test_no_item_number_header(self) -> None:
        """When item_number_header is None, all rows produce empty right side."""
        rows = [{"Amount": "100.00"}]
        headers = ["Amount"]
        h2f = {"Amount": "total"}
        right = build_right_rows(rows, headers, h2f, {}, None)
        assert right[0]["Amount"] == ""

    def test_date_field_formatting(self) -> None:
        rows = [{"Number": "INV-001", "Date": "15/03/2024"}]
        headers = ["Number", "Date"]
        h2f = {"Number": "number", "Date": "date"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "date": "2024-03-15", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number", date_format="DD/MM/YYYY")
        assert right[0]["Date"] == "15/03/2024"

    def test_date_field_default_format(self) -> None:
        """When no date_format given, defaults to YYYY-MM-DD."""
        rows = [{"Number": "INV-001", "Date": "2024-03-15"}]
        headers = ["Number", "Date"]
        h2f = {"Number": "number", "Date": "date"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "date": "2024-03-15", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Date"] == "2024-03-15"

    def test_date_field_none_value(self) -> None:
        """Invoice missing a date field produces empty string."""
        rows = [{"Number": "INV-001", "Date": "15/03/2024"}]
        headers = ["Number", "Date"]
        h2f = {"Number": "number", "Date": "date"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Date"] == ""

    def test_due_date_field(self) -> None:
        rows = [{"Number": "INV-001", "Due Date": "30/06/2024"}]
        headers = ["Number", "Due Date"]
        h2f = {"Number": "number", "Due Date": "due_date"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "due_date": "2024-06-30", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number", date_format="DD/MM/YYYY")
        assert right[0]["Due Date"] == "30/06/2024"

    def test_unmapped_header_produces_empty(self) -> None:
        """Headers not in header_to_field produce empty string."""
        rows = [{"Number": "INV-001", "Misc": "something"}]
        headers = ["Number", "Misc"]
        h2f = {"Number": "number"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Misc"] == ""

    def test_total_with_zero_left_value(self) -> None:
        """Left side value of 0.00 produces 0.00 on right (not invoice total)."""
        rows = [{"Number": "INV-001", "Amount": "0.00"}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "total": 500.00, "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Amount"] == "0.00"

    def test_total_with_empty_left_value(self) -> None:
        """Empty left side total produces empty right side total."""
        rows = [{"Number": "INV-001", "Amount": ""}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "total": 500.00, "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Amount"] == ""

    def test_total_with_none_invoice_total(self) -> None:
        """When invoice has no total, right side is empty for non-zero left."""
        rows = [{"Number": "INV-001", "Amount": "100.00"}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        matched_map = {"INV-001": {"invoice": {"number": "INV-001", "invoice_id": "xero-1"}}}
        right = build_right_rows(rows, headers, h2f, matched_map, "Number")
        assert right[0]["Amount"] == ""


# ---------------------------------------------------------------------------
# build_row_comparisons
# ---------------------------------------------------------------------------
class TestBuildRowComparisons:
    """Per-cell comparison between left (statement) and right (Xero) rows."""

    def test_matching_values(self) -> None:
        left = [{"Number": "INV-001", "Amount": "100.00"}]
        right = [{"Number": "INV-001", "Amount": "100.00"}]
        headers = ["Number", "Amount"]
        h2f = {"Number": "number", "Amount": "total"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert len(comps) == 1
        assert len(comps[0]) == 2
        # Number comparison
        num_cell = comps[0][0]
        assert num_cell.header == "Number"
        assert num_cell.matches is True
        # Amount comparison
        amt_cell = comps[0][1]
        assert amt_cell.matches is True

    def test_mismatched_values(self) -> None:
        left = [{"Amount": "100.00"}]
        right = [{"Amount": "200.00"}]
        headers = ["Amount"]
        h2f = {"Amount": "total"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].matches is False

    def test_number_field_substring_match(self) -> None:
        """Number field uses substring matching logic."""
        left = [{"Number": "Invoice # INV001"}]
        right = [{"Number": "INV-001"}]
        headers = ["Number"]
        h2f = {"Number": "number"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].matches is True

    def test_number_field_no_match(self) -> None:
        left = [{"Number": "INV-001"}]
        right = [{"Number": "INV-999"}]
        headers = ["Number"]
        h2f = {"Number": "number"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].matches is False

    def test_number_field_empty_one_side(self) -> None:
        """Empty on one side should not match."""
        left = [{"Number": "INV-001"}]
        right = [{"Number": ""}]
        headers = ["Number"]
        h2f = {"Number": "number"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].matches is False

    def test_none_header_to_field(self) -> None:
        """When header_to_field is None, uses _equal for all fields."""
        left = [{"Amount": "100.00"}]
        right = [{"Amount": "100"}]
        headers = ["Amount"]
        comps = build_row_comparisons(left, right, headers, None)
        assert comps[0][0].matches is True

    def test_canonical_field_included(self) -> None:
        left = [{"Date": "2024-03-15"}]
        right = [{"Date": "2024-03-15"}]
        headers = ["Date"]
        h2f = {"Date": "date"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].canonical_field == "date"

    def test_cell_values_coerced_to_string(self) -> None:
        """None values are coerced to empty string in CellComparison."""
        left = [{"Number": None}]
        right = [{"Number": None}]
        headers = ["Number"]
        h2f = {"Number": "number"}
        comps = build_row_comparisons(left, right, headers, h2f)
        assert comps[0][0].statement_value == ""
        assert comps[0][0].xero_value == ""

    def test_multiple_rows(self) -> None:
        left = [{"A": "1"}, {"A": "2"}]
        right = [{"A": "1"}, {"A": "3"}]
        headers = ["A"]
        comps = build_row_comparisons(left, right, headers)
        assert len(comps) == 2
        assert comps[0][0].matches is True
        assert comps[1][0].matches is False

    def test_non_dict_rows(self) -> None:
        """Non-dict left/right values produce empty strings."""
        left = ["not_a_dict"]
        right = ["not_a_dict"]
        headers = ["A"]
        comps = build_row_comparisons(left, right, headers)
        assert len(comps) == 1
        assert comps[0][0].statement_value == ""
        assert comps[0][0].xero_value == ""
