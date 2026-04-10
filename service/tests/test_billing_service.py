"""Unit tests for billing reservation and settlement writes."""

from __future__ import annotations

from io import BytesIO

from botocore.exceptions import ClientError
from src.enums import ProcessingStage, TokenReservationStatus
from werkzeug.datastructures import FileStorage

from billing_service import (
    ENTRY_TYPE_ADJUSTMENT,
    ENTRY_TYPE_CONSUME,
    ENTRY_TYPE_RELEASE,
    ENTRY_TYPE_RESERVE,
    LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT,
    SOURCE_UPLOAD_START_FAILURE,
    BillingService,
    BillingServiceError,
    InsufficientTokensError,
    StatementReservationMetadata,
)
from utils.statement_upload_validation import PreparedStatementUpload


def _make_upload(filename: str = "statement.pdf") -> FileStorage:
    """Build a small uploaded-file test double."""
    return FileStorage(stream=BytesIO(b"placeholder"), filename=filename, content_type="application/pdf")


def test_reserve_statement_uploads_builds_atomic_billing_transaction(monkeypatch) -> None:
    """Reservations should update billing and create ledger + header rows together."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    prepared_uploads = [
        PreparedStatementUpload(uploaded_file=_make_upload("one.pdf"), contact_id="contact-1", contact_name="Acme Ltd", page_count=2),
        PreparedStatementUpload(uploaded_file=_make_upload("two.pdf"), contact_id="contact-2", contact_name="Beta Ltd", page_count=3),
    ]

    reserved_uploads = BillingService.reserve_statement_uploads("tenant-1", prepared_uploads)

    assert len(reserved_uploads) == 2
    assert all(upload.reservation_ledger_entry_id.startswith("reserve#") for upload in reserved_uploads)

    transact_call = calls[0]
    transact_items = transact_call["TransactItems"]
    assert isinstance(transact_items, list)
    assert len(transact_items) == 5
    assert transact_items[0]["Update"]["ExpressionAttributeValues"][":total_pages"] == {"N": "5"}
    assert transact_items[1]["Put"]["Item"]["EntryType"] == {"S": ENTRY_TYPE_RESERVE}
    assert transact_items[2]["Put"]["Item"]["PdfPageCount"] == {"N": "2"}
    assert transact_items[2]["Put"]["Item"]["TokenReservationStatus"] == {"S": TokenReservationStatus.RESERVED}
    assert transact_items[2]["Put"]["Item"]["ProcessingStage"] == {"S": ProcessingStage.QUEUED}


def test_reserve_statement_uploads_surfaces_insufficient_balance(monkeypatch) -> None:
    """Conditional billing failures should become user-facing insufficiency errors."""

    exc = ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}, "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}, "TransactWriteItems")

    def _raise_transact_write_items(**kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_raise_transact_write_items)}))

    prepared_uploads = [PreparedStatementUpload(uploaded_file=_make_upload(), contact_id="contact-1", contact_name="Acme Ltd", page_count=2)]

    try:
        BillingService.reserve_statement_uploads("tenant-1", prepared_uploads)
    except InsufficientTokensError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected an InsufficientTokensError")


def test_release_statement_reservation_returns_tokens_and_links_to_reserve(monkeypatch) -> None:
    """Releases should add tokens back and write a settlement row."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))
    monkeypatch.setattr(
        BillingService,
        "get_statement_reservation_metadata",
        classmethod(
            lambda cls, tenant_id, statement_id: StatementReservationMetadata(
                statement_id=statement_id, page_count=4, reservation_ledger_entry_id=f"reserve#{statement_id}", status=TokenReservationStatus.RESERVED
            )
        ),
    )

    released = BillingService.release_statement_reservation("tenant-1", "statement-1", source=SOURCE_UPLOAD_START_FAILURE)

    assert released is True
    transact_items = calls[0]["TransactItems"]
    assert transact_items[0]["Update"]["ExpressionAttributeValues"][":page_count"] == {"N": "4"}
    assert transact_items[1]["Put"]["Item"]["EntryType"] == {"S": ENTRY_TYPE_RELEASE}
    assert transact_items[1]["Put"]["Item"]["TokenDelta"] == {"N": "4"}
    assert transact_items[1]["Put"]["Item"]["SettlesLedgerEntryID"] == {"S": "reserve#statement-1"}


def test_consume_statement_reservation_writes_zero_delta_settlement(monkeypatch) -> None:
    """Consumes should not touch the balance snapshot again."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))
    monkeypatch.setattr(
        BillingService,
        "get_statement_reservation_metadata",
        classmethod(
            lambda cls, tenant_id, statement_id: StatementReservationMetadata(
                statement_id=statement_id, page_count=4, reservation_ledger_entry_id=f"reserve#{statement_id}", status=TokenReservationStatus.RESERVED
            )
        ),
    )

    consumed = BillingService.consume_statement_reservation("tenant-1", "statement-1")

    assert consumed is True
    transact_items = calls[0]["TransactItems"]
    assert len(transact_items) == 2
    assert transact_items[0]["Put"]["Item"]["EntryType"] == {"S": ENTRY_TYPE_CONSUME}
    assert transact_items[0]["Put"]["Item"]["TokenDelta"] == {"N": "0"}


def test_adjust_token_balance_writes_snapshot_and_ledger(monkeypatch) -> None:
    """Positive manual adjustments should grant tokens atomically."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    result = BillingService.adjust_token_balance("tenant-1", 25)

    assert result.tenant_id == "tenant-1"
    assert result.token_delta == 25
    assert result.ledger_entry_id.startswith("adjustment#")

    transact_items = calls[0]["TransactItems"]
    assert len(transact_items) == 2
    assert transact_items[0]["Update"]["ExpressionAttributeValues"][":token_delta"] == {"N": "25"}
    assert "ConditionExpression" not in transact_items[0]["Update"]
    assert transact_items[1]["Put"]["Item"]["EntryType"] == {"S": ENTRY_TYPE_ADJUSTMENT}
    assert transact_items[1]["Put"]["Item"]["Source"] == {"S": LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT}


def test_adjust_token_balance_requires_sufficient_balance_for_negative_deltas(monkeypatch) -> None:
    """Negative manual adjustments should guard against overdrawing the tenant."""

    exc = ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}, "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}, "TransactWriteItems")

    def _raise_transact_write_items(**kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_raise_transact_write_items)}))

    try:
        BillingService.adjust_token_balance("tenant-1", -10)
    except InsufficientTokensError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected an InsufficientTokensError")


def test_adjust_token_balance_rejects_zero_delta() -> None:
    """Zero-value adjustments should be rejected before any DynamoDB write."""

    try:
        BillingService.adjust_token_balance("tenant-1", 0)
    except BillingServiceError as exc:
        assert str(exc) == "TokenDelta must be non-zero."
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected an exception for zero token delta")


# --- reserve_confirmed_statement ---


def test_reserve_confirmed_statement_builds_atomic_transaction(monkeypatch) -> None:
    """Confirming a config-suggestion statement should deduct tokens, create
    a ledger entry, and stamp reservation metadata on the existing header."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    reservation_id = BillingService.reserve_confirmed_statement("tenant-1", "stmt-abc", page_count=3)

    assert reservation_id == "reserve#stmt-abc"
    assert len(calls) == 1

    transact_items = calls[0]["TransactItems"]
    assert isinstance(transact_items, list)
    # 3 items: billing update, ledger put, statement header update.
    assert len(transact_items) == 3

    # Billing snapshot: deducts 3 pages.
    billing_update = transact_items[0]["Update"]
    assert billing_update["ExpressionAttributeValues"][":pages"] == {"N": "3"}
    assert "TokenBalance >= :pages" in billing_update["ConditionExpression"]

    # Ledger entry: reserve type, negative delta.
    ledger_put = transact_items[1]["Put"]
    assert ledger_put["Item"]["EntryType"] == {"S": ENTRY_TYPE_RESERVE}
    assert ledger_put["Item"]["TokenDelta"] == {"N": "-3"}
    assert ledger_put["Item"]["RelatedStatementID"] == {"S": "stmt-abc"}

    # Statement header: stamps reservation metadata (Update, not Put).
    header_update = transact_items[2]["Update"]
    assert header_update["ExpressionAttributeValues"][":rid"] == {"S": "reserve#stmt-abc"}
    assert header_update["ExpressionAttributeValues"][":status"] == {"S": TokenReservationStatus.RESERVED}


def test_reserve_confirmed_statement_raises_on_insufficient_tokens(monkeypatch) -> None:
    """Insufficient balance should raise InsufficientTokensError."""

    exc = ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}, "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}, "TransactWriteItems")

    def _raise(**kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_raise)}))

    try:
        BillingService.reserve_confirmed_statement("tenant-1", "stmt-abc", page_count=5)
    except InsufficientTokensError:
        pass
    else:  # pragma: no cover
        raise AssertionError("Expected InsufficientTokensError")


# --- price_per_token_pence in ledger entries ---


def test_adjust_token_balance_includes_price_per_token_pence_when_provided(monkeypatch) -> None:
    """When price_per_token_pence is given, it should appear in the ledger entry."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    result = BillingService.adjust_token_balance("tenant-1", 100, source="stripe-checkout", price_per_token_pence=9.25)

    assert result.tenant_id == "tenant-1"
    transact_items = calls[0]["TransactItems"]
    ledger_put = transact_items[1]["Put"]["Item"]
    assert ledger_put["PricePerTokenPence"] == {"N": "9.25"}


def test_adjust_token_balance_omits_price_per_token_pence_when_not_provided(monkeypatch) -> None:
    """When price_per_token_pence is not given, the ledger entry should not contain it."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    BillingService.adjust_token_balance("tenant-1", 25)

    transact_items = calls[0]["TransactItems"]
    ledger_put = transact_items[1]["Put"]["Item"]
    assert "PricePerTokenPence" not in ledger_put


def test_adjust_token_balance_stores_zero_price_for_welcome_grant(monkeypatch) -> None:
    """Welcome grants should store price_per_token_pence=0."""

    calls: list[dict[str, object]] = []

    def _fake_transact_write_items(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(BillingService, "_ddb_client", type("FakeClient", (), {"transact_write_items": staticmethod(_fake_transact_write_items)}))

    BillingService.adjust_token_balance("tenant-1", 5, source="welcome-grant", price_per_token_pence=0)

    transact_items = calls[0]["TransactItems"]
    ledger_put = transact_items[1]["Put"]["Item"]
    assert ledger_put["PricePerTokenPence"] == {"N": "0"}
