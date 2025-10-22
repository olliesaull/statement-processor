from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from config import tenant_data_table


@dataclass(frozen=True)
class TenantDataRepository:
    """Repository wrapper around the TenantData DynamoDB table."""

    _table = tenant_data_table

    @classmethod
    def get_item(cls, tenant_id: str) -> Optional[Dict[str, object]]:
        """Fetch a single tenant record by ID."""
        if not tenant_id:
            return None

        response = cls._table.get_item(Key={"TenantID": tenant_id})
        return response.get("Item")

    @classmethod
    def get_syncing_tenants(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> list[str]:
        """
        Fetch multiple tenant records concurrently and return those currently syncing.

        Args:
            tenant_ids: Iterable of tenant IDs to inspect.
            max_workers: Maximum number of concurrent lookups.

        Returns:
            List of tenant IDs where Syncing is truthy.
        """
        unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}

        if not unique_ids:
            return []

        syncing: list[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(cls.get_item, tenant_id): tenant_id for tenant_id in unique_ids}
            for future in as_completed(futures):
                tenant_id = futures[future]
                try:
                    item = future.result()
                except Exception:
                    # Swallow individual lookup failures; caller can retry if needed.
                    # By not returning we assume syncing is False.
                    continue

                if item and bool(item.get("Syncing")):
                    syncing.append(tenant_id)

        return syncing
