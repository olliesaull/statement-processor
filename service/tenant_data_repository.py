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
from typing import Any

from botocore.exceptions import ClientError

from config import ddb, tenant_data_table
from repository_helpers import fetch_items_by_tenant_id


def _now_ms() -> int:
    """Return the current time in epoch milliseconds.

    Extracted so tests can monkey-patch the clock deterministically.
    """
    return int(time.time() * 1000)


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


_PROGRESS_RESOURCES: dict[str, str] = {
    "contacts": "ContactsProgress",
    "invoices": "InvoicesProgress",
    "credit_notes": "CreditNotesProgress",
    "payments": "PaymentsProgress",
    "per_contact_index": "PerContactIndexProgress",
}


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

        Sets EraseTenantDataTime and transitions status if the tenant was
        mid-load (LOADING -> LOAD_INCOMPLETE) or mid-sync (SYNCING -> FREE).

        Args:
            tenant_id: Tenant being disconnected.
            erasure_epoch_ms: Epoch milliseconds when data should be erased.
            current_status: Tenant's status at time of disconnect.
        """
        update_expr = "SET EraseTenantDataTime = :erasure_time"
        expr_values: dict[str, object] = {":erasure_time": erasure_epoch_ms}

        # Transition interrupted states to a safe resting state before erasure runs.
        status_transitions = {TenantStatus.LOADING: TenantStatus.LOAD_INCOMPLETE, TenantStatus.SYNCING: TenantStatus.FREE}
        new_status = status_transitions.get(current_status)
        if new_status:
            update_expr += ", TenantStatus = :new_status"
            expr_values[":new_status"] = new_status

        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expr, ExpressionAttributeValues=expr_values)

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
        deduped = [tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)]
        result: dict[str, dict[str, Any] | None] = {tid: None for tid in deduped}
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
    def update_resource_progress(cls, tenant_id: str, resource: str, status: str, records_fetched: int | None = None, record_total: int | None = None) -> None:
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

        progress: dict[str, Any] = {"status": status, "updated_at": now_ms}
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
        """
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
