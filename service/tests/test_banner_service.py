"""Unit tests for the banner registry."""

from __future__ import annotations

from banner_service import Banner, get_banners, register_banner_provider


def test_banner_dataclass_defaults() -> None:
    """Banner should default to non-dismissible with no link."""
    banner = Banner(message="Hello", alert_type="info")
    assert banner.message == "Hello"
    assert banner.alert_type == "info"
    assert banner.link_text is None
    assert banner.link_url is None
    assert banner.dismissible is False
    assert banner.dismiss_key is None


def test_get_banners_returns_non_none_results(monkeypatch) -> None:
    """Providers returning None should be filtered out."""
    monkeypatch.setattr("banner_service._providers", [])

    def _always(_tid: str) -> Banner:
        return Banner(message="visible", alert_type="info")

    def _never(_tid: str) -> None:
        return None

    register_banner_provider(_always)
    register_banner_provider(_never)

    banners = get_banners("tenant-1", dismissed_keys=set())
    assert len(banners) == 1
    assert banners[0].message == "visible"


def test_get_banners_filters_dismissed(monkeypatch) -> None:
    """Banners whose dismiss_key is in the dismissed set should be excluded."""
    monkeypatch.setattr("banner_service._providers", [])

    def _dismissible(_tid: str) -> Banner:
        return Banner(message="dismiss me", alert_type="success", dismissible=True, dismiss_key="test-key")

    def _not_dismissible(_tid: str) -> Banner:
        return Banner(message="always show", alert_type="info")

    register_banner_provider(_dismissible)
    register_banner_provider(_not_dismissible)

    banners = get_banners("tenant-1", dismissed_keys={"test-key"})
    assert len(banners) == 1
    assert banners[0].message == "always show"


def test_get_banners_keeps_undismissed_dismissible(monkeypatch) -> None:
    """Dismissible banners should show when not in the dismissed set."""
    monkeypatch.setattr("banner_service._providers", [])

    def _dismissible(_tid: str) -> Banner:
        return Banner(message="still here", alert_type="success", dismissible=True, dismiss_key="other-key")

    register_banner_provider(_dismissible)

    banners = get_banners("tenant-1", dismissed_keys={"unrelated-key"})
    assert len(banners) == 1
    assert banners[0].message == "still here"
