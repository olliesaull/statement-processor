# Statement Page Performance Optimizations

**Date:** 2026-04-09
**Scope:** `/statement/<id>` detail page — server-side response time

## Problem

The `/statement/<id>` route takes 320-600 ms per request. Every interaction — initial load, HTMX filter toggles (show/hide payments, incomplete/completed/all), pagination, and mark-complete POST — re-executes the full pipeline: load Xero data from disk/S3, match invoices to statement items, classify items, build view models, then filter and paginate.

HTMX swaps (toggling filters, paginating) re-run all of this even though the underlying data hasn't changed. The browser renders in 15-46 ms — the bottleneck is entirely server-side.

### Measured Timings (478-item statement, local dev)

| Scenario | Server | Render | Total |
|----------|--------|--------|-------|
| Initial page load | 598 ms | 46 ms | 644 ms |
| Hide/show payments | 320-340 ms | 16-35 ms | 355 ms |
| View all items | 345 ms | 14 ms | 359 ms |
| Mark complete (POST) | 404 ms | 15 ms | 419 ms |

## Two Optimizations

### 1. Redis Cache for HTMX Swaps

Cache the fully-built statement view data in Redis so that HTMX partial swaps (filter toggles, pagination) skip the entire build pipeline and just filter + paginate from the cache.

### 2. Per-Contact Xero Data Files in S3

Pre-index Xero data by contact at sync time so the initial page load reads a small per-contact file instead of loading the full tenant dataset and filtering in-memory.

---

## Optimization 1: Redis HTMX Swap Cache

### What Gets Cached

After the initial page load completes the full pipeline (Xero data load, matching, classification, view model building), store the computed results in Redis:

- `statement_rows` — all rows (unfiltered, unpaginated) with cell comparisons, match status, completion status, item types
- `display_headers` — column headers for rendering
- `completed_count`, `incomplete_count`, `has_payment_rows` — aggregate counts
- `is_completed`, `is_processing`, `processing_failed` — statement-level status
- `contact_name`, `page_heading` — display metadata

This is everything the template needs. On cache hit, the route skips steps 1-4 (Xero load, matching, classification, row building) and goes straight to filtering + pagination.

### Cache Key and TTL

- **Key:** `statement_view:{tenant_id}:{statement_id}`
- **TTL:** 120 seconds — long enough to cover a typical browsing session on one statement, short enough that stale data self-corrects
- **Serialization:** JSON (statement_rows are already plain dicts)

### Cache Invalidation

- **Mark complete (POST):** Invalidate the cache, then re-run the full pipeline. The POST changes item status which affects filtering and counts.
- **TTL expiry:** Natural fallback — if the cache expires, next request rebuilds it.
- **Xero data sync:** No explicit invalidation needed. The 120s TTL means any sync that updates Xero data will be picked up within 2 minutes. If tighter consistency is needed later, the sync can broadcast invalidation.

### Request Flow

```
GET /statement/<id>?items_view=incomplete&show_payments=true

1. Check Redis for statement_view:{tenant_id}:{statement_id}
2a. CACHE HIT:
    - Deserialize cached statement_rows + metadata
    - Filter by items_view and show_payments
    - Paginate
    - Render template (~20-50 ms total)
2b. CACHE MISS:
    - Run full pipeline (Xero load, match, classify, build rows)
    - Store result in Redis with 120s TTL
    - Filter, paginate, render (~300-600 ms, same as current)

POST /statement/<id> (mark complete):
    1. Process the POST action (update DynamoDB item status)
    2. Delete Redis cache key
    3. Run full pipeline to rebuild with updated status
    4. Store new result in Redis
    5. Filter, paginate, render
```

### Implementation

Uses the existing `redis` dependency (already imported in app.py for sessions). The caching logic is simple — one key, get/set/delete with TTL — so no additional library is needed:

```python
# Cache hit
cached = redis_client.get(cache_key)
if cached:
    view_data = json.loads(cached)
    # filter + paginate + render

# Cache set (after full pipeline)
redis_client.setex(cache_key, 120, json.dumps(view_data))

# Cache invalidate (on POST)
redis_client.delete(cache_key)
```

No new dependency needed.

### Projected Improvement

| Scenario | Current | With cache |
|----------|---------|------------|
| Initial load | ~600 ms | ~600 ms (cache miss, builds + stores) |
| HTMX swap (filter/paginate) | ~350 ms | **~20-50 ms** (cache hit, filter + render) |
| Mark complete (POST) | ~420 ms | ~420 ms (invalidate + rebuild) |

---

## Optimization 2: Per-Contact Xero Data in S3

### Problem

`get_invoices_by_contact()`, `get_credit_notes_by_contact()`, and `get_payments_by_contact()` each load the **entire tenant dataset** from disk/S3, then filter in-memory by contact_id. For large tenants this means reading and parsing potentially megabytes of JSON to extract the subset for one contact.

### Solution

At Xero sync time, write a combined per-contact file containing invoices, credit notes, and payments for that contact. The statement page reads this single small file instead of three large ones.

### S3 File Structure

Existing files are unchanged:

```
{tenant_id}/data/
├── invoices.json              # existing, unchanged
├── credit_notes.json          # existing, unchanged
├── payments.json              # existing, unchanged
├── contacts.json              # existing, unchanged
└── xero_by_contact/           # NEW
    ├── {contact_id_1}.json
    ├── {contact_id_2}.json
    └── ...
```

### Per-Contact File Format

```json
{
    "invoices": [...],
    "credit_notes": [...],
    "payments": [...]
}
```

Each list contains only the documents for that contact. Empty lists for document types with no data for that contact.

### Sync-Time Indexing

In `sync.py`, after writing the existing flat files, add a step to group by contact_id and write per-contact files:

1. After `sync_invoices`, `sync_credit_notes`, and `sync_payments` complete, load all three datasets from local disk (already cached from the sync)
2. Group each dataset by `contact_id` using a dict of lists
3. Merge the three groupings into combined per-contact dicts
4. Write each combined dict to `{tenant_id}/data/xero_by_contact/{contact_id}.json` — local disk + S3

This runs once per sync, not per page load. The sync already has all the data in memory.

### Incremental Sync Consideration

The existing sync supports incremental updates (`modified_since`). During incremental sync, the full flat files (`invoices.json`, etc.) are updated in-place with the changed records. After this update completes, rebuild all per-contact files from the updated flat files. This is simpler than tracking which contacts were affected and merging deltas, and the cost is just local JSON grouping + S3 PUTs.

### Reading Per-Contact Data

Add a new function to `xero_repository.py`:

```python
def get_xero_data_by_contact(contact_id: str) -> dict:
    """Load combined Xero data for a single contact.

    Returns dict with keys: invoices, credit_notes, payments.
    Falls back to loading full datasets and filtering if
    per-contact file doesn't exist (backward compatibility).
    """
```

The fallback ensures existing statements (uploaded before this change) work without requiring a full re-sync.

### Local Disk Caching

Follows the same pattern as existing Xero data:
- First check: `LOCAL_DATA_DIR/{tenant_id}/xero_by_contact/{contact_id}.json`
- Fallback: Download from S3, cache locally
- The local cache is cleaned up during tenant disconnection (`shutil.rmtree` on tenant dir)

### Tenant Data Erasure

No changes needed. The tenant erasure lambda deletes by prefix `{tenant_id}/`, which covers everything including `{tenant_id}/data/xero_by_contact/`.

### Projected Improvement

| Scenario | Current | With per-contact files |
|----------|---------|----------------------|
| Xero data load (large tenant) | ~150-200 ms | **~10-30 ms** |
| Xero data load (small tenant) | ~50-80 ms | ~10-30 ms |

This saving applies to the initial page load. Combined with the Redis cache, HTMX swaps skip Xero loading entirely.

---

## Combined Projected Timings

| Scenario | Current | With both optimizations |
|----------|---------|------------------------|
| Initial load (cache miss) | ~600 ms | **~300-400 ms** |
| HTMX swap (cache hit) | ~350 ms | **~20-50 ms** |
| Mark complete (POST) | ~420 ms | **~250-300 ms** |

---

## What's Not Changing

- Classification still runs on every cache miss (initial load, after POST). Moving it to the extraction lambda is deferred.
- Invoice matching still runs on every cache miss. The per-contact Xero data just makes it faster to load the inputs.
- The full flat files (`invoices.json`, etc.) are still written during sync and used by other parts of the app.
- Template rendering, pagination logic, and HTMX partial structure are unchanged.

## Dependencies

- No new production dependencies. Uses existing `redis` import for caching.
- `flask-caching` is not needed.

## Deployment Notes

- **Backward compatible.** Statements uploaded before this change fall back to loading full datasets — no re-sync required.
- **Nginx config:** No changes. No new routes or query parameters.
- **Dockerfile:** No new directories to COPY (changes are within existing `service/` tree).
