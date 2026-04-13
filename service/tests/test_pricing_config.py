"""Unit tests for graduated pricing calculation and subscription tier config."""

import json

from pricing_config import SUBSCRIPTION_TIERS, PricingConfig, SubscriptionTier


def test_price_below_first_threshold() -> None:
    """Tokens below the first breakpoint should all be at the base rate."""
    assert PricingConfig.calculate_total_pence(100) == 1000  # 100 x 10p


def test_price_at_first_threshold_boundary() -> None:
    """Exactly 499 tokens should be at the base rate."""
    assert PricingConfig.calculate_total_pence(499) == 4990  # 499 x 10p


def test_price_at_500_tokens() -> None:
    """500 tokens: 499 at 10p + 1 at 9p = 4999p."""
    assert PricingConfig.calculate_total_pence(500) == 4999


def test_price_at_999_tokens() -> None:
    """999 tokens: 499 at 10p + 500 at 9p = 9490p."""
    assert PricingConfig.calculate_total_pence(999) == 9490


def test_price_at_1000_tokens() -> None:
    """1000 tokens: 499 at 10p + 500 at 9p + 1 at 8p = 9498p."""
    assert PricingConfig.calculate_total_pence(1000) == 9498


def test_price_at_1200_tokens() -> None:
    """1200 tokens: 499 at 10p + 500 at 9p + 201 at 8p = 11098p."""
    assert PricingConfig.calculate_total_pence(1200) == 11098


def test_price_is_monotonic() -> None:
    """Every additional token must always cost more total."""
    prev = 0
    for n in range(1, 10001):
        price = PricingConfig.calculate_total_pence(n)
        assert price > prev, f"Price dropped at {n}: {price} <= {prev}"
        prev = price


def test_effective_rate_pence() -> None:
    """Effective rate for graduated pricing rounded to 2dp."""
    # 1200 tokens = 11098p -> 11098 / 1200 = 9.248... -> 9.25
    assert PricingConfig.effective_rate_pence(1200) == 9.25


def test_effective_rate_pence_no_discount() -> None:
    """Below first threshold, effective rate is the base rate."""
    assert PricingConfig.effective_rate_pence(100) == 10.0


def test_tiers_as_json_returns_serialisable_list() -> None:
    """tiers_as_json must return a list of dicts for template injection."""
    import json

    tiers_json = PricingConfig.tiers_as_json()
    # Must be valid JSON
    parsed = json.loads(tiers_json)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    # Each tier has threshold and rate_pence
    for tier in parsed:
        assert "up_to" in tier
        assert "rate_pence" in tier


# --- Subscription tier config tests ---


def test_subscription_tiers_exist() -> None:
    """Three subscription tiers should be defined."""
    assert len(SUBSCRIPTION_TIERS) == 3


def test_tier_50_pricing() -> None:
    """50-page tier: 9p/page = 450p/month."""
    tier = SUBSCRIPTION_TIERS["tier_50"]
    assert tier.tokens_per_month == 50
    assert tier.rate_pence == 9
    assert tier.monthly_price_pence == 450
    assert tier.display_name == "50 Pages/mo"


def test_tier_200_pricing() -> None:
    """200-page tier: 8p/page = 1600p/month."""
    tier = SUBSCRIPTION_TIERS["tier_200"]
    assert tier.tokens_per_month == 200
    assert tier.rate_pence == 8
    assert tier.monthly_price_pence == 1600
    assert tier.display_name == "200 Pages/mo"


def test_tier_500_pricing() -> None:
    """500-page tier: 7p/page = 3500p/month."""
    tier = SUBSCRIPTION_TIERS["tier_500"]
    assert tier.tokens_per_month == 500
    assert tier.rate_pence == 7
    assert tier.monthly_price_pence == 3500
    assert tier.display_name == "500 Pages/mo"


def test_subscription_tier_has_stripe_price_id() -> None:
    """Each tier must have a non-empty stripe_price_id loaded from env."""
    for tier_id, tier in SUBSCRIPTION_TIERS.items():
        assert tier.stripe_price_id, f"{tier_id} missing stripe_price_id"


def test_get_tier_by_id_returns_none_for_unknown() -> None:
    """Unknown tier ID should return None."""
    assert SUBSCRIPTION_TIERS.get("tier_999") is None


def test_subscription_tiers_as_json() -> None:
    """Tiers should serialise to JSON for template injection."""
    result = json.loads(PricingConfig.subscription_tiers_as_json())
    assert isinstance(result, list)
    assert len(result) == 3
    for tier in result:
        assert "tier_id" in tier
        assert "display_name" in tier
        assert "tokens_per_month" in tier
        assert "rate_pence" in tier
        assert "monthly_price_pence" in tier
