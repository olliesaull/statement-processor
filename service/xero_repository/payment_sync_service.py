from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from xero_python.accounting import AccountingApi

from config import logger
from .payment_cache import PaymentCache, PaymentCachePayload
from .serialization import fmt_date, parse_updated_datetime
from .sync_models import ResourceSyncState


PAGE_SIZE = 100
DELTA_REFRESH_SECONDS = 300


class PaymentSyncService:
    """Background synchronisation manager for tenant payments."""

    def __init__(
        self,
        cache: PaymentCache,
        token_provider: Callable[[str], Optional[Dict[str, Any]]],
        api_factory: Callable[[Dict[str, Any]], AccountingApi],
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        self._cache = cache
        self._token_provider = token_provider
        self._api_factory = api_factory
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._status_lock = threading.Lock()
        self._status: Dict[str, ResourceSyncState] = {}
        self._futures: Dict[str, Future] = {}
        self._token_snapshots: Dict[str, Dict[str, Any]] = {}
        self._refresh_interval = timedelta(seconds=DELTA_REFRESH_SECONDS)

    def get_status(self, tenant_id: Optional[str]) -> ResourceSyncState:
        if not tenant_id:
            return ResourceSyncState(resource="payments", status="empty")

        with self._status_lock:
            state = self._status.get(tenant_id)
            if state is not None:
                if state.status == "ready":
                    payload = self._cache.load(tenant_id)
                    if not payload:
                        logger.info("Payment cache missing; resetting sync state", tenant_id=tenant_id)
                        empty_state = ResourceSyncState(resource="payments", status="empty")
                        self._status[tenant_id] = empty_state
                        return empty_state
                return replace(state)

        payload = self._cache.load(tenant_id)
        if payload:
            derived = ResourceSyncState(
                resource="payments",
                status="ready",
                synced_count=len(payload.payments),
                total_count=len(payload.payments),
                last_synced_at=payload.last_synced_at,
                last_updated_utc=payload.last_updated_utc,
            )
            with self._status_lock:
                self._status[tenant_id] = derived
            return replace(derived)

        return ResourceSyncState(resource="payments", status="empty")

    def get_payments(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        if not tenant_id:
            return []
        payload = self._cache.load(tenant_id)
        if not payload:
            return []
        return list(payload.payments)

    def clear(self, tenant_id: Optional[str]) -> None:
        if not tenant_id:
            return
        with self._status_lock:
            future = self._futures.pop(tenant_id, None)
            if future and not future.done():
                future.cancel()
            self._status.pop(tenant_id, None)
            self._token_snapshots.pop(tenant_id, None)
        self._cache.delete(tenant_id)
        logger.info("Cleared payment cache", tenant_id=tenant_id)

    def ensure_background_sync(self, tenant_id: Optional[str], *, force_full: bool = False) -> None:
        if not tenant_id:
            return

        with self._status_lock:
            future = self._futures.get(tenant_id)
            if future and not future.done():
                if force_full:
                    future.cancel()
                else:
                    return

        state = self.get_status(tenant_id)
        if state.status == "syncing":
            return

        if not force_full and state.status == "ready" and not self._should_refresh(state.last_synced_at):
            return

        token = self._token_provider(tenant_id)
        if not token:
            logger.error("Payment sync skipped; OAuth token unavailable", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_unavailable")
            return

        with self._status_lock:
            self._token_snapshots[tenant_id] = dict(token)

        logger.info("Queuing payment sync", tenant_id=tenant_id, force_full=force_full, previous_status=state.status)
        future = self._executor.submit(self._run_sync, tenant_id, force_full)
        with self._status_lock:
            self._futures[tenant_id] = future

    def _update_status(self, tenant_id: str, **kwargs) -> None:
        with self._status_lock:
            current = self._status.get(tenant_id, ResourceSyncState(resource="payments", status="empty"))
            updated = replace(current, **kwargs)
            self._status[tenant_id] = updated

    def _pop_snapshot(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._status_lock:
            snapshot = self._token_snapshots.pop(tenant_id, None)
        if snapshot is None:
            return None
        return dict(snapshot)

    def _should_refresh(self, last_synced_at: Optional[str]) -> bool:
        if not last_synced_at:
            return True
        try:
            normalized = last_synced_at.replace("Z", "+00:00") if last_synced_at.endswith("Z") else last_synced_at
            last_sync = datetime.fromisoformat(normalized)
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=timezone.utc)
        except ValueError:
            return True

        return datetime.now(timezone.utc) - last_sync >= self._refresh_interval

    def _parse_iso_timestamp(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    def _run_sync(self, tenant_id: str, force_full: bool) -> None:
        token = self._pop_snapshot(tenant_id)
        if not token:
            logger.error("Payment sync aborted; missing token snapshot", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_missing")
            return

        try:
            api = self._api_factory(token)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Payment sync aborted; failed to build API client", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error="api_client_initialization_failed")
            return

        self._update_status(
            tenant_id,
            status="syncing",
            synced_count=0,
            total_count=None,
            error=None,
        )

        payload = self._cache.load(tenant_id)
        run_full = force_full or not payload
        logger.info("Starting payment sync", tenant_id=tenant_id, mode="full" if run_full else "delta", force_full=force_full)
        if run_full:
            self._perform_full_sync(tenant_id, api)
        else:
            self._perform_delta_sync(tenant_id, payload, api)

    def _perform_full_sync(self, tenant_id: str, api: AccountingApi) -> None:
        payments: Dict[str, Dict[str, Any]] = {}
        max_updated_utc: Optional[datetime] = None
        page = 1

        try:
            while True:
                result = api.get_payments(
                    tenant_id,
                    order="Date ASC",
                    page=page,
                )

                batch = result.payments or []
                if not batch:
                    break

                for item in batch:
                    record, updated_at = self._serialize_payment(item)
                    key = record.get("payment_id") or record.get("invoice_id")
                    if not key:
                        continue
                    payments[str(key)] = record
                    if updated_at and (not max_updated_utc or updated_at > max_updated_utc):
                        max_updated_utc = updated_at

                synced_count = len(payments)
                self._update_status(
                    tenant_id,
                    status="syncing",
                    synced_count=synced_count,
                    total_count=None,
                    error=None,
                )

                if len(batch) < PAGE_SIZE:
                    break

                page += 1

            ordered = sorted(payments.values(), key=lambda p: (p.get("date") or ""))
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            payload = PaymentCachePayload(
                payments=ordered,
                last_synced_at=last_synced_at,
                last_updated_utc=max_updated_utc.astimezone(timezone.utc).isoformat() if max_updated_utc else None,
            )
            self._cache.save(tenant_id, payload)
            self._update_status(
                tenant_id,
                status="ready",
                synced_count=len(ordered),
                total_count=len(ordered),
                last_synced_at=last_synced_at,
                last_updated_utc=payload.last_updated_utc,
                error=None,
            )
            logger.info("Payment sync completed", tenant_id=tenant_id, payments=len(ordered), force_full=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Payment sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))

    def _perform_delta_sync(self, tenant_id: str, payload: PaymentCachePayload, api: AccountingApi) -> None:
        baseline_max = self._parse_iso_timestamp(payload.last_updated_utc)
        since = baseline_max or self._parse_iso_timestamp(payload.last_synced_at)
        if not since:
            logger.info("Payment delta sync fallback to full; missing baseline", tenant_id=tenant_id)
            self._perform_full_sync(tenant_id, api)
            return

        since = since - timedelta(seconds=1)

        existing: Dict[str, Dict[str, Any]] = {}
        for payment in payload.payments:
            key = payment.get("payment_id") or payment.get("invoice_id")
            if key:
                existing[str(key)] = dict(payment)

        max_updated_utc = baseline_max
        page = 1
        updated_count = 0

        try:
            while True:
                result = api.get_payments(
                    tenant_id,
                    if_modified_since=since,
                    order="Date ASC",
                    page=page,
                )

                batch = result.payments or []
                if not batch:
                    break

                for item in batch:
                    record, updated_at = self._serialize_payment(item)
                    key = record.get("payment_id") or record.get("invoice_id")
                    if not key:
                        continue
                    existing[str(key)] = record
                    if updated_at and (not max_updated_utc or updated_at > max_updated_utc):
                        max_updated_utc = updated_at
                    updated_count += 1

                self._update_status(
                    tenant_id,
                    status="syncing",
                    synced_count=len(existing),
                    total_count=None,
                    error=None,
                )

                if len(batch) < PAGE_SIZE:
                    break

                page += 1

            if updated_count == 0:
                last_synced_at = datetime.now(tz=timezone.utc).isoformat()
                self._update_status(
                    tenant_id,
                    status="ready",
                    synced_count=len(existing),
                    total_count=len(existing),
                    last_synced_at=last_synced_at,
                    last_updated_utc=payload.last_updated_utc,
                    error=None,
                )
                logger.info("Payment delta sync completed with no changes", tenant_id=tenant_id)
                return

            ordered = sorted(existing.values(), key=lambda p: (p.get("date") or ""))
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            last_updated_utc = (
                max_updated_utc.astimezone(timezone.utc).isoformat()
                if max_updated_utc
                else payload.last_updated_utc
            )
            new_payload = PaymentCachePayload(
                payments=ordered,
                last_synced_at=last_synced_at,
                last_updated_utc=last_updated_utc,
            )
            self._cache.save(tenant_id, new_payload)
            self._update_status(
                tenant_id,
                status="ready",
                synced_count=len(ordered),
                total_count=len(ordered),
                last_synced_at=last_synced_at,
                last_updated_utc=last_updated_utc,
                error=None,
            )
            logger.info(
                "Payment delta sync completed",
                tenant_id=tenant_id,
                payments=len(ordered),
                updates=updated_count,
                since=since.isoformat(),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Payment delta sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))

    def _serialize_payment(self, payment: Any) -> tuple[Dict[str, Any], Optional[datetime]]:
        invoice_obj = getattr(payment, "invoice", None)
        contact = None
        if invoice_obj is not None:
            contact_obj = getattr(invoice_obj, "contact", None)
            if contact_obj is not None:
                contact = {
                    "contact_id": getattr(contact_obj, "contact_id", None),
                    "name": getattr(contact_obj, "name", None),
                }

        record = {
            "payment_id": getattr(payment, "payment_id", None),
            "invoice_id": getattr(invoice_obj, "invoice_id", None) if invoice_obj else None,
            "reference": getattr(payment, "reference", None),
            "amount": getattr(payment, "amount", None),
            "date": fmt_date(getattr(payment, "date", None)),
            "status": getattr(payment, "status", None),
            "contact": contact,
        }

        updated_at = parse_updated_datetime(getattr(payment, "updated_date_utc", None))
        return record, updated_at

