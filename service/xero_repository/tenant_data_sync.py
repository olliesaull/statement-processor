from __future__ import annotations

from dataclasses import replace
from typing import Dict, Mapping, Optional

from .sync_models import ResourceSyncState


class TenantDataSyncState:
    def __init__(self, resources: Mapping[str, ResourceSyncState]) -> None:
        self._resources = dict(resources)
        self.status = self._compute_status()
        self.synced_count = sum(state.synced_count for state in self._resources.values())

        total_counts = [state.total_count for state in self._resources.values() if state.total_count is not None]
        self.total_count: Optional[int]
        if len(total_counts) == len(self._resources) and self._resources:
            self.total_count = sum(total_counts)  # type: ignore[arg-type]
        else:
            self.total_count = None

        self.last_synced_at = self._max_iso("last_synced_at")
        self.last_updated_utc = self._max_iso("last_updated_utc")
        self.errors = {name: state.error for name, state in self._resources.items() if state.error}

    def _compute_status(self) -> str:
        if not self._resources:
            return "empty"

        statuses = {name: state.status for name, state in self._resources.items()}
        if any(status == "error" for status in statuses.values()):
            return "error"
        if all(status == "ready" for status in statuses.values()):
            return "ready"
        if any(status == "syncing" for status in statuses.values()):
            return "syncing"
        if all(status == "empty" for status in statuses.values()):
            return "empty"
        return "syncing"

    def _max_iso(self, attr: str) -> Optional[str]:
        values = [getattr(state, attr) for state in self._resources.values() if getattr(state, attr)]
        if not values:
            return None
        return max(values)

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "synced_count": self.synced_count,
            "total_count": self.total_count,
            "last_synced_at": self.last_synced_at,
            "last_updated_utc": self.last_updated_utc,
            "error": self.errors or None,
            "resources": {name: state.to_dict() for name, state in self._resources.items()},
        }


class TenantDataSyncCoordinator:
    """Orchestrates background sync across multiple tenant resources."""

    def __init__(self, resources: Mapping[str, object]) -> None:
        self._resources = dict(resources)

    def ensure_background_sync(self, tenant_id: Optional[str], *, force_full: bool = False) -> None:
        for service in self._resources.values():
            service.ensure_background_sync(tenant_id, force_full=force_full)  # type: ignore[attr-defined]

    def get_status(self, tenant_id: Optional[str]) -> TenantDataSyncState:
        states = {}
        for name, service in self._resources.items():
            state = service.get_status(tenant_id)  # type: ignore[attr-defined]
            # ensure resource name is embedded for downstream consumers
            if state.resource != name:
                state = replace(state, resource=name)
            states[name] = state
        return TenantDataSyncState(states)

    def clear(self, tenant_id: Optional[str]) -> None:
        for service in self._resources.values():
            service.clear(tenant_id)  # type: ignore[attr-defined]

    def get_resource(self, name: str):
        return self._resources[name]

