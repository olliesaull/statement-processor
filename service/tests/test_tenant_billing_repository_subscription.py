"""Tests for subscription state persistence on TenantBillingTable."""

from tenant_billing_repository import SubscriptionState, TenantBillingRepository


def test_get_subscription_state_returns_none_when_no_fields(monkeypatch) -> None:
    """No subscription fields on the item should return None."""
    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: {"TenantID": tenant_id, "TokenBalance": 50}))
    assert TenantBillingRepository.get_subscription_state("tenant-1") is None


def test_get_subscription_state_returns_none_when_no_item(monkeypatch) -> None:
    """No billing record at all should return None."""
    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: None))
    assert TenantBillingRepository.get_subscription_state("unknown") is None


def test_get_subscription_state_returns_state_when_present(monkeypatch) -> None:
    """All subscription fields present should return a SubscriptionState."""
    monkeypatch.setattr(
        TenantBillingRepository,
        "get_item",
        classmethod(
            lambda cls, tenant_id: {
                "TenantID": tenant_id,
                "TokenBalance": 50,
                "SubscriptionTierID": "tier_200",
                "SubscriptionStatus": "active",
                "StripeSubscriptionID": "sub_abc123",
                "SubscriptionCurrentPeriodEnd": "2026-05-13T00:00:00+00:00",
                "TokensCreditedThisPeriod": 200,
            }
        ),
    )
    state = TenantBillingRepository.get_subscription_state("tenant-1")
    assert state is not None
    assert state.tier_id == "tier_200"
    assert state.status == "active"
    assert state.stripe_subscription_id == "sub_abc123"
    assert state.current_period_end == "2026-05-13T00:00:00+00:00"
    assert state.tokens_credited_this_period == 200


def test_update_subscription_state_calls_update_item(monkeypatch) -> None:
    """update_subscription_state should write all five fields via SET expression."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantBillingRepository, "_table", FakeTable())

    TenantBillingRepository.update_subscription_state(
        tenant_id="tenant-1", tier_id="tier_50", status="active", stripe_subscription_id="sub_xyz", current_period_end="2026-06-13T00:00:00+00:00", tokens_credited_this_period=50
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["Key"] == {"TenantID": "tenant-1"}
    assert "SET" in call["UpdateExpression"]
    vals = call["ExpressionAttributeValues"]
    assert vals[":tid"] == "tier_50"
    assert vals[":st"] == "active"
    assert vals[":sid"] == "sub_xyz"
    assert vals[":pe"] == "2026-06-13T00:00:00+00:00"
    assert vals[":tc"] == 50


def test_clear_subscription_state_calls_remove(monkeypatch) -> None:
    """clear_subscription_state should use REMOVE expression."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantBillingRepository, "_table", FakeTable())

    TenantBillingRepository.clear_subscription_state("tenant-1")

    assert len(calls) == 1
    call = calls[0]
    assert call["Key"] == {"TenantID": "tenant-1"}
    assert "REMOVE" in call["UpdateExpression"]
