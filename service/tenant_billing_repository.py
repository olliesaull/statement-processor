"""Repository helpers for tenant billing snapshots stored in DynamoDB."""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from config import tenant_billing_table
from repository_helpers import fetch_items_by_tenant_id


@dataclass(frozen=True)
class SubscriptionState:
    """Cached subscription state from TenantBillingTable.

    Informational cache — Stripe is the source of truth.
    Updated via webhooks.
    """

    tier_id: str
    status: str
    stripe_subscription_id: str
    current_period_end: str
    tokens_credited_this_period: int


@dataclass(frozen=True)
class TenantBillingRepository:
    """Repository wrapper around the TenantBilling DynamoDB table."""

    _table = tenant_billing_table

    @staticmethod
    def _determine_token_balance(item: dict[str, Any]) -> int:
        """Extract an integer token balance from a DynamoDB billing record."""
        raw_balance = item.get("TokenBalance")

        if raw_balance is None:
            return 0

        if isinstance(raw_balance, (int, Decimal)):
            return int(raw_balance)

        return 0

    @classmethod
    def get_item(cls, tenant_id: str) -> dict[str, object] | None:
        """Fetch a single tenant billing record by ID."""
        if not tenant_id:
            return None

        response = cls._table.get_item(Key={"TenantID": tenant_id})
        return response.get("Item")

    @classmethod
    def get_tenant_token_balance(cls, tenant_id: str | None) -> int:
        """Fetch the current token balance snapshot for one tenant."""
        item = cls.get_item((tenant_id or "").strip())
        if not item:
            return 0
        return cls._determine_token_balance(item)

    @classmethod
    def _get_items_by_tenant_id(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, dict[str, object] | None]:
        """Fetch multiple tenant billing records concurrently."""
        return fetch_items_by_tenant_id(cls.get_item, tenant_ids, max_workers=max_workers)

    @classmethod
    def get_stripe_customer_id(cls, tenant_id: str) -> str | None:
        """Fetch the persistent Stripe Customer ID for a tenant.

        Returns None if the tenant has no billing record or no
        StripeCustomerID attribute (first-time purchaser).
        """
        item = cls.get_item((tenant_id or "").strip())
        if not item:
            return None
        cid = item.get("StripeCustomerID")
        return str(cid) if cid else None

    @classmethod
    def set_stripe_customer_id(cls, tenant_id: str, stripe_customer_id: str) -> None:
        """Persist a Stripe Customer ID on the tenant billing record.

        Called after the first Stripe Customer is created for a tenant.
        Subsequent purchases reuse this customer (last-write-wins on
        billing details — invoices snapshot details at creation time).
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="SET StripeCustomerID = :cid", ExpressionAttributeValues={":cid": stripe_customer_id})

    @classmethod
    def get_tenant_token_balances(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, int]:
        """Fetch multiple tenant billing records concurrently and return their token balances."""
        unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}
        balances: dict[str, int] = dict.fromkeys(unique_ids, 0)
        items = cls._get_items_by_tenant_id(unique_ids, max_workers=max_workers)

        for tenant_id, item in items.items():
            if item:
                balances[tenant_id] = cls._determine_token_balance(item)

        return balances

    @classmethod
    def get_subscription_state(cls, tenant_id: str) -> SubscriptionState | None:
        """Fetch cached subscription state for a tenant.

        Returns None if the tenant has no billing record or no subscription fields.
        """
        item = cls.get_item((tenant_id or "").strip())
        if not item:
            return None

        tier_id = item.get("SubscriptionTierID")
        if not tier_id:
            return None

        return SubscriptionState(
            tier_id=str(tier_id),
            status=str(item.get("SubscriptionStatus", "")),
            stripe_subscription_id=str(item.get("StripeSubscriptionID", "")),
            current_period_end=str(item.get("SubscriptionCurrentPeriodEnd", "")),
            tokens_credited_this_period=int(item.get("TokensCreditedThisPeriod", 0)),
        )

    @classmethod
    def update_subscription_state(cls, *, tenant_id: str, tier_id: str, status: str, stripe_subscription_id: str, current_period_end: str, tokens_credited_this_period: int) -> None:
        """Write subscription state fields to the tenant billing record."""
        cls._table.update_item(
            Key={"TenantID": tenant_id},
            UpdateExpression=("SET SubscriptionTierID = :tid, SubscriptionStatus = :st, StripeSubscriptionID = :sid, SubscriptionCurrentPeriodEnd = :pe, TokensCreditedThisPeriod = :tc"),
            ExpressionAttributeValues={":tid": tier_id, ":st": status, ":sid": stripe_subscription_id, ":pe": current_period_end, ":tc": tokens_credited_this_period},
        )

    @classmethod
    def clear_subscription_state(cls, tenant_id: str) -> None:
        """Remove all subscription fields from the tenant billing record."""
        cls._table.update_item(
            Key={"TenantID": tenant_id}, UpdateExpression=("REMOVE SubscriptionTierID, SubscriptionStatus, StripeSubscriptionID, SubscriptionCurrentPeriodEnd, TokensCreditedThisPeriod")
        )
