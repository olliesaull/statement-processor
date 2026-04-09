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

# Graduated pricing tiers. Each tuple is (up_to, rate_pence).
# up_to=None means "unlimited" (the final catch-all tier).
# Tokens are charged at the rate of the tier they fall into:
#   first 499 at 10p, next 500 at 9p, remainder at 8p.
GRADUATED_TIERS: list[tuple[int | None, int]] = [(499, 10), (500, 9), (None, 8)]

# Minimum and maximum tokens per purchase (validated server-side and client-side).
MIN_TOKENS: int = 10
MAX_TOKENS: int = 10_000


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
