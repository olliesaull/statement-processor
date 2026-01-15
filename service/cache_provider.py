"""Shared cache accessors used by the statement processor service."""

from flask_caching import Cache

from config import logger

_STATUS_SUFFIX = "_status"


cache: Cache | None = None


def set_cache(instance: Cache) -> None:
    """Register the shared cache instance."""
    global cache
    cache = instance


def set_tenant_status_cache(tenant_id: str, status_value: str) -> None:
    """Write the tenant status to cache if a cache is configured."""
    if not tenant_id or cache is None:
        return

    cache.set(f"{tenant_id}{_STATUS_SUFFIX}", status_value)
    logger.info("Updated Cache", tenant_id=tenant_id, tenant_status=status_value)
