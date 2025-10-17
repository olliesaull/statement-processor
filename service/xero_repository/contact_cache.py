from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from .resource_cache import S3JSONResourceStore


@dataclass
class ContactCachePayload:
    """Serialized contact cache payload stored per tenant."""

    contacts: List[Dict[str, str]]
    last_synced_at: Optional[str] = None
    last_updated_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class ContactCache:
    """Facade for loading and persisting contact cache payloads."""

    def __init__(self, store: S3JSONResourceStore) -> None:
        self._store = store

    def load(self, tenant_id: str) -> Optional[ContactCachePayload]:
        data = self._store.load(tenant_id)
        if not data:
            return None

        contacts = data.get("contacts") or []
        last_synced_at = data.get("last_synced_at")
        last_updated_utc = data.get("last_updated_utc")
        return ContactCachePayload(
            contacts=[dict(item) for item in contacts],
            last_synced_at=last_synced_at,
            last_updated_utc=last_updated_utc,
        )

    def save(self, tenant_id: str, payload: ContactCachePayload) -> None:
        self._store.save(tenant_id, payload.to_dict())

    def delete(self, tenant_id: str) -> None:
        self._store.delete(tenant_id)

