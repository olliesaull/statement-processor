"""Tests for StripeCustomerID persistence on TenantBillingTable."""

from tenant_billing_repository import TenantBillingRepository


def test_get_stripe_customer_id_returns_none_when_missing(monkeypatch) -> None:
    """No StripeCustomerID attribute should return None."""
    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: {"TenantID": tenant_id, "TokenBalance": 50}))
    assert TenantBillingRepository.get_stripe_customer_id("tenant-1") is None


def test_get_stripe_customer_id_returns_id_when_present(monkeypatch) -> None:
    """StripeCustomerID should be returned when it exists."""
    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: {"TenantID": tenant_id, "TokenBalance": 50, "StripeCustomerID": "cus_abc123"}))
    assert TenantBillingRepository.get_stripe_customer_id("tenant-1") == "cus_abc123"


def test_get_stripe_customer_id_returns_none_for_unknown_tenant(monkeypatch) -> None:
    """No billing record at all should return None."""
    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: None))
    assert TenantBillingRepository.get_stripe_customer_id("unknown") is None


def test_set_stripe_customer_id_calls_update_item(monkeypatch) -> None:
    """set_stripe_customer_id should write the ID to DynamoDB."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantBillingRepository, "_table", FakeTable())

    TenantBillingRepository.set_stripe_customer_id("tenant-1", "cus_new123")

    assert len(calls) == 1
    assert calls[0]["Key"] == {"TenantID": "tenant-1"}
    assert ":cid" in calls[0]["ExpressionAttributeValues"]
    assert calls[0]["ExpressionAttributeValues"][":cid"] == "cus_new123"
