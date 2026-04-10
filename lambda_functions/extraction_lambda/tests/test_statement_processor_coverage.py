"""Tests for core.statement_processor — helper functions and run_extraction.

Covers _derive_date_range, _map_extraction_to_statement, _sanitize_for_dynamodb,
_persist_statement_items, and run_extraction.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from core.models import ExtractionResult, StatementItem, SupplierStatement
from core.statement_processor import ExtractionOutput, PersistItemsRequest, _derive_date_range, _map_extraction_to_statement, _persist_statement_items, _sanitize_for_dynamodb, run_extraction


# ---------------------------------------------------------------------------
# _sanitize_for_dynamodb
# ---------------------------------------------------------------------------
class TestSanitizeForDynamodb:
    """_sanitize_for_dynamodb: recursive type cleaning for DDB compatibility."""

    def test_none_returns_none(self) -> None:
        assert _sanitize_for_dynamodb(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _sanitize_for_dynamodb("") is None
        assert _sanitize_for_dynamodb("  ") is None

    def test_numeric_string_becomes_decimal(self) -> None:
        result = _sanitize_for_dynamodb("42.50")
        assert result == Decimal("42.50")

    def test_numeric_string_with_commas(self) -> None:
        result = _sanitize_for_dynamodb("1,234.56")
        assert result == Decimal("1234.56")

    def test_non_numeric_string_returned_stripped(self) -> None:
        assert _sanitize_for_dynamodb("  hello  ") == "hello"

    def test_float_becomes_decimal(self) -> None:
        result = _sanitize_for_dynamodb(3.14)
        assert isinstance(result, Decimal)
        assert result == Decimal("3.14")

    def test_int_passed_through(self) -> None:
        assert _sanitize_for_dynamodb(42) == 42

    def test_list_recursion(self) -> None:
        result = _sanitize_for_dynamodb(["", "hello", None, "42"])
        # Empty string and None are dropped from lists.
        assert result == ["hello", Decimal("42")]

    def test_dict_recursion(self) -> None:
        result = _sanitize_for_dynamodb({"a": "", "b": "hello", "c": None, "d": "10"})
        assert result == {"b": "hello", "d": Decimal("10")}

    def test_nested_dict_in_list(self) -> None:
        result = _sanitize_for_dynamodb([{"price": "9.99"}])
        assert result == [{"price": Decimal("9.99")}]

    def test_bool_passed_through(self) -> None:
        """Booleans aren't str/float/list/dict — they pass through."""
        assert _sanitize_for_dynamodb(True) is True

    def test_negative_number_string(self) -> None:
        result = _sanitize_for_dynamodb("-123.45")
        assert result == Decimal("-123.45")


# ---------------------------------------------------------------------------
# _derive_date_range
# ---------------------------------------------------------------------------
class TestDeriveDateRange:
    """_derive_date_range: compute earliest/latest dates from items."""

    def test_empty_items(self) -> None:
        earliest, latest = _derive_date_range([], "DD/MM/YYYY")
        assert earliest is None
        assert latest is None

    def test_items_with_no_dates(self) -> None:
        items = [StatementItem(date=None), StatementItem(date="")]
        earliest, latest = _derive_date_range(items, "DD/MM/YYYY")
        assert earliest is None
        assert latest is None

    def test_single_date(self) -> None:
        items = [StatementItem(date="15/03/2024")]
        earliest, latest = _derive_date_range(items, "DD/MM/YYYY")
        assert earliest == "2024-03-15"
        assert latest == "2024-03-15"

    def test_multiple_dates_sorted(self) -> None:
        items = [StatementItem(date="20/03/2024"), StatementItem(date="01/01/2024"), StatementItem(date="15/06/2024")]
        earliest, latest = _derive_date_range(items, "DD/MM/YYYY")
        assert earliest == "2024-01-01"
        assert latest == "2024-06-15"

    def test_unparseable_dates_skipped(self) -> None:
        items = [StatementItem(date="15/03/2024"), StatementItem(date="not-a-date")]
        earliest, latest = _derive_date_range(items, "DD/MM/YYYY")
        assert earliest == "2024-03-15"
        assert latest == "2024-03-15"


# ---------------------------------------------------------------------------
# _map_extraction_to_statement
# ---------------------------------------------------------------------------
class TestMapExtractionToStatement:
    """_map_extraction_to_statement: ExtractionResult → SupplierStatement."""

    def _make_extraction(self, items: list[StatementItem] | None = None) -> ExtractionResult:
        """Build a minimal ExtractionResult for testing."""
        return ExtractionResult(
            items=items or [],
            detected_headers=["Date", "Number", "Total"],
            header_mapping={"Date": "date", "Number": "number", "Total": "total"},
            date_format="DD/MM/YYYY",
            date_confidence="high",
            input_tokens=100,
            output_tokens=50,
            request_ids=["req-1"],
        )

    def test_empty_items(self) -> None:
        extraction = self._make_extraction([])
        result = _map_extraction_to_statement(extraction, "stmt-1")
        assert isinstance(result, SupplierStatement)
        assert result.statement_items == []
        assert result.earliest_item_date is None
        assert result.latest_item_date is None

    def test_item_ids_assigned(self) -> None:
        items = [StatementItem(date="01/01/2024"), StatementItem(date="02/01/2024")]
        extraction = self._make_extraction(items)
        result = _map_extraction_to_statement(extraction, "stmt-1")
        assert result.statement_items[0].statement_item_id == "stmt-1#item-0001"
        assert result.statement_items[1].statement_item_id == "stmt-1#item-0002"

    def test_metadata_copied(self) -> None:
        extraction = self._make_extraction()
        result = _map_extraction_to_statement(extraction, "stmt-1")
        assert result.date_format == "DD/MM/YYYY"
        assert result.date_confidence == "high"
        assert result.detected_headers == ["Date", "Number", "Total"]
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    def test_date_range_computed(self) -> None:
        items = [StatementItem(date="10/01/2024"), StatementItem(date="20/03/2024")]
        extraction = self._make_extraction(items)
        result = _map_extraction_to_statement(extraction, "stmt-1")
        assert result.earliest_item_date == "2024-01-10"
        assert result.latest_item_date == "2024-03-20"


# ---------------------------------------------------------------------------
# _persist_statement_items
# ---------------------------------------------------------------------------
class TestPersistStatementItems:
    """_persist_statement_items: DDB write with completion status preservation."""

    @pytest.fixture()
    def mock_table(self, monkeypatch):
        """Patch tenant_statements_table for persist tests."""
        table = MagicMock()
        monkeypatch.setattr("core.statement_processor.tenant_statements_table", table)
        return table

    def test_no_statement_id_returns_early(self, mock_table) -> None:
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id=None, items=[]))
        mock_table.query.assert_not_called()

    def test_empty_items_deletes_existing(self, mock_table) -> None:
        """When items list is empty, existing rows are deleted but no inserts happen."""
        mock_table.query.return_value = {"Items": [{"StatementID": "stmt-1#item-0001", "Completed": "true"}]}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=[]))

        # Existing items deleted.
        batch_ctx.delete_item.assert_called_once()

    def test_writes_items_with_contact_id(self, mock_table) -> None:
        """Items are written with ContactID when provided."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001", "date": "01/01/2024"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        batch_ctx.put_item.assert_called_once()
        put_args = batch_ctx.put_item.call_args
        record = put_args.kwargs.get("Item") or put_args[1].get("Item")
        assert record["ContactID"] == "c1"
        assert record["TenantID"] == "t1"
        assert record["RecordType"] == "statement_item"

    def test_preserves_existing_completion_status(self, mock_table) -> None:
        """Re-processing preserves per-item Completed flag from prior run."""
        mock_table.query.return_value = {"Items": [{"StatementID": "stmt-1#item-0001", "Completed": "true"}]}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001", "date": "01/01/2024"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        put_args = batch_ctx.put_item.call_args
        record = put_args.kwargs.get("Item") or put_args[1].get("Item")
        assert record["Completed"] == "true"

    def test_new_item_inherits_header_completed(self, mock_table) -> None:
        """New items (not previously seen) inherit header's Completed flag."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "true"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        put_args = batch_ctx.put_item.call_args
        record = put_args.kwargs.get("Item") or put_args[1].get("Item")
        assert record["Completed"] == "true"

    def test_skips_items_without_statement_item_id(self, mock_table) -> None:
        """Items missing statement_item_id are silently skipped."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"date": "01/01/2024"}]  # no statement_item_id
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        batch_ctx.put_item.assert_not_called()

    def test_skips_non_dict_items(self, mock_table) -> None:
        """Non-dict entries in the items list are skipped."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = ["not-a-dict"]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        batch_ctx.put_item.assert_not_called()

    def test_updates_date_range_on_header(self, mock_table) -> None:
        """When date range is provided, the statement header is updated."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id=None, statement_id="stmt-1", items=items, earliest_item_date="2024-01-01", latest_item_date="2024-06-30"))

        mock_table.update_item.assert_called_once()

    def test_no_contact_id_omits_field(self, mock_table) -> None:
        """When contact_id is None, ContactID key is absent from the record."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id=None, statement_id="stmt-1", items=items))

        put_args = batch_ctx.put_item.call_args
        record = put_args.kwargs.get("Item") or put_args[1].get("Item")
        assert "ContactID" not in record

    def test_paginated_query(self, mock_table) -> None:
        """When DDB query is paginated, all pages are consumed."""
        mock_table.query.side_effect = [
            {"Items": [{"StatementID": "stmt-1#item-0001", "Completed": "false"}], "LastEvaluatedKey": {"k": "v"}},
            {"Items": [{"StatementID": "stmt-1#item-0002", "Completed": "true"}]},
        ]
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=[]))

        assert mock_table.query.call_count == 2
        # Both existing items deleted.
        assert batch_ctx.delete_item.call_count == 2

    def test_header_fetch_exception_defaults_to_false(self, mock_table) -> None:
        """If header row fetch fails, header_completed defaults to False."""
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.side_effect = Exception("DDB error")
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)

        items = [{"statement_item_id": "stmt-1#item-0001"}]
        _persist_statement_items(PersistItemsRequest(tenant_id="t1", contact_id="c1", statement_id="stmt-1", items=items))

        put_args = batch_ctx.put_item.call_args
        record = put_args.kwargs.get("Item") or put_args[1].get("Item")
        assert record["Completed"] == "false"


# ---------------------------------------------------------------------------
# run_extraction
# ---------------------------------------------------------------------------
class TestRunExtraction:
    """run_extraction: end-to-end orchestrator with mocked boundaries."""

    @pytest.fixture()
    def extraction_mocks(self, monkeypatch):
        """Set up all external boundary mocks for run_extraction."""
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=b"fake-pdf"))}
        mock_s3.put_object.return_value = {}
        monkeypatch.setattr("core.statement_processor.s3_client", mock_s3)

        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        mock_table.get_item.return_value = {"Item": {"Completed": "false"}}
        batch_ctx = MagicMock()
        mock_table.batch_writer.return_value.__enter__ = MagicMock(return_value=batch_ctx)
        mock_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr("core.statement_processor.tenant_statements_table", mock_table)

        extraction_result = ExtractionResult(
            items=[StatementItem(date="01/01/2024", number="INV-1", total={"Amount": 100.0})],
            detected_headers=["Date", "Number", "Total"],
            header_mapping={"Date": "date"},
            date_format="DD/MM/YYYY",
            date_confidence="high",
            input_tokens=50,
            output_tokens=30,
            request_ids=["req-abc"],
        )
        monkeypatch.setattr("core.statement_processor.extract_statement", lambda *a, **kw: extraction_result)

        monkeypatch.setattr("core.statement_processor.update_processing_stage", lambda *a, **kw: None)

        # apply_outlier_flags returns statement dict unchanged with empty summary.
        monkeypatch.setattr("core.statement_processor.apply_outlier_flags", lambda stmt, **kw: (stmt, {}))

        return {"s3": mock_s3, "table": mock_table, "batch_ctx": batch_ctx}

    def test_returns_filename_and_statement(self, extraction_mocks) -> None:
        result = run_extraction(bucket="test-bucket", pdf_key="t1/statements/stmt-1.pdf", json_key="t1/statements/stmt-1.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        assert isinstance(result, ExtractionOutput)
        assert result.filename == "stmt-1.json"
        assert isinstance(result.statement, dict)

    def test_reads_pdf_from_s3(self, extraction_mocks) -> None:
        run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        extraction_mocks["s3"].get_object.assert_called_once_with(Bucket="test-bucket", Key="t1/stmt.pdf")

    def test_uploads_json_to_s3(self, extraction_mocks) -> None:
        run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        extraction_mocks["s3"].put_object.assert_called_once()
        put_args = extraction_mocks["s3"].put_object.call_args
        assert put_args.kwargs["Key"] == "t1/stmt.json"

    def test_persists_items_to_ddb(self, extraction_mocks) -> None:
        run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        extraction_mocks["batch_ctx"].put_item.assert_called()

    def test_records_bedrock_request_ids(self, extraction_mocks) -> None:
        run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        extraction_mocks["table"].update_item.assert_called()

    def test_persist_failure_does_not_raise(self, extraction_mocks) -> None:
        """DDB persist failure is caught; extraction still completes."""
        extraction_mocks["table"].query.side_effect = Exception("DDB down")

        # Should not raise despite DDB failure.
        result = run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        assert isinstance(result, ExtractionOutput)

    def test_bedrock_request_id_failure_does_not_raise(self, extraction_mocks) -> None:
        """Failure to write Bedrock request IDs is logged but doesn't blow up."""
        # update_item is called by both _persist_statement_items (date range)
        # and run_extraction (Bedrock request IDs). Let the first succeed, fail
        # the second.
        extraction_mocks["table"].update_item.side_effect = [None, Exception("update failed")]

        result = run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        assert result.filename == "stmt.json"

    def test_null_table_skips_request_id_write(self, extraction_mocks, monkeypatch) -> None:
        """When tenant_statements_table is None, request ID write is skipped."""
        monkeypatch.setattr("core.statement_processor.tenant_statements_table", None)

        # _persist_statement_items also uses tenant_statements_table via module
        # global, so mock it at the function level to avoid the NoneType error.
        monkeypatch.setattr("core.statement_processor._persist_statement_items", lambda req: None)

        result = run_extraction(bucket="test-bucket", pdf_key="t1/stmt.pdf", json_key="t1/stmt.json", tenant_id="t1", contact_id="c1", statement_id="stmt-1", page_count=1)
        assert isinstance(result, ExtractionOutput)
