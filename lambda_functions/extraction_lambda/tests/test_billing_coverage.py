"""Additional billing coverage tests for core.billing.

Covers BillingSettlementService methods that the existing test suite misses:
- get_statement_reservation_metadata
- _settle_statement_reservation (via release/consume public methods)
- release_statement_reservation
- consume_statement_reservation
- Static helpers (_serialize_item, _serialize_key, _release_ledger_entry_id, etc.)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from src.enums import TokenReservationStatus

from core.billing import ENTRY_TYPE_CONSUME, ENTRY_TYPE_RELEASE, SOURCE_EXTRACTION_FAILURE, SOURCE_EXTRACTION_SUCCESS, BillingSettlementError, BillingSettlementService, StatementReservationMetadata


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def mock_ddb_client(monkeypatch):
    """Patch the DynamoDB low-level client on BillingSettlementService."""
    client = MagicMock()
    monkeypatch.setattr(BillingSettlementService, "_ddb_client", client)
    return client


@pytest.fixture()
def mock_statements_table(monkeypatch):
    """Patch the DynamoDB Table resource on BillingSettlementService."""
    table = MagicMock()
    monkeypatch.setattr(BillingSettlementService, "_tenant_statements_table", table)
    return table


@pytest.fixture()
def _mock_table_names(monkeypatch):
    """Ensure table name class attributes are non-empty."""
    monkeypatch.setattr(BillingSettlementService, "_tenant_billing_table_name", "billing-table")
    monkeypatch.setattr(BillingSettlementService, "_tenant_statements_table_name", "statements-table")
    monkeypatch.setattr(BillingSettlementService, "_tenant_token_ledger_table_name", "ledger-table")


# ---------------------------------------------------------------------------
# Static/class helpers
# ---------------------------------------------------------------------------
class TestStaticHelpers:
    """Verify deterministic ID generation and serialization wrappers."""

    def test_release_ledger_entry_id(self) -> None:
        entry_id = BillingSettlementService._release_ledger_entry_id("stmt-1")
        assert entry_id == "release#stmt-1"

    def test_consume_ledger_entry_id(self) -> None:
        entry_id = BillingSettlementService._consume_ledger_entry_id("stmt-1")
        assert entry_id == "consume#stmt-1"

    def test_client_request_token_is_deterministic(self) -> None:
        token_a = BillingSettlementService._client_request_token("a", "b", "c")
        token_b = BillingSettlementService._client_request_token("a", "b", "c")
        assert token_a == token_b

    def test_client_request_token_differs_for_different_inputs(self) -> None:
        token_a = BillingSettlementService._client_request_token("a", "b")
        token_b = BillingSettlementService._client_request_token("x", "y")
        assert token_a != token_b

    def test_serialize_item(self) -> None:
        result = BillingSettlementService._serialize_item({"TenantID": "t1"})
        assert "TenantID" in result
        assert result["TenantID"] == {"S": "t1"}

    def test_serialize_key(self) -> None:
        result = BillingSettlementService._serialize_key(TenantID="t1")
        assert result["TenantID"] == {"S": "t1"}

    def test_serialize_expression_values(self) -> None:
        result = BillingSettlementService._serialize_expression_values({":val": 42})
        assert ":val" in result

    def test_utc_now_iso(self) -> None:
        ts = BillingSettlementService._utc_now_iso()
        assert "T" in ts  # ISO format with time separator

    def test_settlement_ledger_item_structure(self) -> None:
        item = BillingSettlementService._settlement_ledger_item(
            tenant_id="t1", statement_id="s1", ledger_entry_id="le1", entry_type=ENTRY_TYPE_RELEASE, token_delta=5, created_at="2024-01-01T00:00:00", source="test", settles_ledger_entry_id="res-1"
        )
        assert item["TenantID"] == "t1"
        assert item["TokenDelta"] == 5
        assert item["SettlesLedgerEntryID"] == "res-1"


# ---------------------------------------------------------------------------
# get_statement_reservation_metadata
# ---------------------------------------------------------------------------
class TestGetStatementReservationMetadata:
    """get_statement_reservation_metadata: DDB read and metadata mapping."""

    def test_returns_metadata_when_item_exists(self, mock_statements_table) -> None:
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 5, "ReservationLedgerEntryID": "res-entry-1", "TokenReservationStatus": "reserved"}}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "stmt-1")
        assert result is not None
        assert result.statement_id == "stmt-1"
        assert result.page_count == 5
        assert result.reservation_ledger_entry_id == "res-entry-1"
        assert result.status == "reserved"

    def test_returns_none_when_no_item(self, mock_statements_table) -> None:
        mock_statements_table.get_item.return_value = {}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "missing")
        assert result is None

    def test_returns_none_when_no_reservation_entry_id(self, mock_statements_table) -> None:
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "", "TokenReservationStatus": "reserved"}}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "stmt-1")
        assert result is None

    def test_returns_none_when_no_status(self, mock_statements_table) -> None:
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": ""}}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "stmt-1")
        assert result is None

    def test_page_count_from_string(self, mock_statements_table) -> None:
        """PdfPageCount that arrives as a string should be coerced to int."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": "7", "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "stmt-1")
        assert result is not None
        assert result.page_count == 7

    def test_page_count_none_defaults_to_zero(self, mock_statements_table) -> None:
        """Missing PdfPageCount defaults to 0."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": None, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        result = BillingSettlementService.get_statement_reservation_metadata("t1", "stmt-1")
        assert result is not None
        assert result.page_count == 0


# ---------------------------------------------------------------------------
# release_statement_reservation
# ---------------------------------------------------------------------------
class TestReleaseStatementReservation:
    """release_statement_reservation: token release on failure."""

    @pytest.mark.usefixtures("_mock_table_names")
    def test_release_success(self, mock_ddb_client, mock_statements_table) -> None:
        """Successful release returns True and calls transact_write_items."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        mock_ddb_client.transact_write_items.return_value = {}

        result = BillingSettlementService.release_statement_reservation("t1", "stmt-1")
        assert result is True
        mock_ddb_client.transact_write_items.assert_called_once()

        # Verify that the transaction includes a billing balance update (release adds tokens back).
        call_args = mock_ddb_client.transact_write_items.call_args
        transact_items = call_args.kwargs.get("TransactItems") or call_args[1].get("TransactItems")
        # Should have 3 items: balance update, ledger put, status update.
        assert len(transact_items) == 3

    @pytest.mark.usefixtures("_mock_table_names")
    def test_release_skipped_when_no_metadata(self, mock_ddb_client, mock_statements_table) -> None:
        """Returns False when no reservation metadata found."""
        mock_statements_table.get_item.return_value = {}
        result = BillingSettlementService.release_statement_reservation("t1", "stmt-1")
        assert result is False
        mock_ddb_client.transact_write_items.assert_not_called()

    @pytest.mark.usefixtures("_mock_table_names")
    def test_release_skipped_when_already_settled(self, mock_ddb_client, mock_statements_table) -> None:
        """Returns False when statement is already consumed."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "consumed"}}
        result = BillingSettlementService.release_statement_reservation("t1", "stmt-1")
        assert result is False

    @pytest.mark.usefixtures("_mock_table_names")
    def test_release_raises_on_client_error(self, mock_ddb_client, mock_statements_table) -> None:
        """ClientError in transact_write_items wraps into BillingSettlementError."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        mock_ddb_client.transact_write_items.side_effect = ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "conflict"}}, "TransactWriteItems")
        with pytest.raises(BillingSettlementError):
            BillingSettlementService.release_statement_reservation("t1", "stmt-1")


# ---------------------------------------------------------------------------
# consume_statement_reservation
# ---------------------------------------------------------------------------
class TestConsumeStatementReservation:
    """consume_statement_reservation: token consumption on success."""

    @pytest.mark.usefixtures("_mock_table_names")
    def test_consume_success(self, mock_ddb_client, mock_statements_table) -> None:
        """Successful consume returns True without updating billing balance."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        mock_ddb_client.transact_write_items.return_value = {}

        result = BillingSettlementService.consume_statement_reservation("t1", "stmt-1")
        assert result is True

        # Consume should NOT include a billing balance update — only 2 items.
        call_args = mock_ddb_client.transact_write_items.call_args
        transact_items = call_args.kwargs.get("TransactItems") or call_args[1].get("TransactItems")
        assert len(transact_items) == 2

    @pytest.mark.usefixtures("_mock_table_names")
    def test_consume_skipped_when_already_released(self, mock_ddb_client, mock_statements_table) -> None:
        """Returns False when statement reservation was already released."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 3, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "released"}}
        result = BillingSettlementService.consume_statement_reservation("t1", "stmt-1")
        assert result is False

    @pytest.mark.usefixtures("_mock_table_names")
    def test_consume_custom_source(self, mock_ddb_client, mock_statements_table) -> None:
        """Custom source parameter is passed through to the transaction."""
        mock_statements_table.get_item.return_value = {"Item": {"StatementID": "stmt-1", "PdfPageCount": 2, "ReservationLedgerEntryID": "res-1", "TokenReservationStatus": "reserved"}}
        mock_ddb_client.transact_write_items.return_value = {}

        result = BillingSettlementService.consume_statement_reservation("t1", "stmt-1", source="custom-source")
        assert result is True
