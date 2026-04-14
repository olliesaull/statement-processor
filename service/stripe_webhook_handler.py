"""Stripe webhook event handler for subscription billing.

Processes invoice.paid, customer.subscription.updated, and
customer.subscription.deleted events. Separated from the Flask route
for testability — dependencies are injected via constructor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from logger import logger
from pricing_config import STRIPE_PRICE_TO_TIER


class StripeWebhookHandler:
    """Dispatch and handle Stripe webhook events for subscriptions.

    Dependencies default to real classes but can be overridden in tests.
    """

    def __init__(self, billing_service: Any = None, billing_repo: Any = None, stripe_repo: Any = None, stripe_service: Any = None) -> None:
        if billing_service is None:
            from billing_service import BillingService  # pylint: disable=import-outside-toplevel

            billing_service = BillingService
        if billing_repo is None:
            from tenant_billing_repository import TenantBillingRepository  # pylint: disable=import-outside-toplevel

            billing_repo = TenantBillingRepository
        if stripe_repo is None:
            from stripe_repository import StripeRepository  # pylint: disable=import-outside-toplevel

            stripe_repo = StripeRepository
        if stripe_service is None:
            from stripe_service import StripeService  # pylint: disable=import-outside-toplevel

            stripe_service = StripeService()

        self._billing_service = billing_service
        self._billing_repo = billing_repo
        self._stripe_repo = stripe_repo
        self._stripe_service = stripe_service

    def handle_event(self, event: dict) -> None:
        """Dispatch a Stripe event to the appropriate handler."""
        event_type = event.get("type", "")
        handlers = {"invoice.paid": self._handle_invoice_paid, "customer.subscription.updated": self._handle_subscription_updated, "customer.subscription.deleted": self._handle_subscription_deleted}
        handler = handlers.get(event_type)
        if handler:
            handler(event)
        else:
            logger.debug("Ignoring unhandled Stripe event type", event_type=event_type)

    def _handle_invoice_paid(self, event: dict) -> None:
        """Process a paid subscription invoice.

        Idempotency: checks StripeRepository before crediting.
        Tier changes: credits difference (upgrade) or 0 (downgrade).
        """
        invoice = event["data"]["object"]
        invoice_id = invoice["id"]

        # Idempotency check — already processed?
        if self._stripe_repo.is_invoice_processed(invoice_id):
            logger.info("Invoice already processed, skipping", invoice_id=invoice_id)
            return

        # API version 2026-03-25.dahlia moved subscription details to
        # invoice.parent.subscription_details.
        parent_sub = invoice.get("parent", {}).get("subscription_details", {}) or {}
        metadata = parent_sub.get("metadata", {})
        subscription_id = parent_sub.get("subscription", "") or invoice.get("subscription", "")
        tenant_id = metadata.get("tenant_id", "")

        # Resolve tier from the price ID on invoice line items rather than
        # metadata — metadata is set at checkout and is NOT updated when
        # the customer changes tier via the Stripe Customer Portal.
        tier = self._resolve_tier_from_invoice_lines(invoice)

        # Fallback: retrieve tenant_id from subscription if not in invoice.
        if not tenant_id and subscription_id:
            sub_metadata = self._stripe_service.retrieve_subscription_metadata(subscription_id)
            tenant_id = sub_metadata.get("tenant_id", "")

        if not tenant_id or not tier:
            logger.error("Invoice missing required subscription data", invoice_id=invoice_id, tenant_id=tenant_id, tier_resolved=tier is not None)
            return

        tier_id = tier.tier_id
        token_count = tier.tokens_per_month

        # Determine period end from the invoice line item.
        lines = invoice.get("lines", {}).get("data", [])
        period_end_ts = lines[0]["period"]["end"] if lines else 0
        period_end_iso = datetime.fromtimestamp(period_end_ts, tz=UTC).isoformat() if period_end_ts else ""

        # Determine how many tokens were already credited this period.
        # On a new billing period (renewal), reset to 0 so the full tier amount
        # is credited. On a mid-period tier change, use the delta so the tenant
        # only receives the difference (upgrade) or 0 (downgrade).
        existing_state = self._billing_repo.get_subscription_state(tenant_id)
        is_new_period = not existing_state or period_end_iso != existing_state.current_period_end
        already_credited = 0 if is_new_period else existing_state.tokens_credited_this_period
        tokens_to_credit = max(0, token_count - already_credited)

        ledger_entry_id = f"subscription#{invoice_id}"

        if tokens_to_credit > 0:
            self._billing_service.adjust_token_balance(tenant_id=tenant_id, token_delta=tokens_to_credit, source="subscription", ledger_entry_id=ledger_entry_id)

        # Record processed invoice for idempotency.
        self._stripe_repo.record_processed_invoice(invoice_id=invoice_id, tenant_id=tenant_id, tier_id=tier_id, tokens_credited=tokens_to_credit, ledger_entry_id=ledger_entry_id)

        # Update cached subscription state.
        self._billing_repo.update_subscription_state(
            tenant_id=tenant_id,
            tier_id=tier_id,
            status="active",
            stripe_subscription_id=subscription_id,
            current_period_end=period_end_iso,
            tokens_credited_this_period=already_credited + tokens_to_credit,
        )

        logger.info("Processed subscription invoice", invoice_id=invoice_id, tenant_id=tenant_id, tier_id=tier_id, tokens_credited=tokens_to_credit)

    @staticmethod
    def _resolve_tier_from_invoice_lines(invoice: dict) -> "SubscriptionTier | None":
        """Resolve the subscription tier from invoice line item price IDs.

        On a proration invoice (tier change), multiple lines exist — pick
        the non-proration line (the new tier charge). On a simple renewal,
        there's typically one line.
        """
        lines = invoice.get("lines", {}).get("data", [])
        for line in lines:
            # Skip proration credits (negative amounts / credited items).
            sub_item_details = line.get("parent", {}).get("subscription_item_details", {}) or {}
            if sub_item_details.get("proration") and line.get("amount", 0) < 0:
                continue
            price_id = line.get("pricing", {}).get("price_details", {}).get("price", "")
            tier = STRIPE_PRICE_TO_TIER.get(price_id)
            if tier:
                return tier
        # Fallback: try any line with a matching price.
        for line in lines:
            price_id = line.get("pricing", {}).get("price_details", {}).get("price", "")
            tier = STRIPE_PRICE_TO_TIER.get(price_id)
            if tier:
                return tier
        return None

    @staticmethod
    def _resolve_tier_from_subscription_items(subscription: dict) -> "SubscriptionTier | None":
        """Resolve the subscription tier from subscription item price IDs."""
        items = subscription.get("items", {}).get("data", [])
        for item in items:
            price_id = item.get("price", {}).get("id", "")
            tier = STRIPE_PRICE_TO_TIER.get(price_id)
            if tier:
                return tier
        return None

    def _handle_subscription_updated(self, event: dict) -> None:
        """Update cached subscription state from a subscription.updated event."""
        subscription = event["data"]["object"]
        metadata = subscription.get("metadata", {})
        tenant_id = metadata.get("tenant_id", "")

        if not tenant_id:
            logger.warning("subscription.updated missing tenant_id in metadata", subscription_id=subscription.get("id"))
            return

        # Resolve tier from price ID on subscription items — metadata is
        # stale after Customer Portal tier changes.
        tier = self._resolve_tier_from_subscription_items(subscription)
        tier_id = tier.tier_id if tier else metadata.get("tier_id", "")

        # API version 2026-03-25.dahlia moved current_period_end from the
        # top-level subscription to items.data[].current_period_end.
        items = subscription.get("items", {}).get("data", [])
        period_end_ts = items[0]["current_period_end"] if items else subscription.get("current_period_end")
        period_end_iso = datetime.fromtimestamp(period_end_ts, tz=UTC).isoformat() if period_end_ts else ""

        # Track pending cancellation — cancel_at is a Unix timestamp when set.
        cancel_at_ts = subscription.get("cancel_at")
        cancel_at_iso = datetime.fromtimestamp(cancel_at_ts, tz=UTC).isoformat() if cancel_at_ts else ""

        # Preserve tokens_credited_this_period from existing state.
        existing_state = self._billing_repo.get_subscription_state(tenant_id)
        tokens_credited = existing_state.tokens_credited_this_period if existing_state else 0

        self._billing_repo.update_subscription_state(
            tenant_id=tenant_id,
            tier_id=tier_id,
            status=subscription.get("status", ""),
            stripe_subscription_id=subscription["id"],
            current_period_end=period_end_iso,
            tokens_credited_this_period=tokens_credited,
            cancel_at=cancel_at_iso,
        )

        logger.info("Updated subscription state", tenant_id=tenant_id, subscription_id=subscription["id"], tier_id=tier_id, cancel_at=cancel_at_iso)

    def _handle_subscription_deleted(self, event: dict) -> None:
        """Clear subscription state when a subscription is cancelled."""
        subscription = event["data"]["object"]
        metadata = subscription.get("metadata", {})
        tenant_id = metadata.get("tenant_id", "")

        if not tenant_id:
            logger.warning("subscription.deleted missing tenant_id in metadata", subscription_id=subscription.get("id"))
            return

        self._billing_repo.clear_subscription_state(tenant_id)

        logger.info("Cleared subscription state", tenant_id=tenant_id, subscription_id=subscription["id"])
