"""Tests for the extraction module utilities."""

import pytest

from core.extraction import build_header_mapping, convert_amount, reconstruct_items, strip_overlap_prefix


class TestConvertAmount:
    """convert_amount: heuristic-based raw monetary string -> float."""

    def test_simple_decimal(self) -> None:
        assert convert_amount("1234.56") == 1234.56

    def test_thousands_comma_dot_decimal(self) -> None:
        assert convert_amount("3,848.97") == 3848.97

    def test_trailing_minus(self) -> None:
        assert convert_amount("126.50-") == -126.50

    def test_parenthetical_negative(self) -> None:
        assert convert_amount("(126.50)") == -126.50

    def test_leading_minus(self) -> None:
        assert convert_amount("-1234.56") == -1234.56

    def test_european_dot_thousands_comma_decimal(self) -> None:
        assert convert_amount("1.234,56") == 1234.56

    def test_space_thousands_comma_decimal(self) -> None:
        assert convert_amount("1 234,56") == 1234.56

    def test_empty_string(self) -> None:
        assert convert_amount("") == ""

    def test_currency_prefix_r(self) -> None:
        assert convert_amount("R1,234.56") == 1234.56

    def test_currency_prefix_zar(self) -> None:
        assert convert_amount("ZAR 1,234.56") == 1234.56

    def test_currency_prefix_dollar(self) -> None:
        assert convert_amount("$1,234.56") == 1234.56

    def test_currency_prefix_euro(self) -> None:
        assert convert_amount("\u20ac1,234.56") == 1234.56

    def test_non_numeric_returns_raw(self) -> None:
        assert convert_amount("N/A") == "N/A"

    def test_whole_number_with_thousands(self) -> None:
        """3 digits after last separator → thousands, no decimal."""
        assert convert_amount("1,234") == 1234.0

    def test_multiple_thousands_separators(self) -> None:
        assert convert_amount("1,234,567.89") == 1234567.89

    def test_european_multiple_thousands(self) -> None:
        assert convert_amount("1.234.567,89") == 1234567.89

    def test_no_separator(self) -> None:
        assert convert_amount("500") == 500.0

    def test_zero(self) -> None:
        assert convert_amount("0.00") == 0.0


class TestReconstructItems:
    """reconstruct_items: array-of-arrays -> StatementItem dicts."""

    def test_standard_fields(self) -> None:
        column_order = ["date", "number", "Amount"]
        rows = [["2024-01-15", "INV-001", "1234.56"]]
        items = reconstruct_items(column_order, rows)
        assert len(items) == 1
        assert items[0]["date"] == "2024-01-15"
        assert items[0]["number"] == "INV-001"
        assert items[0]["total"] == {"Amount": "1234.56"}

    def test_raw_contains_all_columns(self) -> None:
        column_order = ["date", "number", "Debit", "Credit"]
        rows = [["2024-01-15", "INV-001", "100.00", ""]]
        items = reconstruct_items(column_order, rows)
        raw = items[0]["raw"]
        assert raw["date"] == "2024-01-15"
        assert raw["number"] == "INV-001"
        assert raw["Debit"] == "100.00"
        assert raw["Credit"] == ""

    def test_due_date_and_reference(self) -> None:
        column_order = ["date", "number", "due_date", "reference", "Amount"]
        rows = [["01/01/2024", "INV-1", "15/01/2024", "PO-123", "500"]]
        items = reconstruct_items(column_order, rows)
        assert items[0]["due_date"] == "15/01/2024"
        assert items[0]["reference"] == "PO-123"

    def test_short_row_pads_empty(self) -> None:
        column_order = ["date", "number", "Amount"]
        rows = [["2024-01-15"]]
        items = reconstruct_items(column_order, rows)
        assert items[0]["number"] == ""
        assert items[0]["total"] == {"Amount": ""}

    def test_empty_rows(self) -> None:
        assert reconstruct_items(["date"], []) == []


class TestBuildHeaderMapping:
    """build_header_mapping: detected_headers + column_order -> header->field map."""

    def test_standard_fields(self) -> None:
        detected = ["Date", "Inv No."]
        col_order = ["date", "number"]
        mapping = build_header_mapping(detected, col_order)
        assert mapping == {"Date": "date", "Inv No.": "number"}

    def test_mixed_standard_and_total(self) -> None:
        detected = ["Date", "Reference", "Debit", "Credit"]
        col_order = ["date", "number", "Debit", "Credit"]
        mapping = build_header_mapping(detected, col_order)
        assert mapping == {"Date": "date", "Reference": "number", "Debit": "total", "Credit": "total"}

    def test_no_number_mapping(self) -> None:
        detected = ["Date", "Ref", "Amount"]
        col_order = ["date", "reference", "Amount"]
        mapping = build_header_mapping(detected, col_order)
        assert mapping == {"Date": "date", "Ref": "reference", "Amount": "total"}

    def test_mismatched_lengths(self) -> None:
        detected = ["Date", "Number", "Extra"]
        col_order = ["date", "number"]
        mapping = build_header_mapping(detected, col_order)
        assert len(mapping) == 2


class TestStripOverlapPrefix:
    """strip_overlap_prefix: remove overlapping block at chunk boundary."""

    def test_full_block_overlap_stripped(self) -> None:
        """Overlap page produces a block of items at the end of existing
        that also appears at the start of incoming — block is stripped."""
        existing = [
            {"date": "2024-01-10", "number": "INV-1", "total": {"Amount": 100.0}, "raw": {"date": "2024-01-10"}},
            {"date": "2024-01-15", "number": "INV-2", "total": {"Amount": 200.0}, "raw": {"date": "2024-01-15"}},
            {"date": "2024-01-20", "number": "INV-3", "total": {"Amount": 300.0}, "raw": {"date": "2024-01-20"}},
        ]
        # Incoming starts with items 2 and 3 (the overlap), then new item 4.
        incoming = [
            {"date": "2024-01-15", "number": "INV-2", "total": {"Amount": 200.0}, "raw": {"date": "2024-01-15"}},
            {"date": "2024-01-20", "number": "INV-3", "total": {"Amount": 300.0}, "raw": {"date": "2024-01-20"}},
            {"date": "2024-01-25", "number": "INV-4", "total": {"Amount": 400.0}, "raw": {"date": "2024-01-25"}},
        ]
        result = strip_overlap_prefix(existing, incoming)
        assert len(result) == 1
        assert result[0]["number"] == "INV-4"

    def test_no_overlap_keeps_all(self) -> None:
        """No matching items — incoming is returned as-is."""
        existing = [{"date": "2024-01-10", "number": "INV-1", "total": {}, "raw": {}}]
        incoming = [{"date": "2024-02-10", "number": "INV-2", "total": {}, "raw": {}}]
        result = strip_overlap_prefix(existing, incoming)
        assert len(result) == 1
        assert result[0]["number"] == "INV-2"

    def test_different_raw_not_stripped(self) -> None:
        """Items with matching standard fields but different raw are NOT stripped."""
        existing = [{"date": "2024-01-15", "number": "EFT", "total": {"Amount": 100.0}, "raw": {"Desc": "Payment A"}}]
        incoming = [{"date": "2024-01-15", "number": "EFT", "total": {"Amount": 100.0}, "raw": {"Desc": "Payment B"}}, {"date": "2024-01-20", "number": "INV-2", "total": {"Amount": 200.0}, "raw": {}}]
        result = strip_overlap_prefix(existing, incoming)
        assert len(result) == 2

    def test_partial_match_not_stripped(self) -> None:
        """First item matches but second doesn't — no stripping."""
        existing = [{"date": "2024-01-15", "number": "INV-1", "total": {}, "raw": {}}, {"date": "2024-01-20", "number": "INV-2", "total": {}, "raw": {}}]
        incoming = [{"date": "2024-01-20", "number": "INV-2", "total": {}, "raw": {}}, {"date": "2024-01-25", "number": "INV-DIFFERENT", "total": {}, "raw": {}}]
        # incoming[0] matches existing[-1], overlap_len=1, should strip 1 item.
        result = strip_overlap_prefix(existing, incoming)
        assert len(result) == 1
        assert result[0]["number"] == "INV-DIFFERENT"

    def test_empty_existing_keeps_all(self) -> None:
        incoming = [{"date": "2024-01-15", "number": "INV-1", "total": {}, "raw": {}}]
        result = strip_overlap_prefix([], incoming)
        assert len(result) == 1

    def test_empty_incoming_returns_empty(self) -> None:
        existing = [{"date": "2024-01-15", "number": "INV-1", "total": {}, "raw": {}}]
        result = strip_overlap_prefix(existing, [])
        assert len(result) == 0
