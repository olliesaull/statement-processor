# Backfill `ReconcileReadyAt` + `*Progress` (one-shot migration)

Before the contacts-first-unlock change, tenant sync state was tracked only with `TenantStatus` + `LastSyncTime`. The new code gates `/statement/<id>` on `ReconcileReadyAt` and renders per-resource progress from `{Contacts,CreditNotes,Invoices,Payments,PerContactIndex}Progress`. Legacy rows written by the old code path render as "not ready" indefinitely until backfilled.

This script writes the new attributes for every row that is provably post-initial-load (`TenantStatus == FREE` with a non-null `LastSyncTime`) but missing `ReconcileReadyAt`:

- `ReconcileReadyAt = LastSyncTime` (so the statement gate unblocks immediately).
- `ContactsProgress` / `CreditNotesProgress` / `InvoicesProgress` / `PaymentsProgress` = `{status: complete, records_fetched: null, record_total: null, updated_at: LastSyncTime}`.
- `PerContactIndexProgress` = `{status: complete, updated_at: LastSyncTime}`.

`LastFullLoadCompletedAt` is written only when not already set (via `if_not_exists`).

## Why a script and not a runtime lazy backfill

Running this as a deploy-time script keeps the write path explicit (easy to audit / rollback) and avoids leaving a permanent check on every `reconcile_ready_required` request for a one-shot repair. See the decision log entry in `docs/decisions/log.md`.

## Idempotency

Every `UpdateItem` uses `ConditionExpression: attribute_not_exists(ReconcileReadyAt)`. Re-running the script after a partial run (or racing a live sync) is a no-op on rows that are already fully set.

## Setup

Run from inside this directory:

```bash
cd /home/ollie/statement-processor/scripts/backfill_reconcile_ready
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## How to run

Always dry-run first to see how many rows are affected and a sample of candidates:

```bash
cd /home/ollie/statement-processor/scripts/backfill_reconcile_ready
source venv/bin/activate
python3.13 backfill_reconcile_ready.py --dry-run
```

Then commit:

```bash
python3.13 backfill_reconcile_ready.py            # interactive confirmation
python3.13 backfill_reconcile_ready.py --yes      # skip confirmation (CI / one-liner)
```

## Notes

- Loads `../../service/.env` by default. Same caveats as `manual_token_adjustment/`: importing `config.py` triggers SSM secret fetching and opens a Valkey connection, so the env file must point at valid SSM params and a reachable Valkey endpoint.
- Safe to run against prod **after** the app has been deployed with the new schema readers — running before the new readers exist just writes no-op attributes.
- Rollback: reverting the PR is enough. The additive attributes this script writes are harmless to older code paths.
