"""Unit tests for StripeRepository — DynamoDB idempotency and customer ID cache.

All DynamoDB calls are intercepted by monkeypatching the table objects so no
real AWS calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import stripe_repository as stripe_repository_module
from stripe_repository import StripeRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_table_mock() -> MagicMock:
    """Build a minimal DynamoDB Table mock."""
    return MagicMock()


# ---------------------------------------------------------------------------
# is_session_processed
# ---------------------------------------------------------------------------


def test_is_session_processed_returns_true_when_item_exists(monkeypatch) -> None:
    """Should return True when DynamoDB contains a record for this session."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {"Item": {"StripeEventID": "cs_abc"}}
    monkeypatch.setattr(stripe_repository_module, "_event_store", table_mock)

    result = StripeRepository.is_session_processed("cs_abc")

    assert result is True
    table_mock.get_item.assert_called_once_with(Key={"StripeEventID": "cs_abc"}, ProjectionExpression="StripeEventID")


def test_is_session_processed_returns_false_when_no_item(monkeypatch) -> None:
    """Should return False when no record exists for this session."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {}  # no "Item" key
    monkeypatch.setattr(stripe_repository_module, "_event_store", table_mock)

    result = StripeRepository.is_session_processed("cs_notfound")

    assert result is False


# ---------------------------------------------------------------------------
# record_processed_session
# ---------------------------------------------------------------------------


def test_record_processed_session_writes_correct_attributes(monkeypatch) -> None:
    """All required attributes must appear in the written item."""
    table_mock = _make_table_mock()
    monkeypatch.setattr(stripe_repository_module, "_event_store", table_mock)

    StripeRepository.record_processed_session(session_id="cs_xyz", tenant_id="tenant-1", tokens_credited=50, ledger_entry_id="purchase#cs_xyz")

    table_mock.put_item.assert_called_once()
    item = table_mock.put_item.call_args.kwargs["Item"]

    assert item["StripeEventID"] == "cs_xyz"
    assert item["EventType"] == "checkout.session.completed"
    assert item["TenantID"] == "tenant-1"
    assert item["TokensCredited"] == 50
    assert item["LedgerEntryID"] == "purchase#cs_xyz"
    # ProcessedAt must be an ISO-8601 string
    assert isinstance(item["ProcessedAt"], str)
    assert "T" in item["ProcessedAt"]


# ---------------------------------------------------------------------------
# get_processed_session
# ---------------------------------------------------------------------------


def test_get_processed_session_returns_item_when_found(monkeypatch) -> None:
    """Should return the DynamoDB item dict when a record exists."""
    stored_item = {"StripeEventID": "cs_abc", "TokensCredited": 100}
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {"Item": stored_item}
    monkeypatch.setattr(stripe_repository_module, "_event_store", table_mock)

    result = StripeRepository.get_processed_session("cs_abc")

    assert result == stored_item


def test_get_processed_session_returns_none_when_not_found(monkeypatch) -> None:
    """Should return None when the record does not exist (race window)."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {}
    monkeypatch.setattr(stripe_repository_module, "_event_store", table_mock)

    result = StripeRepository.get_processed_session("cs_missing")

    assert result is None


# ---------------------------------------------------------------------------
# get_cached_customer_id
# ---------------------------------------------------------------------------


def test_get_cached_customer_id_returns_cached_value(monkeypatch) -> None:
    """Should return the StripeCustomerID attribute from TenantBillingTable."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {"Item": {"StripeCustomerID": "cus_cached"}}
    monkeypatch.setattr(stripe_repository_module, "_billing_table", table_mock)

    result = StripeRepository.get_cached_customer_id("tenant-1")

    assert result == "cus_cached"
    table_mock.get_item.assert_called_once_with(Key={"TenantID": "tenant-1"}, ProjectionExpression="StripeCustomerID")


def test_get_cached_customer_id_returns_none_when_attribute_absent(monkeypatch) -> None:
    """Should return None when the item exists but StripeCustomerID is not set."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {"Item": {"TokenBalance": 100}}  # no StripeCustomerID
    monkeypatch.setattr(stripe_repository_module, "_billing_table", table_mock)

    result = StripeRepository.get_cached_customer_id("tenant-1")

    assert result is None


def test_get_cached_customer_id_returns_none_when_no_item(monkeypatch) -> None:
    """Should return None when the tenant billing record does not exist."""
    table_mock = _make_table_mock()
    table_mock.get_item.return_value = {}
    monkeypatch.setattr(stripe_repository_module, "_billing_table", table_mock)

    result = StripeRepository.get_cached_customer_id("tenant-new")

    assert result is None


# ---------------------------------------------------------------------------
# cache_customer_id
# ---------------------------------------------------------------------------


def test_cache_customer_id_writes_to_billing_table(monkeypatch) -> None:
    """Should write StripeCustomerID via update_item on the billing table."""
    table_mock = _make_table_mock()
    monkeypatch.setattr(stripe_repository_module, "_billing_table", table_mock)

    StripeRepository.cache_customer_id("tenant-1", "cus_new")

    table_mock.update_item.assert_called_once_with(Key={"TenantID": "tenant-1"}, UpdateExpression="SET StripeCustomerID = :cid", ExpressionAttributeValues={":cid": "cus_new"})
