from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .resource_cache import S3JSONResourceStore


@dataclass
class CreditNoteCachePayload:
    credit_notes: List[Dict[str, Any]]
    last_synced_at: Optional[str] = None
    last_updated_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class CreditNoteCache:
    def __init__(self, store: S3JSONResourceStore) -> None:
        self._store = store

    def load(self, tenant_id: str) -> Optional[CreditNoteCachePayload]:
        data = self._store.load(tenant_id)
        if not data:
            return None

        credit_notes = data.get("credit_notes") or []
        last_synced_at = data.get("last_synced_at")
        last_updated_utc = data.get("last_updated_utc")
        return CreditNoteCachePayload(
            credit_notes=[dict(item) for item in credit_notes],
            last_synced_at=last_synced_at,
            last_updated_utc=last_updated_utc,
        )

    def save(self, tenant_id: str, payload: CreditNoteCachePayload) -> None:
        self._store.save(tenant_id, payload.to_dict())

    def delete(self, tenant_id: str) -> None:
        self._store.delete(tenant_id)

