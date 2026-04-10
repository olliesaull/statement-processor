"""Tests for small service utility modules.

Covers:
- utils/tenant_status.py  — tenant status parsing and retrieval
- utils/workflows.py      — extraction state machine launcher
- utils/statement_rows.py — item-type labels and Xero ID extraction
- utils/formatting.py     — numeric normalisation, money formatting, dates, invoice data
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

import utils.formatting as formatting_mod
import utils.statement_rows as statement_rows_mod
import utils.tenant_status as tenant_status_mod
import utils.workflows as workflows_mod
from tenant_data_repository import TenantStatus

# ---------------------------------------------------------------------------
# Module 1: utils/tenant_status.py
# ---------------------------------------------------------------------------


class TestParseTenantStatusValue:
    """Tests for _parse_tenant_status_value — enum/string/invalid normalisation."""

    def test_returns_enum_unchanged(self) -> None:
        """A TenantStatus enum value should pass through untouched."""
        result = tenant_status_mod._parse_tenant_status_value(TenantStatus.FREE, "t1")
        assert result is TenantStatus.FREE

    def test_converts_valid_string(self) -> None:
        """A valid string matching a TenantStatus value should be parsed."""
        result = tenant_status_mod._parse_tenant_status_value("FREE", "t1")
        assert result is TenantStatus.FREE

    def test_converts_loading_string(self) -> None:
        """Cover another valid enum value to confirm string round-trip."""
        result = tenant_status_mod._parse_tenant_status_value("LOADING", "t1")
        assert result is TenantStatus.LOADING

    def test_invalid_string_returns_none(self) -> None:
        """An unrecognised string should log a warning and return None."""
        result = tenant_status_mod._parse_tenant_status_value("bogus", "t1")
        assert result is None

    def test_none_returns_none(self) -> None:
        """None (missing status field) should return None."""
        result = tenant_status_mod._parse_tenant_status_value(None, "t1")
        assert result is None

    def test_non_string_non_enum_returns_none(self) -> None:
        """Non-string, non-enum types (e.g. int) should return None."""
        result = tenant_status_mod._parse_tenant_status_value(42, "t1")
        assert result is None


class TestGetTenantStatus:
    """Tests for get_tenant_status — DynamoDB retrieval wrapper."""

    def test_returns_status_for_existing_tenant(self, monkeypatch) -> None:
        """Should parse and return the status from DynamoDB record."""
        monkeypatch.setattr(tenant_status_mod, "TenantDataRepository", type("FakeRepo", (), {"get_item": staticmethod(lambda tid: {"TenantStatus": "FREE"})}))
        assert tenant_status_mod.get_tenant_status("t1") is TenantStatus.FREE

    def test_returns_none_when_record_missing(self, monkeypatch) -> None:
        """Should return None when DynamoDB returns nothing."""
        monkeypatch.setattr(tenant_status_mod, "TenantDataRepository", type("FakeRepo", (), {"get_item": staticmethod(lambda tid: None)}))
        assert tenant_status_mod.get_tenant_status("t1") is None

    def test_returns_none_for_empty_tenant_id(self) -> None:
        """Empty tenant_id should short-circuit to None without a DB call."""
        assert tenant_status_mod.get_tenant_status("") is None

    def test_returns_none_when_status_key_absent(self, monkeypatch) -> None:
        """Record exists but has no TenantStatus key."""
        monkeypatch.setattr(tenant_status_mod, "TenantDataRepository", type("FakeRepo", (), {"get_item": staticmethod(lambda tid: {"SomeOtherKey": "val"})}))
        assert tenant_status_mod.get_tenant_status("t1") is None


# ---------------------------------------------------------------------------
# Module 2: utils/workflows.py
# ---------------------------------------------------------------------------


class _FakeStepFunctions:
    """Minimal stub for the Step Functions client."""

    def __init__(self, side_effect=None):
        self.calls: list[dict] = []
        self._side_effect = side_effect

    def start_execution(self, **kwargs):
        self.calls.append(kwargs)
        if self._side_effect:
            raise self._side_effect


_DEFAULT_KWARGS = dict(tenant_id="t1", contact_id="c1", statement_id="s1", pdf_key="t1/pdf/s1.pdf", json_key="t1/json/s1.json", page_count=3)


class TestStartExtractionStateMachine:
    """Tests for start_extraction_state_machine."""

    def test_success_returns_true(self, monkeypatch) -> None:
        """Happy path: execution starts and returns True."""
        fake_sf = _FakeStepFunctions()
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:sm:test")
        monkeypatch.setattr(workflows_mod, "S3_BUCKET_NAME", "test-bucket")
        monkeypatch.setattr(workflows_mod, "stepfunctions_client", fake_sf)

        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is True
        assert len(fake_sf.calls) == 1

    def test_missing_arn_returns_false(self, monkeypatch) -> None:
        """When ARN is falsy, should return False immediately."""
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "")
        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is False

    def test_none_arn_returns_false(self, monkeypatch) -> None:
        """None ARN should also return False."""
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", None)
        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is False

    def test_execution_already_exists_returns_true(self, monkeypatch) -> None:
        """Duplicate execution is idempotent — returns True."""
        exc = ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}}, "StartExecution")
        fake_sf = _FakeStepFunctions(side_effect=exc)
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "arn:test")
        monkeypatch.setattr(workflows_mod, "S3_BUCKET_NAME", "bucket")
        monkeypatch.setattr(workflows_mod, "stepfunctions_client", fake_sf)

        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is True

    def test_other_client_error_returns_false(self, monkeypatch) -> None:
        """Non-duplicate ClientError should return False."""
        exc = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "StartExecution")
        fake_sf = _FakeStepFunctions(side_effect=exc)
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "arn:test")
        monkeypatch.setattr(workflows_mod, "S3_BUCKET_NAME", "bucket")
        monkeypatch.setattr(workflows_mod, "stepfunctions_client", fake_sf)

        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is False

    def test_unexpected_exception_returns_false(self, monkeypatch) -> None:
        """Totally unexpected errors should still return False."""
        fake_sf = _FakeStepFunctions(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "arn:test")
        monkeypatch.setattr(workflows_mod, "S3_BUCKET_NAME", "bucket")
        monkeypatch.setattr(workflows_mod, "stepfunctions_client", fake_sf)

        assert workflows_mod.start_extraction_state_machine(**_DEFAULT_KWARGS) is False

    def test_execution_name_truncated_to_80_chars(self, monkeypatch) -> None:
        """Execution name is '{tenant_id}-{statement_id}' capped at 80 chars."""
        fake_sf = _FakeStepFunctions()
        monkeypatch.setattr(workflows_mod, "EXTRACTION_STATE_MACHINE_ARN", "arn:test")
        monkeypatch.setattr(workflows_mod, "S3_BUCKET_NAME", "bucket")
        monkeypatch.setattr(workflows_mod, "stepfunctions_client", fake_sf)

        long_kwargs = {**_DEFAULT_KWARGS, "tenant_id": "t" * 50, "statement_id": "s" * 50}
        workflows_mod.start_extraction_state_machine(**long_kwargs)

        exec_name = fake_sf.calls[0]["name"]
        assert len(exec_name) == 80


# ---------------------------------------------------------------------------
# Module 3: utils/statement_rows.py
# ---------------------------------------------------------------------------


class TestFormatItemTypeLabel:
    """Tests for format_item_type_label — item type display mapping."""

    @pytest.mark.parametrize(
        "input_val, expected",
        [("credit_note", "CRN"), ("invoice", "INV"), ("payment", "PMT"), ("CREDIT_NOTE", "CRN"), ("Invoice", "INV")],
        ids=["credit_note", "invoice", "payment", "upper_credit_note", "mixed_case_invoice"],
    )
    def test_known_types(self, input_val: str, expected: str) -> None:
        """Known item types should map to their short labels."""
        assert statement_rows_mod.format_item_type_label(input_val) == expected

    def test_unknown_type_uppercased(self) -> None:
        """Unknown types should be uppercased with underscores replaced by spaces."""
        assert statement_rows_mod.format_item_type_label("debit_note") == "DEBIT NOTE"

    def test_none_returns_empty(self) -> None:
        """None input should return empty string."""
        assert statement_rows_mod.format_item_type_label(None) == ""

    def test_empty_string_returns_empty(self) -> None:
        """Empty string should return empty string."""
        assert statement_rows_mod.format_item_type_label("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        """Whitespace-only string should return empty string."""
        assert statement_rows_mod.format_item_type_label("   ") == ""


class TestXeroIdsForRow:
    """Tests for xero_ids_for_row — extract matched Xero IDs from row data."""

    def test_returns_ids_when_matched(self) -> None:
        """Should return both invoice and credit note IDs when present."""
        matched_map = {"101": {"invoice": {"invoice_id": "inv-abc", "credit_note_id": "cn-xyz"}}}
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, matched_map)
        assert inv_id == "inv-abc"
        assert cn_id == "cn-xyz"

    def test_returns_none_when_no_header(self) -> None:
        """None header means we cannot look up the row number."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row(None, {"Number": "101"}, {})
        assert inv_id is None
        assert cn_id is None

    def test_returns_none_when_row_number_empty(self) -> None:
        """Empty row number string should yield (None, None)."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": ""}, {})
        assert inv_id is None
        assert cn_id is None

    def test_returns_none_when_row_number_missing_from_map(self) -> None:
        """Row number not present in matched map should yield (None, None)."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "999"}, {})
        assert inv_id is None
        assert cn_id is None

    def test_returns_none_when_match_not_dict(self) -> None:
        """Non-dict match entry should yield (None, None)."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, {"101": "not-a-dict"})
        assert inv_id is None
        assert cn_id is None

    def test_returns_none_when_invoice_payload_not_dict(self) -> None:
        """Match exists but 'invoice' key is not a dict."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, {"101": {"invoice": "bad"}})
        assert inv_id is None
        assert cn_id is None

    def test_returns_none_for_whitespace_only_ids(self) -> None:
        """Whitespace-only IDs should be treated as absent."""
        matched_map = {"101": {"invoice": {"invoice_id": "  ", "credit_note_id": "  "}}}
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, matched_map)
        assert inv_id is None
        assert cn_id is None

    def test_returns_only_invoice_id(self) -> None:
        """When credit_note_id is absent, only invoice_id should be returned."""
        matched_map = {"101": {"invoice": {"invoice_id": "inv-abc"}}}
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, matched_map)
        assert inv_id == "inv-abc"
        assert cn_id is None

    def test_strips_whitespace_from_ids(self) -> None:
        """Leading/trailing whitespace in IDs should be stripped."""
        matched_map = {"101": {"invoice": {"invoice_id": " inv-abc ", "credit_note_id": " cn-xyz "}}}
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": "101"}, matched_map)
        assert inv_id == "inv-abc"
        assert cn_id == "cn-xyz"

    def test_row_number_stripped_from_left_row(self) -> None:
        """Whitespace around row number in left_row should be tolerated."""
        matched_map = {"101": {"invoice": {"invoice_id": "inv-abc", "credit_note_id": None}}}
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": " 101 "}, matched_map)
        assert inv_id == "inv-abc"
        assert cn_id is None

    def test_row_number_none_in_left_row(self) -> None:
        """None value for the header key should yield (None, None)."""
        inv_id, cn_id = statement_rows_mod.xero_ids_for_row("Number", {"Number": None}, {})
        assert inv_id is None
        assert cn_id is None


# ---------------------------------------------------------------------------
# Module 4: utils/formatting.py
# ---------------------------------------------------------------------------


class TestNormalizeSeparators:
    """Tests for _normalize_separators — raw value to dot-decimal string."""

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        assert formatting_mod._normalize_separators(None) is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string should return None."""
        assert formatting_mod._normalize_separators("") is None

    def test_int_passthrough(self) -> None:
        """int values should be stringified directly."""
        assert formatting_mod._normalize_separators(42) == "42"

    def test_float_passthrough(self) -> None:
        """float values should be stringified directly."""
        assert formatting_mod._normalize_separators(3.14) == "3.14"

    def test_decimal_passthrough(self) -> None:
        """Decimal values should be stringified directly."""
        assert formatting_mod._normalize_separators(Decimal("99.99")) == "99.99"

    def test_whitespace_only_returns_none(self) -> None:
        """Whitespace-only string should return None."""
        assert formatting_mod._normalize_separators("   ") is None

    def test_trailing_minus(self) -> None:
        """Trailing minus should be moved to front."""
        assert formatting_mod._normalize_separators("126.50-") == "-126.50"

    def test_thousands_separator_three_digits_after_dot(self) -> None:
        """3+ digits after last separator means it is a thousands separator."""
        # "1.000" → last dot has 3 digits after → thousands separator → "1000"
        assert formatting_mod._normalize_separators("1.000") == "1000"

    def test_thousands_separator_three_digits_after_comma(self) -> None:
        """Comma as thousands separator with 3+ digits after."""
        assert formatting_mod._normalize_separators("1,000") == "1000"

    def test_eu_format(self) -> None:
        """European format with dot-thousands and comma-decimal."""
        assert formatting_mod._normalize_separators("1.234,50") == "1234.50"

    def test_standard_format(self) -> None:
        """Standard US format with comma-thousands and dot-decimal."""
        assert formatting_mod._normalize_separators("1,234.50") == "1234.50"

    def test_no_separators(self) -> None:
        """Plain integer string without separators."""
        assert formatting_mod._normalize_separators("12345") == "12345"

    def test_only_non_numeric_returns_none(self) -> None:
        """String with only non-numeric characters should return None."""
        assert formatting_mod._normalize_separators("abc") is None

    def test_single_minus_returns_none(self) -> None:
        """A bare minus sign should return None."""
        assert formatting_mod._normalize_separators("-") is None

    def test_negative_decimal(self) -> None:
        """Negative decimal should be preserved."""
        assert formatting_mod._normalize_separators("-42.50") == "-42.50"

    def test_one_digit_after_separator_is_decimal(self) -> None:
        """1 digit after last separator → treated as decimal."""
        assert formatting_mod._normalize_separators("99.5") == "99.5"


class TestToDecimal:
    """Tests for _to_decimal — string/numeric to Decimal conversion."""

    def test_valid_string(self) -> None:
        """Normal numeric string should parse to Decimal."""
        assert formatting_mod._to_decimal("42.50") == Decimal("42.50")

    def test_none_returns_none(self) -> None:
        """None returns None immediately."""
        assert formatting_mod._to_decimal(None) is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None immediately."""
        assert formatting_mod._to_decimal("") is None

    def test_non_numeric_string_returns_none(self) -> None:
        """A non-numeric string that normalises to None returns None with a warning."""
        assert formatting_mod._to_decimal("abc") is None

    def test_invalid_operation_returns_none(self) -> None:
        """A string that normalises but still fails Decimal() should return None.

        The trailing-minus logic produces something like '--42' which triggers
        InvalidOperation in Decimal().
        """
        # "--" normalises to "--" which Decimal can't parse
        assert formatting_mod._to_decimal("--") is None


class TestFormatMoney:
    """Tests for format_money — human-readable money formatting."""

    def test_integer_input(self) -> None:
        """Integer should be formatted with .00 and thousands separators."""
        assert formatting_mod.format_money(1234) == "1,234.00"

    def test_decimal_string(self) -> None:
        """Standard decimal string should format correctly."""
        assert formatting_mod.format_money("1234.5") == "1,234.50"

    def test_negative_value(self) -> None:
        """Negative values should keep the minus sign."""
        assert formatting_mod.format_money("-99.9") == "-99.90"

    def test_none_returns_empty(self) -> None:
        """None returns empty string."""
        assert formatting_mod.format_money(None) == ""

    def test_empty_returns_empty(self) -> None:
        """Empty string returns empty string."""
        assert formatting_mod.format_money("") == ""

    def test_non_numeric_returns_original(self) -> None:
        """Non-parseable value returns the original string."""
        assert formatting_mod.format_money("N/A") == "N/A"


class TestFmtDate:
    """Tests for fmt_date — date/datetime to ISO string."""

    def test_datetime_object(self) -> None:
        """datetime should format as YYYY-MM-DD."""
        assert formatting_mod.fmt_date(datetime(2024, 3, 1, 12, 30)) == "2024-03-01"

    def test_date_object(self) -> None:
        """date should format as YYYY-MM-DD."""
        assert formatting_mod.fmt_date(date(2024, 12, 25)) == "2024-12-25"

    def test_string_returns_none(self) -> None:
        """Non-date types should return None."""
        assert formatting_mod.fmt_date("2024-03-01") is None

    def test_none_returns_none(self) -> None:
        """None should return None."""
        assert formatting_mod.fmt_date(None) is None


class TestFmtInvoiceData:
    """Tests for fmt_invoice_data — invoice object to dict normalisation."""

    def test_full_invoice_object(self) -> None:
        """All fields should be extracted from an attribute-accessible object."""
        contact = SimpleNamespace(contact_id="c-1", name="Acme Corp")
        inv = SimpleNamespace(
            invoice_id="inv-1",
            invoice_number="INV-001",
            type="ACCREC",
            status="AUTHORISED",
            date=date(2024, 3, 1),
            due_date=date(2024, 4, 1),
            reference="PO-123",
            total=Decimal("1500.00"),
            contact=contact,
        )
        result = formatting_mod.fmt_invoice_data(inv)
        assert result == {
            "invoice_id": "inv-1",
            "number": "INV-001",
            "type": "ACCREC",
            "status": "AUTHORISED",
            "date": "2024-03-01",
            "due_date": "2024-04-01",
            "reference": "PO-123",
            "total": Decimal("1500.00"),
            "contact_id": "c-1",
            "contact_name": "Acme Corp",
        }

    def test_missing_contact(self) -> None:
        """When contact is None, contact fields should be None."""
        inv = SimpleNamespace(invoice_id="inv-2", invoice_number=None, type=None, status=None, date=None, due_date=None, reference=None, total=None, contact=None)
        result = formatting_mod.fmt_invoice_data(inv)
        assert result["contact_id"] is None
        assert result["contact_name"] is None
        assert result["date"] is None

    def test_missing_attributes(self) -> None:
        """Object with no attributes should produce all-None dict."""
        inv = SimpleNamespace()
        result = formatting_mod.fmt_invoice_data(inv)
        assert result["invoice_id"] is None
        assert result["contact_id"] is None
