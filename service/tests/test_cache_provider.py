"""Unit tests for cache provider helpers."""

from typing import Any

import cache_provider


class DummyCache:
    """Test double that records cache set calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []

    def set(self, key: str, value: str, timeout: int | None = None) -> None:
        self.calls.append((key, value, timeout))


def test_set_tenant_status_cache_uses_explicit_timeout(monkeypatch: Any) -> None:
    dummy_cache = DummyCache()
    monkeypatch.setitem(cache_provider._CACHE_STATE, "cache", dummy_cache)

    cache_provider.set_tenant_status_cache("tenant-123", "LOADING")

    assert dummy_cache.calls == [("tenant-123_status", "LOADING", cache_provider._TENANT_STATUS_CACHE_TIMEOUT_SECONDS)]


def test_set_tenant_status_cache_skips_empty_tenant_id(monkeypatch: Any) -> None:
    dummy_cache = DummyCache()
    monkeypatch.setitem(cache_provider._CACHE_STATE, "cache", dummy_cache)

    cache_provider.set_tenant_status_cache("", "FREE")

    assert dummy_cache.calls == []
