"""Single source of truth for token pricing.

Graduated pricing tiers: the first N tokens are charged at one rate,
the next M at a lower rate, and so on. This avoids price cliffs where
buying N+1 tokens would paradoxically cost less than N tokens.

Design decision: we store a single effective rate (total_pence / token_count,
rounded to 2dp) rather than individual ledger entries per tier. The Stripe
invoice is authoritative for exact per-tier breakdowns. See spec:
docs/superpowers/specs/2026-04-09-pricing-model-redesign.md §1.4

This module is imported by both Python server code (validation, Stripe
session creation) and serialised to JSON for the JavaScript live price
calculator, ensuring one source of truth for pricing logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from config import get_envar

# Graduated pricing tiers. Each tuple is (up_to, rate_pence).
# up_to=None means "unlimited" (the final catch-all tier).
# Tokens are charged at the rate of the tier they fall into:
#   first 499 at 10p, next 500 at 9p, remainder at 8p.
GRADUATED_TIERS: list[tuple[int | None, int]] = [(499, 10), (500, 9), (None, 8)]

# Minimum and maximum tokens per purchase (validated server-side and client-side).
MIN_TOKENS: int = 10
MAX_TOKENS: int = 10_000


@dataclass(frozen=True)
class SubscriptionTier:
    """A monthly subscription tier with fixed token allocation and per-token rate.

    Attributes:
        tier_id: Internal identifier (e.g. "tier_50"). Used in DynamoDB and Stripe metadata.
        display_name: User-facing name (e.g. "50 Pages/mo"). Subject to change.
        tokens_per_month: Number of tokens credited each billing cycle.
        rate_pence: Per-token rate in pence.
        stripe_price_id: Stripe Price ID for this tier's recurring price.
    """

    tier_id: str
    display_name: str
    tokens_per_month: int
    rate_pence: int
    stripe_price_id: str

    @property
    def monthly_price_pence(self) -> int:
        """Total monthly price in pence."""
        return self.tokens_per_month * self.rate_pence


SUBSCRIPTION_TIERS: dict[str, SubscriptionTier] = {
    "tier_50": SubscriptionTier(tier_id="tier_50", display_name="50 Pages/mo", tokens_per_month=50, rate_pence=9, stripe_price_id=get_envar("STRIPE_PRICE_ID_TIER_50")),
    "tier_200": SubscriptionTier(tier_id="tier_200", display_name="200 Pages/mo", tokens_per_month=200, rate_pence=8, stripe_price_id=get_envar("STRIPE_PRICE_ID_TIER_200")),
    "tier_500": SubscriptionTier(tier_id="tier_500", display_name="500 Pages/mo", tokens_per_month=500, rate_pence=7, stripe_price_id=get_envar("STRIPE_PRICE_ID_TIER_500")),
}


class PricingConfig:
    """Graduated pricing calculations for pay-as-you-go token purchases."""

    # Expose module-level constants as class attributes for convenient access.
    MIN_TOKENS = MIN_TOKENS
    MAX_TOKENS = MAX_TOKENS

    @staticmethod
    def calculate_total_pence(token_count: int) -> int:
        """Calculate the total price in pence for a graduated token purchase.

        Args:
            token_count: Number of tokens to price.

        Returns:
            Total price in pence (integer — no fractional pence).
        """
        total = 0
        remaining = token_count
        for up_to, rate_pence in GRADUATED_TIERS:
            if up_to is None:
                # Final tier: absorbs all remaining tokens.
                total += remaining * rate_pence
                remaining = 0
            else:
                chunk = min(remaining, up_to)
                total += chunk * rate_pence
                remaining -= chunk
            if remaining <= 0:
                break
        return total

    @staticmethod
    def effective_rate_pence(token_count: int) -> float:
        """Calculate the effective per-token rate in pence, rounded to 2dp.

        This is the value stored in the token ledger's PricePerTokenPence
        field. Rounding means reconstructing total from tokens x rate may
        differ by a few pence — the Stripe invoice is always authoritative.

        Args:
            token_count: Number of tokens purchased.

        Returns:
            Effective rate in pence, rounded to 2 decimal places.
        """
        total = PricingConfig.calculate_total_pence(token_count)
        return round(total / token_count, 2)

    @staticmethod
    def tiers_as_json() -> str:
        """Serialise graduated tiers to JSON for template injection.

        The JavaScript live price calculator reads this to compute prices
        client-side using the same tier definitions as the server.

        Returns:
            JSON string: list of {up_to: int|null, rate_pence: int}.
        """
        return json.dumps([{"up_to": up_to, "rate_pence": rate_pence} for up_to, rate_pence in GRADUATED_TIERS])

    @staticmethod
    def subscription_tiers_as_json() -> str:
        """Serialise subscription tiers to JSON for template injection."""
        return json.dumps(
            [
                {"tier_id": tier.tier_id, "display_name": tier.display_name, "tokens_per_month": tier.tokens_per_month, "rate_pence": tier.rate_pence, "monthly_price_pence": tier.monthly_price_pence}
                for tier in SUBSCRIPTION_TIERS.values()
            ]
        )
