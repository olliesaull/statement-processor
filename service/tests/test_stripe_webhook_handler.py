"""Tests for Stripe webhook handler — subscription event processing."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from stripe_webhook_handler import StripeWebhookHandler


@dataclass(frozen=True)
class _FakeTier:
    """Minimal tier object for tests."""

    tier_id: str
    tokens_per_month: int


# Fake price-to-tier mapping used by all tests.
_FAKE_PRICE_MAP = {
    "price_test_50": _FakeTier(tier_id="tier_50", tokens_per_month=50),
    "price_test_200": _FakeTier(tier_id="tier_200", tokens_per_month=200),
    "price_test_500": _FakeTier(tier_id="tier_500", tokens_per_month=500),
}


def _make_handler(
    *, billing_service: MagicMock | None = None, billing_repo: MagicMock | None = None, stripe_repo: MagicMock | None = None, stripe_service: MagicMock | None = None
) -> StripeWebhookHandler:
    """Create a handler with mocked dependencies."""
    return StripeWebhookHandler(
        billing_service=billing_service or MagicMock(), billing_repo=billing_repo or MagicMock(), stripe_repo=stripe_repo or MagicMock(), stripe_service=stripe_service or MagicMock()
    )


_TIER_TO_PRICE = {"tier_50": "price_test_50", "tier_200": "price_test_200", "tier_500": "price_test_500"}


@pytest.fixture(autouse=True)
def _patch_price_map():
    """Patch the price-to-tier lookup for all tests in this module."""
    with patch("stripe_webhook_handler.STRIPE_PRICE_TO_TIER", _FAKE_PRICE_MAP):
        yield


def _invoice_paid_event(*, invoice_id: str = "in_abc", subscription_id: str = "sub_xyz", tenant_id: str = "tenant-1", tier_id: str = "tier_50", period_end: int = 1747094400) -> dict:
    """Build a minimal invoice.paid event dict matching API version 2026-03-25.dahlia."""
    price_id = _TIER_TO_PRICE.get(tier_id, "price_test_unknown")
    return {
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": invoice_id,
                "parent": {"subscription_details": {"subscription": subscription_id, "metadata": {"tenant_id": tenant_id}}, "type": "subscription_details"},
                "lines": {"data": [{"amount": 450, "period": {"end": period_end}, "parent": {"subscription_item_details": {"proration": False}}, "pricing": {"price_details": {"price": price_id}}}]},
            }
        },
    }


def _subscription_updated_event(*, subscription_id: str = "sub_xyz", tenant_id: str = "tenant-1", tier_id: str = "tier_50", status: str = "active", current_period_end: int = 1747094400) -> dict:
    """Build a minimal customer.subscription.updated event dict matching API version 2026-03-25.dahlia."""
    price_id = _TIER_TO_PRICE.get(tier_id, "price_test_unknown")
    return {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": subscription_id, "status": status, "items": {"data": [{"current_period_end": current_period_end, "price": {"id": price_id}}]}, "metadata": {"tenant_id": tenant_id}}},
    }


def _subscription_deleted_event(*, subscription_id: str = "sub_xyz", tenant_id: str = "tenant-1") -> dict:
    """Build a minimal customer.subscription.deleted event dict."""
    return {"type": "customer.subscription.deleted", "data": {"object": {"id": subscription_id, "metadata": {"tenant_id": tenant_id}}}}


# ---------------------------------------------------------------------------
# invoice.paid
# ---------------------------------------------------------------------------


def test_invoice_paid_credits_tokens_and_updates_state() -> None:
    """Normal renewal: credits full tier amount and updates subscription state."""
    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    # No existing subscription state (fresh subscription or period reset).
    billing_repo.get_subscription_state.return_value = None

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    handler.handle_event(_invoice_paid_event())

    billing_service.adjust_token_balance.assert_called_once()
    call_kwargs = billing_service.adjust_token_balance.call_args.kwargs
    assert call_kwargs["tenant_id"] == "tenant-1"
    assert call_kwargs["token_delta"] == 50
    stripe_repo.record_processed_invoice.assert_called_once()
    billing_repo.update_subscription_state.assert_called_once()


def test_invoice_paid_skips_already_processed() -> None:
    """Idempotency: already-processed invoices are skipped entirely."""
    billing_service = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = True

    handler = _make_handler(billing_service=billing_service, stripe_repo=stripe_repo)
    handler.handle_event(_invoice_paid_event())

    billing_service.adjust_token_balance.assert_not_called()
    stripe_repo.record_processed_invoice.assert_not_called()


def test_invoice_paid_credits_difference_on_upgrade() -> None:
    """Upgrade (same period): credits new_tier_tokens - already_credited."""
    from datetime import UTC, datetime

    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    # Already credited 50 tokens from previous tier, same billing period.
    existing_state = MagicMock()
    existing_state.tokens_credited_this_period = 50
    existing_state.current_period_end = datetime.fromtimestamp(1747094400, tz=UTC).isoformat()
    billing_repo.get_subscription_state.return_value = existing_state

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    # Upgrading to tier_200 (200 tokens) within the same period.
    handler.handle_event(_invoice_paid_event(tier_id="tier_200"))

    call_kwargs = billing_service.adjust_token_balance.call_args.kwargs
    assert call_kwargs["token_delta"] == 150  # 200 - 50


def test_invoice_paid_skips_credit_on_downgrade() -> None:
    """Downgrade (same period): already got more tokens than new tier, credit 0."""
    from datetime import UTC, datetime

    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    existing_state = MagicMock()
    existing_state.tokens_credited_this_period = 200
    existing_state.current_period_end = datetime.fromtimestamp(1747094400, tz=UTC).isoformat()
    billing_repo.get_subscription_state.return_value = existing_state

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    # Downgrading to tier_50 (50 tokens) — already got 200 this period.
    handler.handle_event(_invoice_paid_event(tier_id="tier_50"))

    billing_service.adjust_token_balance.assert_not_called()
    # State should still be updated and invoice recorded.
    stripe_repo.record_processed_invoice.assert_called_once()
    billing_repo.update_subscription_state.assert_called_once()


def test_invoice_paid_resets_credit_on_new_period() -> None:
    """Renewal (new period): resets already_credited and credits full tier amount."""
    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    # Previous period had 50 tokens credited, but period_end differs from invoice.
    existing_state = MagicMock()
    existing_state.tokens_credited_this_period = 50
    existing_state.current_period_end = "2025-04-13T00:00:00+00:00"
    billing_repo.get_subscription_state.return_value = existing_state

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    # Invoice for new period (period_end=1747094400 → 2025-05-13).
    handler.handle_event(_invoice_paid_event())

    billing_service.adjust_token_balance.assert_called_once()
    call_kwargs = billing_service.adjust_token_balance.call_args.kwargs
    assert call_kwargs["token_delta"] == 50  # Full tier amount, not 0


def test_invoice_paid_unrecognised_price_logs_error() -> None:
    """Invoice with an unrecognised price ID should log error and not credit."""
    billing_service = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False

    handler = _make_handler(billing_service=billing_service, stripe_repo=stripe_repo)

    # Build event with a price ID that isn't in the tier map.
    event = _invoice_paid_event()
    event["data"]["object"]["lines"]["data"][0]["pricing"]["price_details"]["price"] = "price_unknown"

    with patch("stripe_webhook_handler.logger") as mock_logger:
        handler.handle_event(event)
        mock_logger.error.assert_called()

    billing_service.adjust_token_balance.assert_not_called()


# ---------------------------------------------------------------------------
# customer.subscription.updated
# ---------------------------------------------------------------------------


def test_subscription_updated_updates_cached_state() -> None:
    """subscription.updated should update cached state on billing record."""
    billing_repo = MagicMock()
    handler = _make_handler(billing_repo=billing_repo)
    handler.handle_event(_subscription_updated_event())

    billing_repo.update_subscription_state.assert_called_once()
    call_kwargs = billing_repo.update_subscription_state.call_args.kwargs
    assert call_kwargs["tenant_id"] == "tenant-1"
    assert call_kwargs["tier_id"] == "tier_50"
    assert call_kwargs["status"] == "active"


# ---------------------------------------------------------------------------
# customer.subscription.deleted
# ---------------------------------------------------------------------------


def test_subscription_deleted_clears_state() -> None:
    """subscription.deleted should clear subscription state."""
    billing_repo = MagicMock()
    handler = _make_handler(billing_repo=billing_repo)
    handler.handle_event(_subscription_deleted_event())

    billing_repo.clear_subscription_state.assert_called_once_with("tenant-1")


# ---------------------------------------------------------------------------
# Unhandled event types
# ---------------------------------------------------------------------------


def test_unhandled_event_type_ignored_gracefully() -> None:
    """Unknown event types should be silently ignored."""
    handler = _make_handler()
    # Should not raise.
    handler.handle_event({"type": "payment_intent.created", "data": {"object": {}}})
