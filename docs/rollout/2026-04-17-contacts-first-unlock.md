# Rollout — Contacts-first unlock + per-resource progress

Merge commit: `1b0ac71`. Feature branch: `feature/contacts-first-unlock`.

## 1. Staging deploy

Deploy to staging. No schema changes in CDK — additive DDB attributes only.

## 2. Backfill existing tenants

`ReconcileReadyAt` is new. Every existing `FREE` tenant lacks it and will hit the not-ready gate on `/statement/<id>` until backfilled.

```bash
# Dry run — print candidate count and sample rows.
python3.13 scripts/backfill_reconcile_ready/backfill_reconcile_ready.py --dry-run

# Live run — interactive confirmation prompt; use --yes in CI/headless contexts.
python3.13 scripts/backfill_reconcile_ready/backfill_reconcile_ready.py
```

Idempotent (`ConditionExpression: attribute_not_exists(ReconcileReadyAt)`). Safe to re-run. Detail in `scripts/backfill_reconcile_ready/README.md`.

## 3. Staging smoke (per plan Step 14)

Must hit these user flows manually with a real Xero tenant on staging before promoting to prod:

- **Backfilled tenant** — `/tenant_management` shows progress panel "all done"; `/statement/<id>` renders normally.
- **Fresh connect** — contacts phase gates nav → unlocks → heavy phase runs with live % → reconcile becomes available.
- **Partial failure** — kill worker mid-heavy phase → `LOAD_INCOMPLETE` → Retry sync recovers.
- **Index-only retry** — new path: if only `PerContactIndexProgress=failed`, Retry sync rebuilds the index without re-fetching (added during review).
- **Wait → HX-Redirect** — navigate to `/statement/<id>` during heavy phase → not-ready view → auto-redirect on completion without manual reload.
- **Multi-tenant** — two tenants in session, one synced, one mid-sync → panel renders both correctly.
- **Concurrent sync (no 409)** — second `/api/tenants/<id>/sync` POST before the first completes returns 200 with the panel fragment; the worker-side `try_acquire_sync` inside `sync_data` silently drops the overlap with a `WARNING "Sync already in flight; skipping overlapping start"` log from `sync_data`. The user sees no error toast. This is the intended UX: benign double-clicks and background retries must not surface as errors.
- **Concurrent retry-sync 409** — overlap on `/api/tenants/<id>/retry-sync` IS rejected with 409 + `htmx:responseError` toast, because retry-sync synchronously acquires the lock before executor submission. Retry is an explicit recovery action the caller expects to observe the outcome of.
- **`HX-Redirect` through CloudFront** — `curl -i` `/statement/<id>/wait` as an authed user against the staging CloudFront domain; `HX-Redirect` header must reach the client.

## 4. Production

Deploy → re-run backfill (idempotent) → watch CloudWatch for:
- New `"Xero pagination metadata"` info logs (confirm `item_count` populated for invoices/credit_notes/payments).
- `/tenants/sync-progress` 5xx rate.
- Any `"Failed to release sync lock after submission failure"` logs (indicates the new retry-sync rollback path fired).

## Rollback

Revert the merge commit. Backfill-written fields (`ReconcileReadyAt`, `*Progress`, `LastFullLoadCompletedAt`) are harmless to the prior code — leave them in place. No DDB cleanup needed.
