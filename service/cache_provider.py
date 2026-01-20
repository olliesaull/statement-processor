"""Shared cache accessors used by the statement processor service."""

from flask_caching import Cache

from config import logger

_STATUS_SUFFIX = "_status"


_CACHE_STATE: dict[str, Cache | None] = {"cache": None}


def set_cache(instance: Cache) -> None:
    """Register the shared cache instance."""
    _CACHE_STATE["cache"] = instance


def get_cache() -> Cache | None:
    """Return the shared cache instance if configured."""
    return _CACHE_STATE["cache"]


def set_tenant_status_cache(tenant_id: str, status_value: str) -> None:
    """Write the tenant status to cache if a cache is configured."""
    cache_instance = get_cache()
    if not tenant_id or cache_instance is None:
        return

    cache_instance.set(f"{tenant_id}{_STATUS_SUFFIX}", status_value)
    logger.info("Updated Cache", tenant_id=tenant_id, tenant_status=status_value)
