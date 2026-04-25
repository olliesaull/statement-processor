"""
Repository helpers for tenant metadata stored in DynamoDB.

Provides:
- A typed ``TenantStatus`` enum
- Lookups for individual tenants and bulk status/token balance checks
- Per-resource sync progress writes and the ``ReconcileReadyAt`` gate
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from botocore.exceptions import ClientError

from config import ddb, tenant_data_table
from repository_helpers import fetch_items_by_tenant_id

SYNC_STALE_THRESHOLD_MS: Final[int] = 5 * 60 * 1000
"""How old a ``LastHeartbeatAt`` must be before ``try_acquire_sync`` treats an
in-flight sync as a crashed worker. Shared by the background sync path and
the retry-sync API so both enforce one consistent timeout."""


def _now_ms() -> int:
    """Return the current time in epoch milliseconds.

    Extracted so tests can monkey-patch the clock deterministically.
    """
    return int(time.time() * 1000)


class ProgressStatus(StrEnum):
    """States a single ``*Progress`` map can be in.

    ``pending`` — not yet attempted on this sync cycle.
    ``in_progress`` — fetcher is actively running; ``records_fetched`` grows.
    ``complete`` — resource finished successfully.
    ``failed`` — fetcher raised; the Retry-sync path picks this up.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class TenantStatus(StrEnum):
    """Known tenant processing states.

    ``LOADING`` — initial contacts-first phase of the very first sync after a
    tenant connects. The app is gated for the user during this phase.

    ``SYNCING`` — overloaded by design: covers both the post-contacts "heavy
    phase" of the initial load (invoices + credit notes + payments + per-contact
    index) AND any later manual/incremental sync. The distinguishing signal is
    ``ReconcileReadyAt``: when it is unset the tenant has never completed a full
    load, and ``/statement/<id>`` remains gated; when it is set the tenant is in
    a post-initial-load incremental sync and `/statement/<id>` is open. See the
    decision log entry "Overload TenantStatus.SYNCING rather than add
    LOADING_HEAVY" for rationale.

    ``FREE`` — idle, reconcile-ready.

    ``LOAD_INCOMPLETE`` — a sync attempt failed before ``ReconcileReadyAt`` was
    set; the UI surfaces a Retry-sync button.

    ``ERASED`` — tenant data has been wiped after disconnect + grace period.
    """

    FREE = "FREE"
    SYNCING = "SYNCING"
    LOADING = "LOADING"
    LOAD_INCOMPLETE = "LOAD_INCOMPLETE"
    ERASED = "ERASED"


# IMPORTANT: the attribute names on the right-hand side are duplicated in
# lambda_functions/tenant_erasure_lambda/main.py (_mark_as_erased REMOVE
# expression). Any addition/rename here MUST be mirrored there — the
# erasure Lambda does not share code with the service. See decision log
# 2026-04-23 ("Tenant management UX fixes — five choices") for context.
_PROGRESS_RESOURCES: dict[str, str] = {
    "contacts": "ContactsProgress",
    "invoices": "InvoicesProgress",
    "credit_notes": "CreditNotesProgress",
    "payments": "PaymentsProgress",
    "per_contact_index": "PerContactIndexProgress",
}

ALL_SYNC_RESOURCES: Final[tuple[str, ...]] = tuple(_PROGRESS_RESOURCES.keys())
"""Every progress-tracked resource, in persistence order.

Consumed by:
- ``sync.sync_data`` to scope the start-of-run progress-map reset.
- ``utils.sync_progress.is_retry_recommended`` to iterate retry candidates.
- ``routes.api._collect_retry_resources`` for the same.

Source of truth is ``_PROGRESS_RESOURCES`` above; this tuple is derived so
adding a new sync resource means one edit, not three."""


def _progress_attribute_name(resource: str) -> str:
    """Map a snake_case resource identifier to its DynamoDB attribute name.

    Raises:
        ValueError: when the resource is not one of the known sync targets.
    """
    try:
        return _PROGRESS_RESOURCES[resource]
    except KeyError as exc:
        raise ValueError(f"Unknown progress resource: {resource!r}") from exc


@dataclass(frozen=True)
class TenantDataRepository:
    """Repository wrapper around the TenantData DynamoDB table."""

    _table = tenant_data_table

    @staticmethod
    def _determine_status(item: dict[str, Any]) -> TenantStatus:
        """Extract a tenant status value from a DynamoDB record."""
        raw_status = item.get("TenantStatus")

        if isinstance(raw_status, TenantStatus):
            return raw_status

        if isinstance(raw_status, str):
            candidate = raw_status.strip().upper()
            for status in TenantStatus:
                if candidate == status:
                    return status

        return TenantStatus.FREE

    @classmethod
    def get_item(cls, tenant_id: str) -> dict[str, object] | None:
        """Fetch a single tenant record by ID."""
        if not tenant_id:
            return None

        response = cls._table.get_item(Key={"TenantID": tenant_id})
        return response.get("Item")

    @classmethod
    def _get_items_by_tenant_id(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, dict[str, object] | None]:
        """Fetch multiple tenant records concurrently."""
        return fetch_items_by_tenant_id(cls.get_item, tenant_ids, max_workers=max_workers)

    @classmethod
    def get_dismissed_banners(cls, tenant_id: str) -> set[str]:
        """Fetch the set of permanently dismissed banner keys for a tenant.

        Returns:
            Set of dismiss_key strings. Empty set if no row or no attribute.
        """
        item = cls.get_item(tenant_id)
        if not item:
            return set()
        raw = item.get("DismissedBanners")
        if isinstance(raw, set):
            return raw
        return set()

    @classmethod
    def dismiss_banner(cls, tenant_id: str, dismiss_key: str) -> None:
        """Permanently dismiss a banner for a tenant.

        Uses DynamoDB ADD on a string set, which is atomic and idempotent.

        Args:
            tenant_id: Tenant dismissing the banner.
            dismiss_key: Unique banner identifier to dismiss.
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="ADD DismissedBanners :dismiss_key", ExpressionAttributeValues={":dismiss_key": {dismiss_key}})

    @classmethod
    def schedule_erasure(cls, tenant_id: str, erasure_epoch_ms: int, current_status: TenantStatus) -> None:
        """Schedule tenant data for erasure at a future time.

        Sets EraseTenantDataTime and transitions interrupted states to a safe
        resting state before erasure runs:

        - LOADING (initial contacts phase) -> LOAD_INCOMPLETE so the Retry-sync
          affordance reappears if the user reconnects before the grace period.
        - SYNCING is overloaded (see ``TenantStatus`` docstring). We resolve the
          ambiguity by inspecting ``ReconcileReadyAt``:
          * Unset -> still in the initial heavy phase, fall through to
            LOAD_INCOMPLETE so reconciliation stays gated until a full sync
            completes.
          * Set -> post-initial incremental sync, FREE is correct because
            reconciliation remained available throughout.

        Args:
            tenant_id: Tenant being disconnected.
            erasure_epoch_ms: Epoch milliseconds when data should be erased.
            current_status: Tenant's status at time of disconnect.
        """
        update_expr = "SET EraseTenantDataTime = :erasure_time"
        expr_values: dict[str, object] = {":erasure_time": erasure_epoch_ms}

        new_status = cls._resolve_erasure_transition(tenant_id, current_status)
        if new_status:
            update_expr += ", TenantStatus = :new_status"
            expr_values[":new_status"] = new_status

        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expr, ExpressionAttributeValues=expr_values)

    @classmethod
    def _resolve_erasure_transition(cls, tenant_id: str, current_status: TenantStatus) -> TenantStatus | None:
        """Pick the post-disconnect status, reading ``ReconcileReadyAt`` for SYNCING."""
        if current_status == TenantStatus.LOADING:
            return TenantStatus.LOAD_INCOMPLETE
        if current_status != TenantStatus.SYNCING:
            return None

        # SYNCING is the overloaded state. A missing row or missing
        # ``ReconcileReadyAt`` means the tenant never completed a full load,
        # so LOAD_INCOMPLETE preserves the Retry-sync path on reconnect.
        item = cls.get_item(tenant_id)
        if item and item.get("ReconcileReadyAt") is not None:
            return TenantStatus.FREE
        return TenantStatus.LOAD_INCOMPLETE

    @classmethod
    def cancel_erasure(cls, tenant_id: str) -> None:
        """Cancel a pending erasure by removing the scheduled time.

        Called when a tenant reconnects before the erasure Lambda runs.

        Args:
            tenant_id: Tenant reconnecting.
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="REMOVE EraseTenantDataTime")

    @classmethod
    def get_tenant_statuses(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, TenantStatus]:
        """
        Fetch multiple tenant records concurrently and return their status.

        Args:
            tenant_ids: Iterable of tenant IDs to inspect.
            max_workers: Maximum number of concurrent lookups.

        Returns:
            Mapping of tenant IDs to their current status.
        """
        unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}
        statuses: dict[str, TenantStatus] = dict.fromkeys(unique_ids, TenantStatus.FREE)
        items = cls._get_items_by_tenant_id(unique_ids, max_workers=max_workers)

        for tenant_id, item in items.items():
            if item:
                statuses[tenant_id] = cls._determine_status(item)

        return statuses

    @classmethod
    def get_many(cls, tenant_ids: list[str]) -> dict[str, dict[str, Any] | None]:
        """Fetch multiple tenant rows in one DynamoDB ``BatchGetItem`` call.

        Preferred for the sync-progress HTMX endpoint where we read the full
        tenant row for every session tenant on a 3-second cadence. Replacing
        ``get_tenant_statuses``'s per-key concurrent ``get_item`` calls with a
        single ``BatchGetItem`` halves the DynamoDB RCU load and avoids
        thread-pool overhead for a handful of tenants.

        Args:
            tenant_ids: Tenant IDs to fetch. DynamoDB caps ``BatchGetItem`` at
                100 keys per call, so larger lists are chunked transparently.

        Returns:
            Mapping of every requested tenant_id to its item dict, or ``None``
            when the row does not exist.
        """
        # BatchGetItem rejects duplicate keys with ValidationException, so
        # dedup up-front. dict.fromkeys preserves first-seen order.
        deduped = list(dict.fromkeys(tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)))
        result: dict[str, dict[str, Any] | None] = dict.fromkeys(deduped)
        if not deduped:
            return {}

        table_name = cls._table.name
        # DynamoDB hard-caps BatchGetItem at 100 keys per call.
        for chunk_start in range(0, len(deduped), 100):
            chunk = deduped[chunk_start : chunk_start + 100]
            keys = [{"TenantID": tid} for tid in chunk]
            remaining_keys: list[dict[str, str]] = keys
            while remaining_keys:
                response = ddb.batch_get_item(RequestItems={table_name: {"Keys": remaining_keys}})
                items = response.get("Responses", {}).get(table_name, [])
                for item in items:
                    tid = item.get("TenantID")
                    if isinstance(tid, str):
                        result[tid] = item
                unprocessed = response.get("UnprocessedKeys", {}).get(table_name, {}).get("Keys", [])
                if not unprocessed:
                    break
                remaining_keys = unprocessed

        return result

    @classmethod
    def update_resource_progress(cls, tenant_id: str, resource: str, status: ProgressStatus, records_fetched: int | None = None, record_total: int | None = None) -> None:
        """Write per-resource sync progress plus a heartbeat timestamp.

        The progress map is written as a whole each time so readers always see
        a consistent snapshot. ``record_total`` is intentionally written even
        when ``None`` — the sync UI relies on ``record_total=null`` to render
        an indeterminate progress bar when Xero omits pagination totals.

        For ``per_contact_index`` the count fields are omitted entirely, since
        the operation is bounded and single-step (see plan Step 4b).

        Args:
            tenant_id: Tenant whose progress is being updated.
            resource: Snake-case resource identifier — one of
                ``contacts``, ``invoices``, ``credit_notes``, ``payments``,
                ``per_contact_index``.
            status: ``pending`` | ``in_progress`` | ``complete`` | ``failed``.
            records_fetched: Records written so far (per-page accumulator).
                Ignored for ``per_contact_index``.
            record_total: Upstream total when Xero pagination provides one;
                ``None`` means indeterminate. Ignored for ``per_contact_index``.
        """
        attribute_name = _progress_attribute_name(resource)
        now_ms = _now_ms()

        # Coerce StrEnum → raw str so downstream DDB reads don't return an enum
        # value (boto3 serialises StrEnum as str but normalise defensively).
        progress: dict[str, Any] = {"status": str(status), "updated_at": now_ms}
        # per_contact_index is a bounded, single-step operation — no counts.
        if resource != "per_contact_index":
            progress["records_fetched"] = records_fetched
            progress["record_total"] = record_total

        cls._table.update_item(
            Key={"TenantID": tenant_id},
            UpdateExpression="SET #progress = :progress, LastHeartbeatAt = :heartbeat",
            ExpressionAttributeNames={"#progress": attribute_name},
            ExpressionAttributeValues={":progress": progress, ":heartbeat": now_ms},
        )

    @classmethod
    def reset_resource_progress(cls, tenant_id: str, resources: Iterable[str]) -> None:
        """Reset the named progress sub-maps to ``{status: pending}``.

        Called by ``sync_data`` at start so a new run doesn't inherit stale
        FAILED / IN_PROGRESS markers from an interrupted prior attempt.
        Resources not listed stay untouched — retry paths rely on this to
        preserve COMPLETE markers for the resources they're skipping.

        Caller contract: the tenant row must already exist — in practice this
        is only called after ``try_acquire_sync`` has succeeded, which writes
        the row as part of the lock-claim ``UpdateItem``. No
        ``ConditionExpression`` is used here: a missing row is an invariant
        violation, not a runtime condition to guard.

        Args:
            tenant_id: Tenant whose maps are being reset.
            resources: Snake-case resource identifiers to reset. Unknown
                identifiers raise via ``_progress_attribute_name``.
        """
        resources = list(resources)
        if not resources:
            return
        names = {f"#r{i}": _progress_attribute_name(r) for i, r in enumerate(resources)}
        set_clauses = [f"{alias} = :pending" for alias in names]
        # Mirror update_resource_progress's StrEnum coercion.
        pending: dict[str, Any] = {"status": str(ProgressStatus.PENDING)}
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="SET " + ", ".join(set_clauses), ExpressionAttributeNames=names, ExpressionAttributeValues={":pending": pending})

    @classmethod
    def mark_reconcile_ready(cls, tenant_id: str) -> None:
        """Flag the tenant as reconcile-ready after a successful initial or retry sync.

        Sets both ``ReconcileReadyAt`` (the load-bearing gate read by
        ``reconcile_ready_required``) and ``LastFullLoadCompletedAt`` (operator
        telemetry). Writing both as a single ``UpdateItem`` keeps them
        consistent — a partial write would be visible to the UI gate.

        Args:
            tenant_id: Tenant whose sync has fully completed.
        """
        now_ms = _now_ms()
        cls._table.update_item(
            Key={"TenantID": tenant_id},
            UpdateExpression="SET ReconcileReadyAt = :reconcile_ready_at, LastFullLoadCompletedAt = :completed_at",
            ExpressionAttributeValues={":reconcile_ready_at": now_ms, ":completed_at": now_ms},
        )

    @classmethod
    def try_acquire_sync(cls, tenant_id: str, target_status: TenantStatus, stale_threshold_ms: int) -> bool:
        """Atomically transition a tenant into a sync-active status.

        The ConditionExpression prevents overlapping syncs while still allowing
        a crashed worker to be recovered. It succeeds when any of the
        following hold:

        - The row does not exist yet (first-ever sync);
        - ``TenantStatus`` is ``FREE`` or ``LOAD_INCOMPLETE`` (idle / retryable);
        - ``TenantStatus`` is ``LOADING`` or ``SYNCING`` but the last heartbeat
          is older than ``stale_threshold_ms`` (or missing entirely), covering
          worker-crash recovery.

        ``ERASED`` is rejected — a disconnected tenant cannot be synced.

        Args:
            tenant_id: Tenant to acquire.
            target_status: Status to set on success (typically ``LOADING`` for
                first sync, ``SYNCING`` for manual/retry).
            stale_threshold_ms: How old the last heartbeat must be before an
                in-flight sync is treated as a crash. Passed as a DURATION
                (e.g. ``5 * 60 * 1000`` for 5 minutes) — the method converts
                it to an absolute timestamp using ``_now_ms()``.

        Returns:
            ``True`` when the status was successfully acquired; ``False`` when
            another sync is in flight (``ConditionalCheckFailedException``).

        Raises:
            ClientError: Any non-conditional DynamoDB error (throttling,
                permission denied, etc.) propagates unchanged — callers should
                log and surface these rather than swallow them.
            ValueError: When ``stale_threshold_ms`` is not strictly positive.
                A zero or negative threshold would clobber a fresh in-flight
                sync by making the "older than" comparison trivially true.
        """
        if stale_threshold_ms <= 0:
            raise ValueError(f"stale_threshold_ms must be positive; got {stale_threshold_ms}")
        now_ms = _now_ms()
        stale_threshold = now_ms - stale_threshold_ms

        condition = (
            "attribute_not_exists(#ts)"
            " OR #ts = :free"
            " OR #ts = :load_incomplete"
            " OR ((#ts = :loading OR #ts = :syncing)"
            " AND (attribute_not_exists(LastHeartbeatAt) OR LastHeartbeatAt < :stale_threshold))"
        )

        try:
            cls._table.update_item(
                Key={"TenantID": tenant_id},
                UpdateExpression="SET #ts = :target_status, LastHeartbeatAt = :now",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#ts": "TenantStatus"},
                ExpressionAttributeValues={
                    ":target_status": target_status.value,
                    ":now": now_ms,
                    ":free": TenantStatus.FREE.value,
                    ":load_incomplete": TenantStatus.LOAD_INCOMPLETE.value,
                    ":loading": TenantStatus.LOADING.value,
                    ":syncing": TenantStatus.SYNCING.value,
                    ":stale_threshold": stale_threshold,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        return True

    @classmethod
    def release_sync_lock(cls, tenant_id: str, fallback_status: TenantStatus) -> None:
        """Release a sync lock previously claimed via ``try_acquire_sync``.

        Clears ``LastHeartbeatAt`` and sets ``TenantStatus`` to
        ``fallback_status``. Intended for callers that acquired the lock
        synchronously (e.g. the retry-sync endpoint) but failed to hand off to
        the background worker — without this, the tenant would stay "locked in
        flight" until ``SYNC_STALE_THRESHOLD_MS`` elapsed, blocking legitimate
        retries for 5 minutes.

        Args:
            tenant_id: Tenant whose lock to release.
            fallback_status: Status to set. Use ``LOAD_INCOMPLETE`` when the
                lock was acquired to drive a retry and the retry never ran.
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="SET TenantStatus = :status REMOVE LastHeartbeatAt", ExpressionAttributeValues={":status": fallback_status.value})
