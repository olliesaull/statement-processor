"""Tests for Stripe webhook handler — subscription event processing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from stripe_webhook_handler import StripeWebhookHandler


def _make_handler(*, billing_service: MagicMock | None = None, billing_repo: MagicMock | None = None, stripe_repo: MagicMock | None = None) -> StripeWebhookHandler:
    """Create a handler with mocked dependencies."""
    return StripeWebhookHandler(billing_service=billing_service or MagicMock(), billing_repo=billing_repo or MagicMock(), stripe_repo=stripe_repo or MagicMock())


def _invoice_paid_event(
    *, invoice_id: str = "in_abc", subscription_id: str = "sub_xyz", tenant_id: str = "tenant-1", tier_id: str = "tier_50", token_count: str = "50", period_end: int = 1747094400
) -> dict:
    """Build a minimal invoice.paid event dict."""
    return {
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": invoice_id,
                "subscription": subscription_id,
                "subscription_details": {"metadata": {"tenant_id": tenant_id, "tier_id": tier_id, "token_count": token_count}},
                "lines": {"data": [{"period": {"end": period_end}}]},
            }
        },
    }


def _subscription_updated_event(
    *, subscription_id: str = "sub_xyz", tenant_id: str = "tenant-1", tier_id: str = "tier_50", token_count: str = "50", status: str = "active", current_period_end: int = 1747094400
) -> dict:
    """Build a minimal customer.subscription.updated event dict."""
    return {
        "type": "customer.subscription.updated",
        "data": {"object": {"id": subscription_id, "status": status, "current_period_end": current_period_end, "metadata": {"tenant_id": tenant_id, "tier_id": tier_id, "token_count": token_count}}},
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
    """Upgrade: credits new_tier_tokens - already_credited."""
    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    # Already credited 50 tokens from previous tier.
    existing_state = MagicMock()
    existing_state.tokens_credited_this_period = 50
    billing_repo.get_subscription_state.return_value = existing_state

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    # Upgrading to tier_200 (200 tokens).
    handler.handle_event(_invoice_paid_event(tier_id="tier_200", token_count="200"))

    call_kwargs = billing_service.adjust_token_balance.call_args.kwargs
    assert call_kwargs["token_delta"] == 150  # 200 - 50


def test_invoice_paid_skips_credit_on_downgrade() -> None:
    """Downgrade: already got more tokens than new tier, credit 0."""
    billing_service = MagicMock()
    billing_repo = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False
    existing_state = MagicMock()
    existing_state.tokens_credited_this_period = 200
    billing_repo.get_subscription_state.return_value = existing_state

    handler = _make_handler(billing_service=billing_service, billing_repo=billing_repo, stripe_repo=stripe_repo)
    # Downgrading to tier_50 (50 tokens) — already got 200.
    handler.handle_event(_invoice_paid_event(tier_id="tier_50", token_count="50"))

    billing_service.adjust_token_balance.assert_not_called()
    # State should still be updated and invoice recorded.
    stripe_repo.record_processed_invoice.assert_called_once()
    billing_repo.update_subscription_state.assert_called_once()


def test_invoice_paid_unknown_tier_logs_error() -> None:
    """Unknown tier in metadata should log error and return without crediting."""
    billing_service = MagicMock()
    stripe_repo = MagicMock()
    stripe_repo.is_invoice_processed.return_value = False

    handler = _make_handler(billing_service=billing_service, stripe_repo=stripe_repo)

    with patch("stripe_webhook_handler.logger") as mock_logger:
        handler.handle_event(_invoice_paid_event(tier_id="", token_count=""))
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
