from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ContactCachePayload:
    """Serialized contact cache payload stored per tenant."""

    contacts: List[Dict[str, str]]
    last_synced_at: Optional[str] = None
    last_updated_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class ContactCacheBackend:
    """Simple backend interface for persisting cached contacts."""

    def load(self, tenant_id: str) -> Optional[ContactCachePayload]:
        raise NotImplementedError

    def save(self, tenant_id: str, payload: ContactCachePayload) -> None:
        raise NotImplementedError

    def delete(self, tenant_id: str) -> None:
        raise NotImplementedError


class FileContactCacheBackend(ContactCacheBackend):
    """File-based backend storing contact caches as JSON blobs per tenant."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, tenant_id: str) -> Path:
        safe_id = tenant_id.replace("/", "_")
        return self.base_dir / f"{safe_id}.json"

    def load(self, tenant_id: str) -> Optional[ContactCachePayload]:
        path = self._path_for(tenant_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        contacts = data.get("contacts") or []
        last_synced_at = data.get("last_synced_at")
        last_updated_utc = data.get("last_updated_utc")
        return ContactCachePayload(
            contacts=[dict(item) for item in contacts],
            last_synced_at=last_synced_at,
            last_updated_utc=last_updated_utc,
        )

    def save(self, tenant_id: str, payload: ContactCachePayload) -> None:
        path = self._path_for(tenant_id)
        tmp_path = path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload.to_dict(), handle, indent=2, sort_keys=True)
        os.replace(tmp_path, path)

    def delete(self, tenant_id: str) -> None:
        path = self._path_for(tenant_id)
        if path.exists():
            path.unlink()


class ContactCache:
    """Facade for loading and persisting contact cache payloads."""

    def __init__(self, backend: ContactCacheBackend) -> None:
        self._backend = backend

    def load(self, tenant_id: str) -> Optional[ContactCachePayload]:
        return self._backend.load(tenant_id)

    def save(self, tenant_id: str, payload: ContactCachePayload) -> None:
        self._backend.save(tenant_id, payload)

    def delete(self, tenant_id: str) -> None:
        self._backend.delete(tenant_id)
