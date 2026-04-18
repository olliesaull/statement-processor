#!/usr/bin/env python3.13
"""Backfill ``ReconcileReadyAt`` + per-resource progress maps for legacy tenants.

Before the contacts-first-unlock change, tenants tracked sync state with a
single ``TenantStatus`` + ``LastSyncTime`` pair. The new UI gates
``/statement/<id>`` on ``ReconcileReadyAt`` (and renders per-resource progress
from ``{Contacts,CreditNotes,Invoices,Payments,PerContactIndex}Progress``), so
rows written by the old code path render as "not ready" indefinitely after
the deploy.

This script finds every tenant row that is demonstrably past an initial load
(``TenantStatus=FREE`` with a non-null ``LastSyncTime``) but lacks
``ReconcileReadyAt``, and seeds:

- ``ReconcileReadyAt = LastSyncTime`` (authoritative gate).
- ``ContactsProgress`` / ``InvoicesProgress`` / ``CreditNotesProgress`` /
  ``PaymentsProgress`` set to ``{status: complete, updated_at: LastSyncTime,
  records_fetched: null, record_total: null}``.
- ``PerContactIndexProgress = {status: complete, updated_at: LastSyncTime}``.

The operation is idempotent: a DynamoDB ``ConditionExpression`` guards every
write so re-running is safe. ``--dry-run`` skips writes and prints a sample.

Usage:
    python3.13 scripts/backfill_reconcile_ready/backfill_reconcile_ready.py
    python3.13 scripts/backfill_reconcile_ready/backfill_reconcile_ready.py --dry-run
    python3.13 scripts/backfill_reconcile_ready/backfill_reconcile_ready.py --yes
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = REPO_ROOT / "service"
COMMON_DIR = REPO_ROOT / "common"
DEFAULT_ENV_FILE = SERVICE_DIR / ".env"

_RESOURCE_PROGRESS_KEYS: tuple[str, ...] = ("ContactsProgress", "CreditNotesProgress", "InvoicesProgress", "PaymentsProgress")
_PER_CONTACT_INDEX_KEY = "PerContactIndexProgress"
_SAMPLE_LIMIT = 5


def _build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser for the backfill tool."""
    parser = argparse.ArgumentParser(description="Backfill ReconcileReadyAt + *Progress maps for legacy tenant rows.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts and a sample of candidate rows, but do not write.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt (only applies when writing).")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Path to the service env file (default: %(default)s).")
    return parser


def _load_environment(env_file: Path) -> None:
    """Load the service environment before importing service modules."""
    if env_file.exists():
        load_dotenv(env_file, override=False)
        print(f"Loaded environment from {env_file}")
    else:
        print(f"Env file not found at {env_file}; using existing shell environment only.")


def needs_backfill(item: dict[str, Any]) -> bool:
    """Return True when a TenantData row should be backfilled.

    The filter mirrors the intent documented in the module docstring:
    ``TenantStatus`` is ``FREE``, ``LastSyncTime`` has been written (proving an
    initial load completed under the old code), and ``ReconcileReadyAt`` has
    not. Any row failing any of these conditions is skipped — including rows
    already carrying ``ReconcileReadyAt``, which makes re-running idempotent.
    """
    status = item.get("TenantStatus")
    if isinstance(status, str):
        status = status.strip().upper()
    if status != "FREE":
        return False
    if item.get("LastSyncTime") in (None, ""):
        return False
    return item.get("ReconcileReadyAt") is None


def iter_tenant_items(table, scan_kwargs: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
    """Yield every tenant row from DynamoDB, paginating through ``LastEvaluatedKey``.

    Intentionally fetches the whole row — the backfill needs ``LastSyncTime``
    and existing ``*Progress`` keys (if any) to avoid overwriting partial
    state the app may have written between deploy + backfill.
    """
    kwargs = dict(scan_kwargs or {})
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", []) or []
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key


def build_update_kwargs(tenant_id: str, last_sync_time: Any) -> dict[str, Any]:
    """Build ``UpdateItem`` kwargs for a single tenant row.

    Uses a ``ConditionExpression`` on ``attribute_not_exists(ReconcileReadyAt)``
    so a concurrent sync writing ``ReconcileReadyAt`` between scan and write
    is a no-op rather than a silent overwrite.
    """
    progress_payload = {"status": "complete", "records_fetched": None, "record_total": None, "updated_at": last_sync_time}
    index_payload = {"status": "complete", "updated_at": last_sync_time}

    set_clauses = ["ReconcileReadyAt = :reconcile_ready_at", "LastFullLoadCompletedAt = if_not_exists(LastFullLoadCompletedAt, :reconcile_ready_at)"]
    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {":reconcile_ready_at": last_sync_time, ":per_contact_index": index_payload}

    for key in _RESOURCE_PROGRESS_KEYS:
        alias = f"#{key}"
        value_ref = f":{key}"
        set_clauses.append(f"{alias} = if_not_exists({alias}, {value_ref})")
        expr_names[alias] = key
        expr_values[value_ref] = progress_payload

    # PerContactIndexProgress: same "don't clobber" contract.
    set_clauses.append(f"#{_PER_CONTACT_INDEX_KEY} = if_not_exists(#{_PER_CONTACT_INDEX_KEY}, :per_contact_index)")
    expr_names[f"#{_PER_CONTACT_INDEX_KEY}"] = _PER_CONTACT_INDEX_KEY

    return {
        "Key": {"TenantID": tenant_id},
        "UpdateExpression": "SET " + ", ".join(set_clauses),
        "ExpressionAttributeNames": expr_names,
        "ExpressionAttributeValues": expr_values,
        "ConditionExpression": "attribute_not_exists(ReconcileReadyAt)",
    }


def collect_candidates(table: Any) -> list[dict[str, Any]]:
    """Scan the table once and return every row that still needs backfilling.

    Kept separate from ``backfill_table`` so ``main()`` can derive the
    confirmation-prompt count from the same list it later writes — avoiding a
    redundant second scan and the race window it would create.
    """
    # Narrow the scan to rows that at least have LastSyncTime + FREE. The final
    # filter (needs_backfill) still runs in Python so the test suite can drive
    # edge cases without hitting a filter-expression parser.
    scan_kwargs = {"FilterExpression": "TenantStatus = :free AND attribute_exists(LastSyncTime) AND attribute_not_exists(ReconcileReadyAt)", "ExpressionAttributeValues": {":free": "FREE"}}
    return [item for item in iter_tenant_items(table, scan_kwargs=scan_kwargs) if needs_backfill(item)]


def backfill_table(table: Any, *, dry_run: bool, logger: Callable[[str], None] = print, candidates: list[dict[str, Any]] | None = None) -> tuple[int, int]:
    """(Optionally) write a backfill row per candidate.

    Args:
        candidates: Pre-collected rows to backfill. When ``None`` the function
            performs its own scan — kept as a convenience for ad-hoc callers
            and existing tests, but ``main()`` collects once and injects.

    Returns:
        (candidate_count, written_count) — identical on a happy run; a gap
        means some writes hit the ``ConditionExpression`` (already-backfilled
        rows raced us) or raised and were skipped.
    """
    if candidates is None:
        candidates = collect_candidates(table)

    logger(f"Found {len(candidates)} tenant row(s) requiring backfill.")
    if candidates[:_SAMPLE_LIMIT]:
        logger("Sample:")
        for sample in candidates[:_SAMPLE_LIMIT]:
            logger(f"  TenantID={sample.get('TenantID')} LastSyncTime={sample.get('LastSyncTime')}")

    if dry_run or not candidates:
        return len(candidates), 0

    written = 0
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    for item in candidates:
        tenant_id = item.get("TenantID")
        if not tenant_id:
            logger(f"Skipping row without TenantID: {item!r}")
            continue
        last_sync_time = item.get("LastSyncTime")
        try:
            table.update_item(**build_update_kwargs(str(tenant_id), last_sync_time))
            written += 1
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Concurrent writer beat us — fine, backfill is already in effect.
                logger(f"Skipped {tenant_id}: ReconcileReadyAt already set (raced).")
                continue
            logger(f"Failed to backfill {tenant_id}: {exc!s}")

    logger(f"Backfill wrote {written} row(s).")
    return len(candidates), written


def _confirm_or_abort(*, assume_yes: bool, count: int) -> None:
    if assume_yes or count == 0:
        return
    answer = input(f"About to backfill {count} tenant row(s). Proceed? [y/N]: ").strip().lower()
    if answer not in {"y", "yes"}:
        print("Aborted.")
        raise SystemExit(0)


def main() -> int:
    """CLI entry point — loads env, resolves the table, runs the backfill."""
    parser = _build_parser()
    args = parser.parse_args()

    env_file = Path(args.env_file).expanduser().resolve()
    _load_environment(env_file)

    for path in (SERVICE_DIR, COMMON_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from config import tenant_data_table  # pylint: disable=import-outside-toplevel

    # One scan drives both the confirmation prompt and the writes, so the
    # number in the prompt is exactly what gets written (no race window).
    candidates = collect_candidates(tenant_data_table)

    if args.dry_run:
        backfill_table(tenant_data_table, dry_run=True, candidates=candidates)
        return 0

    _confirm_or_abort(assume_yes=args.yes, count=len(candidates))
    backfill_table(tenant_data_table, dry_run=False, candidates=candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
