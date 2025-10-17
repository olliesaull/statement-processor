from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ResourceSyncState:
    """Represents sync progress and errors for a tenant-scoped resource."""

    resource: str
    status: str
    synced_count: int = 0
    total_count: Optional[int] = None
    last_synced_at: Optional[str] = None
    last_updated_utc: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource": self.resource,
            "status": self.status,
            "synced_count": self.synced_count,
            "total_count": self.total_count,
            "last_synced_at": self.last_synced_at,
            "last_updated_utc": self.last_updated_utc,
            "error": self.error,
        }

