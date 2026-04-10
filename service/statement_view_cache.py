"""Redis cache for precomputed statement detail view data.

Caches the fully-built statement rows and display headers so that HTMX
partial swaps (filter toggles, pagination) skip the entire build pipeline
(S3 fetch, Xero data load, matching, classification, row building) and
go straight to filtering + rendering.

Cache key:  stmt_view:{tenant_id}:{statement_id}
TTL:        120 seconds — long enough for a browsing session on one
            statement, short enough that stale data self-corrects.
"""

import json
from typing import Any

import redis as redis_lib

from config import VALKEY_URL
from logger import logger

_CACHE_TTL_SECONDS = 120
_KEY_PREFIX = "stmt_view"

# Lazy connection pool — connects on first use, not on import.
_redis: redis_lib.Redis = redis_lib.from_url(VALKEY_URL)


def _cache_key(tenant_id: str, statement_id: str) -> str:
    """Build the Redis key for a statement view cache entry."""
    return f"{_KEY_PREFIX}:{tenant_id}:{statement_id}"


def get_cached_statement_view(tenant_id: str, statement_id: str) -> dict[str, Any] | None:
    """Retrieve cached statement view data.

    Returns the cached dict on hit, or None on miss or error.
    Errors are logged but never raised — a cache failure just means
    the pipeline re-runs.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        raw = _redis.get(key)
        if raw is None:
            return None
        data: dict[str, Any] = json.loads(raw)
        logger.info("Statement view cache hit", tenant_id=tenant_id, statement_id=statement_id)
        return data
    except Exception:
        logger.exception("Failed to read statement view cache", key=key)
        return None


def cache_statement_view(tenant_id: str, statement_id: str, view_data: dict[str, Any]) -> None:
    """Store statement view data in Redis with a TTL.

    Errors are logged but never raised — a cache write failure is
    non-fatal; the next request will just rebuild from scratch.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        _redis.setex(key, _CACHE_TTL_SECONDS, json.dumps(view_data))
        logger.info("Statement view cached", tenant_id=tenant_id, statement_id=statement_id)
    except Exception:
        logger.exception("Failed to write statement view cache", key=key)


def invalidate_statement_view_cache(tenant_id: str, statement_id: str) -> None:
    """Remove cached statement view data.

    Called after POST actions that change item completion status,
    so the next GET rebuilds with fresh data from DynamoDB.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        _redis.delete(key)
        logger.info("Statement view cache invalidated", tenant_id=tenant_id, statement_id=statement_id)
    except Exception:
        logger.exception("Failed to invalidate statement view cache", key=key)
