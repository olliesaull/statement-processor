"""Redis cache for precomputed statement detail view data.

Caches the fully-built statement rows and display headers so that HTMX
partial swaps (filter toggles, pagination) skip the entire build pipeline
(S3 fetch, Xero data load, matching, classification, row building) and
go straight to filtering + rendering.

Cache key:  stmt_view:{tenant_id}:{generation}:{statement_id}
TTL:        120 seconds — long enough for a browsing session on one
            statement, short enough that stale data self-corrects.

Invalidation strategy:
  - Explicit delete: called on every POST to /statement/<id> (item
    completion status changes).
  - Generation bump: called after Xero sync completes, which atomically
    invalidates all cached statements for the tenant without needing to
    know statement IDs.
  - Passive TTL expiry: 120-second self-correction for any mutation path
    not covered above.
  - Excel downloads bypass the cache entirely (handled by the caller,
    not this module) because they need intermediate pipeline data not
    stored in the cached dict.

Note: item completion status (is_completed per row) is embedded in cached
rows and is NOT recalculated on cache hit.  Any new mutation path that
changes item status must call invalidate_statement_view_cache or
bump_tenant_generation.
"""

import dataclasses
import json
from typing import Any

from config import redis_client
from logger import logger


class _DataclassEncoder(json.JSONEncoder):
    """JSON encoder that converts dataclass instances to dicts.

    CellComparison objects in the cached statement rows are frozen
    dataclasses.  After a round-trip through JSON they come back as
    plain dicts, which is fine — all consumers access them via dict
    notation (template filters, list comprehensions).
    """

    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        return super().default(o)


_CACHE_TTL_SECONDS = 120

# Maximum serialised cache entry size.  At ~1.4 KB per statement row
# (8 display headers x ~150 bytes/CellComparison + ~200 bytes metadata),
# 1.5 MB covers approximately 1,000 statement items.  Entries above this
# threshold are not cached — the pipeline re-runs each time.  Adjust if
# real-world statements regularly exceed 1,000 items.
_MAX_CACHE_SIZE_BYTES = 1_500_000  # 1.5 MB

_KEY_PREFIX = "stmt_view"
_GENERATION_PREFIX = "tenant_gen"


def _tenant_generation(tenant_id: str) -> int:
    """Return the current cache generation for a tenant (0 if unset or error).

    The generation counter is incremented after each Xero sync so that
    stale per-tenant cache entries become unreachable without requiring a
    SCAN or knowing individual statement IDs.
    """
    try:
        val = redis_client.get(f"{_GENERATION_PREFIX}:{tenant_id}")
        return int(val) if val else 0
    except Exception:
        return 0


def bump_tenant_generation(tenant_id: str) -> None:
    """Increment the tenant generation counter, invalidating all cached views.

    Called after Xero sync completes — new cache keys will include the
    bumped generation, so pre-sync entries expire naturally via TTL.
    """
    try:
        redis_client.incr(f"{_GENERATION_PREFIX}:{tenant_id}")
        logger.info("Bumped tenant cache generation", tenant_id=tenant_id)
    except Exception:
        logger.exception("Failed to bump tenant cache generation", tenant_id=tenant_id)


def _cache_key(tenant_id: str, statement_id: str) -> str:
    """Build the Redis key for a statement view cache entry.

    Includes the tenant generation counter so that a sync bump atomically
    invalidates all cached statements for the tenant.
    """
    gen = _tenant_generation(tenant_id)
    return f"{_KEY_PREFIX}:{tenant_id}:{gen}:{statement_id}"


def get_cached_statement_view(tenant_id: str, statement_id: str) -> dict[str, Any] | None:
    """Retrieve cached statement view data.

    Returns the cached dict on hit, or None on miss or error.
    Errors are logged but never raised — a cache failure just means
    the pipeline re-runs.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        raw = redis_client.get(key)
        if raw is None:
            # Cache miss — intentionally silent (no log) to avoid noise;
            # misses are the normal path on first load.
            return None
        data: dict[str, Any] = json.loads(raw)
        logger.info("Statement view cache hit", tenant_id=tenant_id, statement_id=statement_id)
        return data
    except Exception:
        logger.exception("Failed to read statement view cache", key=key)
        return None


def cache_statement_view(tenant_id: str, statement_id: str, view_data: dict[str, Any]) -> None:
    """Store statement view data in Redis with a TTL.

    Entries larger than _MAX_CACHE_SIZE_BYTES are skipped to avoid
    memory pressure on the Valkey instance for unusually large statements.

    Errors are logged but never raised — a cache write failure is
    non-fatal; the next request will just rebuild from scratch.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        serialised = json.dumps(view_data, cls=_DataclassEncoder)
        size_bytes = len(serialised.encode("utf-8"))
        size_kb = round(size_bytes / 1024, 1)

        if size_bytes > _MAX_CACHE_SIZE_BYTES:
            logger.warning("Statement view too large to cache; skipping", tenant_id=tenant_id, statement_id=statement_id, size_kb=size_kb, max_kb=round(_MAX_CACHE_SIZE_BYTES / 1024, 1))
            return

        redis_client.setex(key, _CACHE_TTL_SECONDS, serialised)
        logger.info("Statement view cached", tenant_id=tenant_id, statement_id=statement_id, size_kb=size_kb)
    except Exception:
        logger.exception("Failed to write statement view cache", key=key)


def invalidate_statement_view_cache(tenant_id: str, statement_id: str) -> None:
    """Remove cached statement view data.

    Called after POST actions that change item completion status,
    so the next GET rebuilds with fresh data from DynamoDB.
    """
    key = _cache_key(tenant_id, statement_id)
    try:
        redis_client.delete(key)
        logger.info("Statement view cache invalidated", tenant_id=tenant_id, statement_id=statement_id)
    except Exception:
        logger.exception("Failed to invalidate statement view cache", key=key)
