from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

from xero_python.accounting import AccountingApi

from config import logger
from core.contact_cache import ContactCache, ContactCachePayload


PAGE_SIZE = 100
DELTA_REFRESH_SECONDS = 300


@dataclass
class ContactSyncState:
    status: str
    synced_count: int = 0
    total_count: Optional[int] = None
    last_synced_at: Optional[str] = None
    last_updated_utc: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "synced_count": self.synced_count,
            "total_count": self.total_count,
            "last_synced_at": self.last_synced_at,
            "last_updated_utc": self.last_updated_utc,
            "error": self.error,
        }


class ContactSyncService:
    """Background synchronisation manager for Xero contacts."""

    def __init__(
        self,
        cache: ContactCache,
        token_provider: Callable[[str], Optional[Dict[str, Any]]],
        api_factory: Callable[[Dict[str, Any]], AccountingApi],
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        self._cache = cache
        self._token_provider = token_provider
        self._api_factory = api_factory
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._status_lock = threading.Lock()
        self._status: Dict[str, ContactSyncState] = {}
        self._futures: Dict[str, Future] = {}
        self._token_snapshots: Dict[str, Dict[str, Any]] = {}
        self._refresh_interval = timedelta(seconds=DELTA_REFRESH_SECONDS)

    def get_status(self, tenant_id: Optional[str]) -> ContactSyncState:
        if not tenant_id:
            return ContactSyncState(status="empty")

        with self._status_lock:
            state = self._status.get(tenant_id)
            if state is not None:
                if state.status == "ready":
                    payload = self._cache.load(tenant_id)
                    if not payload:
                        logger.info("Contact cache missing; resetting sync state", tenant_id=tenant_id)
                        self._status[tenant_id] = ContactSyncState(status="empty")
                        return ContactSyncState(status="empty")
                return replace(state)

        payload = self._cache.load(tenant_id)
        if payload:
            derived = ContactSyncState(
                status="ready",
                synced_count=len(payload.contacts),
                total_count=len(payload.contacts),
                last_synced_at=payload.last_synced_at,
                last_updated_utc=payload.last_updated_utc,
            )
            with self._status_lock:
                self._status[tenant_id] = derived
            return replace(derived)

        return ContactSyncState(status="empty")

    def get_contacts(self, tenant_id: Optional[str]) -> List[Dict[str, str]]:
        if not tenant_id:
            return []
        payload = self._cache.load(tenant_id)
        if not payload:
            return []
        return list(payload.contacts)

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
        logger.info("Cleared contact cache", tenant_id=tenant_id)

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
            logger.error("Contact sync skipped; OAuth token unavailable", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_unavailable")
            return

        with self._status_lock:
            self._token_snapshots[tenant_id] = dict(token)

        logger.info("Queuing contact sync", tenant_id=tenant_id, force_full=force_full, previous_status=state.status)
        future = self._executor.submit(self._run_sync, tenant_id, force_full)
        with self._status_lock:
            self._futures[tenant_id] = future

    def _update_status(self, tenant_id: str, **kwargs) -> None:
        with self._status_lock:
            current = self._status.get(tenant_id, ContactSyncState(status="empty"))
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

    def _coerce_updated_timestamp(
        self,
        updated_date: Optional[datetime],
        current_max: Optional[datetime],
    ) -> tuple[Optional[str], Optional[datetime]]:
        if not updated_date:
            return None, current_max
        if updated_date.tzinfo is None:
            updated_date = updated_date.replace(tzinfo=timezone.utc)
        updated_iso = updated_date.astimezone(timezone.utc).isoformat()
        if not current_max or updated_date > current_max:
            return updated_iso, updated_date
        return updated_iso, current_max

    def _run_sync(self, tenant_id: str, force_full: bool) -> None:
        token = self._pop_snapshot(tenant_id)
        if not token:
            logger.error("Contact sync aborted; missing token snapshot", tenant_id=tenant_id)
            self._update_status(tenant_id, status="error", error="token_missing")
            return

        try:
            api = self._api_factory(token)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Contact sync aborted; failed to build API client", tenant_id=tenant_id, error=str(exc))
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
        logger.info("Starting contact sync", tenant_id=tenant_id, mode="full" if run_full else "delta", force_full=force_full)
        if run_full:
            self._perform_full_sync(tenant_id, api)
        else:
            self._perform_delta_sync(tenant_id, payload, api)

    def _perform_full_sync(self, tenant_id: str, api: AccountingApi) -> None:
        contacts: List[Dict[str, str]] = []
        seen_contact_ids: set[str] = set()
        max_updated_utc: Optional[datetime] = None
        page = 1

        try:
            while True:
                result = api.get_contacts(
                    xero_tenant_id=tenant_id,
                    page=page,
                    include_archived=False,
                    page_size=PAGE_SIZE,
                )

                batch = result.contacts or []
                if not batch:
                    break

                for item in batch:
                    contact_id = getattr(item, "contact_id", None)
                    if not contact_id or contact_id in seen_contact_ids:
                        continue
                    seen_contact_ids.add(contact_id)

                    name = getattr(item, "name", "") or ""
                    updated_date = getattr(item, "updated_date_utc", None)
                    updated_iso, max_updated_utc = self._coerce_updated_timestamp(updated_date, max_updated_utc)

                    contacts.append(
                        {
                            "contact_id": str(contact_id),
                            "name": name,
                            "updated_at": updated_iso,
                        }
                    )

                synced_count = len(contacts)
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

            contacts.sort(key=lambda c: c["name"].casefold())
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            payload = ContactCachePayload(
                contacts=contacts,
                last_synced_at=last_synced_at,
                last_updated_utc=max_updated_utc.astimezone(timezone.utc).isoformat() if max_updated_utc else None,
            )
            self._cache.save(tenant_id, payload)
            self._update_status(
                tenant_id,
                status="ready",
                synced_count=len(contacts),
                total_count=len(contacts),
                last_synced_at=last_synced_at,
                last_updated_utc=payload.last_updated_utc,
                error=None,
            )
            logger.info("Contact sync completed", tenant_id=tenant_id, contacts=len(contacts), force_full=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Contact sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))

    def _perform_delta_sync(self, tenant_id: str, payload: ContactCachePayload, api: AccountingApi) -> None:
        baseline_max = self._parse_iso_timestamp(payload.last_updated_utc)
        since = baseline_max or self._parse_iso_timestamp(payload.last_synced_at)
        if not since:
            logger.info("Delta sync fallback to full; missing baseline", tenant_id=tenant_id)
            self._perform_full_sync(tenant_id, api)
            return

        since = since - timedelta(seconds=1)

        existing = {c.get("contact_id"): dict(c) for c in payload.contacts if c.get("contact_id")}
        max_updated_utc = baseline_max
        page = 1
        updated_count = 0

        try:
            while True:
                result = api.get_contacts(
                    xero_tenant_id=tenant_id,
                    if_modified_since=since,
                    page=page,
                    include_archived=False,
                    page_size=PAGE_SIZE,
                )

                batch = result.contacts or []
                if not batch:
                    break

                for item in batch:
                    contact_id = getattr(item, "contact_id", None)
                    if not contact_id:
                        continue

                    name = getattr(item, "name", "") or ""
                    updated_date = getattr(item, "updated_date_utc", None)
                    updated_iso, max_updated_utc = self._coerce_updated_timestamp(updated_date, max_updated_utc)

                    existing[str(contact_id)] = {
                        "contact_id": str(contact_id),
                        "name": name,
                        "updated_at": updated_iso,
                    }
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
                logger.info("Contact delta sync completed with no changes", tenant_id=tenant_id)
                return

            contacts = sorted(existing.values(), key=lambda c: c["name"].casefold())
            last_synced_at = datetime.now(tz=timezone.utc).isoformat()
            last_updated_utc = (
                max_updated_utc.astimezone(timezone.utc).isoformat()
                if max_updated_utc
                else payload.last_updated_utc
            )
            new_payload = ContactCachePayload(
                contacts=contacts,
                last_synced_at=last_synced_at,
                last_updated_utc=last_updated_utc,
            )
            self._cache.save(tenant_id, new_payload)
            self._update_status(
                tenant_id,
                status="ready",
                synced_count=len(contacts),
                total_count=len(contacts),
                last_synced_at=last_synced_at,
                last_updated_utc=last_updated_utc,
                error=None,
            )
            logger.info(
                "Contact delta sync completed",
                tenant_id=tenant_id,
                contacts=len(contacts),
                updates=updated_count,
                since=since.isoformat(),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Contact delta sync failed", tenant_id=tenant_id, error=str(exc))
            self._update_status(tenant_id, status="error", error=str(exc))
