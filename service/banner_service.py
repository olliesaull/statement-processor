"""Reusable banner registry for site-wide notifications.

Banners are provided by registered callable providers. Each provider
receives a tenant ID and returns a Banner or None. The registry
collects non-None results and filters out any whose dismiss_key
has been permanently dismissed by the tenant.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from core.config_suggestion import get_pending_suggestion_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Banner:
    """A single site-wide notification banner.

    Attributes:
        message: Text shown in the banner body.
        alert_type: Bootstrap alert variant (info, success, warning, danger).
        link_text: Optional call-to-action label.
        link_url: Optional URL for the call-to-action link.
        dismissible: Whether the user can permanently dismiss this banner.
        dismiss_key: Unique key for persistence. Required when dismissible.
    """

    message: str
    alert_type: str
    link_text: str | None = None
    link_url: str | None = None
    dismissible: bool = False
    dismiss_key: str | None = None


# Provider protocol: (tenant_id) -> Banner | None
BannerProvider = Callable[[str], Banner | None]

_providers: list[BannerProvider] = []


def register_banner_provider(provider: BannerProvider) -> None:
    """Add a banner provider to the global registry."""
    _providers.append(provider)


def get_banners(tenant_id: str, dismissed_keys: set[str]) -> list[Banner]:
    """Collect active banners for a tenant, filtering dismissed ones.

    Args:
        tenant_id: Tenant to evaluate banners for.
        dismissed_keys: Set of dismiss_key values the tenant has dismissed.

    Returns:
        List of banners that should be displayed.
    """
    banners: list[Banner] = []
    for provider in _providers:
        banner = provider(tenant_id)
        if banner is None:
            continue
        # Skip dismissed banners.
        if banner.dismiss_key and banner.dismiss_key in dismissed_keys:
            continue
        banners.append(banner)
    return banners


def config_review_banner_provider(tenant_id: str) -> Banner | None:
    """Banner provider for pending config review notifications.

    Replaces the former inject_pending_review_count context processor.
    Caching is handled by the context processor caller, not here.

    Args:
        tenant_id: Tenant to check for pending suggestions.

    Returns:
        Info banner with count and link to configs, or None if no pending reviews.
    """
    try:
        count = get_pending_suggestion_count(tenant_id)
    except Exception:
        logger.exception("Failed to fetch pending suggestion count", extra={"tenant_id": tenant_id})
        return None

    if count <= 0:
        return None

    # Match the original grammar: "1 statement needs" vs "3 statements need"
    plural_s = "s" if count != 1 else ""
    verb = "needs" if count == 1 else "need"

    return Banner(message=f"{count} statement{plural_s} {verb} config review.", alert_type="info", link_text="Review now", link_url="/configs", dismissible=False)


register_banner_provider(config_review_banner_provider)
