from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from xero_python.accounting import AccountingApi

from config import logger
from .invoice_cache import InvoiceCache, InvoiceCachePayload
from .sync_models import ResourceSyncState
from .serialization import fmt_date, parse_updated_datetime


PAGE_SIZE = 100
DELTA_REFRESH_SECONDS = 300


class InvoiceSyncService:
    """Background synchronisation manager for tenant invoices."""

    def __init__(
        self,
        cache: InvoiceCache,
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
            return ResourceSyncState(resource="invoices", status="empty")

        with self._status_lock:
            state = self._status.get(tenant_id)
            if state is not None:
                if state.status == "ready":
                    payload = self._cache.load(tenant_id)
                    if not payload:
                        logger.info("Invoice cache missing; resetting sync state", tenant_id=tenant_id)
                        empty_state = ResourceSyncState(resource="invoices", status="empty")
                        self._status[tenant_id] = empty_state
                        return empty_state
                return replace(state)

        payload = self._cache.load(tenant_id)
        if payload:
            derived = ResourceSyncState(
                resource="invoices",
                status="ready",
                synced_count=len(payload.invoices),
                total_count=len(payload.invoices),
                last_synced_at=payload.last_synced_at,
                last_updated_utc=payload.last_updated_utc,
            )
            with self._status_lock:
                self._status[tenant_id] = derived
            return replace(derived)

        return ResourceSyncState(resource="invoices", status="empty")

    def get_invoices(self, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        if not tenant_id:
            return []
        payload = self._cache.load(tenant_id)
        if not payload:
            return []
        return list(payload.invoices)

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
        logger.info("Cleared invoice cache", tenant_id=tenant_id)

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
            logger.error("Invoice sync skipped; OAuth token unavailable", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_unavailable")
            return

        with self._status_lock:
            self._token_snapshots[tenant_id] = dict(token)

        logger.info("Queuing invoice sync", tenant_id=tenant_id, force_full=force_full, previous_status=state.status)
        future = self._executor.submit(self._run_sync, tenant_id, force_full)
        with self._status_lock:
            self._futures[tenant_id] = future

    def _update_status(self, tenant_id: str, **kwargs) -> None:
        with self._status_lock:
            current = self._status.get(tenant_id, ResourceSyncState(resource="invoices", status="empty"))
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
            logger.error("Invoice sync aborted; missing token snapshot", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_missing")
            return

        try:
            api = self._api_factory(token)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Invoice sync aborted; failed to build API client", tenant_id=tenant_id, error=str(exc))
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
        logger.info("Starting invoice sync", tenant_id=tenant_id, mode="full" if run_full else "delta", force_full=force_full)
        if run_full:
            self._perform_full_sync(tenant_id, api)
        else:
            self._perform_delta_sync(tenant_id, payload, api)

    def _perform_full_sync(self, tenant_id: str, api: AccountingApi) -> None:
        invoices: Dict[str, Dict[str, Any]] = {}
        max_updated_utc: Optional[datetime] = None
        page = 1

        try:
            while True:
                result = api.get_invoices(
                    tenant_id,
                    order="InvoiceNumber ASC",
                    page=page,
                    include_archived=False,
                    created_by_my_app=False,
                    unitdp=2,
                    summary_only=False,
                    page_size=PAGE_SIZE,
                    statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"],
                )

                batch = result.invoices or []
                if not batch:
                    break

                for item in batch:
                    record, updated_at = self._serialize_invoice(item)
                    key = record.get("invoice_id") or record.get("number")
                    if not key:
                        continue
                    invoices[str(key)] = record
                    if updated_at and (not max_updated_utc or updated_at > max_updated_utc):
                        max_updated_utc = updated_at

                synced_count = len(invoices)
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

            ordered = sorted(invoices.values(), key=lambda inv: (inv.get("number") or "").casefold())
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            payload = InvoiceCachePayload(
                invoices=ordered,
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
            logger.info("Invoice sync completed", tenant_id=tenant_id, invoices=len(ordered), force_full=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Invoice sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))

    def _perform_delta_sync(self, tenant_id: str, payload: InvoiceCachePayload, api: AccountingApi) -> None:
        baseline_max = self._parse_iso_timestamp(payload.last_updated_utc)
        since = baseline_max or self._parse_iso_timestamp(payload.last_synced_at)
        if not since:
            logger.info("Invoice delta sync fallback to full; missing baseline", tenant_id=tenant_id)
            self._perform_full_sync(tenant_id, api)
            return

        since = since - timedelta(seconds=1)

        existing: Dict[str, Dict[str, Any]] = {}
        for inv in payload.invoices:
            key = inv.get("invoice_id") or inv.get("number")
            if key:
                existing[str(key)] = dict(inv)

        max_updated_utc = baseline_max
        page = 1
        updated_count = 0

        try:
            while True:
                result = api.get_invoices(
                    tenant_id,
                    if_modified_since=since,
                    order="InvoiceNumber ASC",
                    page=page,
                    include_archived=False,
                    created_by_my_app=False,
                    unitdp=2,
                    summary_only=False,
                    page_size=PAGE_SIZE,
                )

                batch = result.invoices or []
                if not batch:
                    break

                for item in batch:
                    record, updated_at = self._serialize_invoice(item)
                    key = record.get("invoice_id") or record.get("number")
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
                logger.info("Invoice delta sync completed with no changes", tenant_id=tenant_id)
                return

            ordered = sorted(existing.values(), key=lambda inv: (inv.get("number") or "").casefold())
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            last_updated_utc = (
                max_updated_utc.astimezone(timezone.utc).isoformat()
                if max_updated_utc
                else payload.last_updated_utc
            )
            new_payload = InvoiceCachePayload(
                invoices=ordered,
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
                "Invoice delta sync completed",
                tenant_id=tenant_id,
                invoices=len(ordered),
                updates=updated_count,
                since=since.isoformat(),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Invoice delta sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))

    def _serialize_invoice(self, invoice: Any) -> tuple[Dict[str, Any], Optional[datetime]]:
        """Return a serialisable invoice dict and its updated timestamp (if present)."""
        contact_obj = getattr(invoice, "contact", None)
        contact = (
            {
                "contact_id": getattr(contact_obj, "contact_id", None),
                "name": getattr(contact_obj, "name", None),
                "email": getattr(contact_obj, "email_address", None),
                "is_customer": getattr(contact_obj, "is_customer", None),
                "is_supplier": getattr(contact_obj, "is_supplier", None),
                "status": getattr(contact_obj, "contact_status", None),
            }
            if contact_obj
            else None
        )

        record = {
            "invoice_id": getattr(invoice, "invoice_id", None),
            "number": getattr(invoice, "invoice_number", None),
            "type": getattr(invoice, "type", None),
            "status": getattr(invoice, "status", None),
            "date": fmt_date(getattr(invoice, "date", None)),
            "due_date": fmt_date(getattr(invoice, "due_date", None)),
            "reference": getattr(invoice, "reference", None),
            "subtotal": getattr(invoice, "sub_total", None),
            "total_tax": getattr(invoice, "total_tax", None),
            "total": getattr(invoice, "total", None),
            "contact": contact,
        }

        updated_at = parse_updated_datetime(getattr(invoice, "updated_date_utc", None))
        return record, updated_at
