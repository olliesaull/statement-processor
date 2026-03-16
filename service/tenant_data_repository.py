"""
Repository helpers for tenant metadata stored in DynamoDB.

Provides:
- A typed ``TenantStatus`` enum
- Lookups for individual tenants and bulk status/token balance checks
"""

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from config import tenant_data_table


class TenantStatus(StrEnum):
    """Known tenant processing states."""

    FREE = "FREE"
    SYNCING = "SYNCING"
    LOADING = "LOADING"


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

    @staticmethod
    def _determine_token_balance(item: dict[str, Any]) -> int:
        """Extract an integer token balance from a DynamoDB record."""
        raw_balance = item.get("TokenBalance")

        if raw_balance is None:
            return 0

        if isinstance(raw_balance, (int, Decimal)):
            return int(raw_balance)

        return 0

    @classmethod
    def get_item(cls, tenant_id: str) -> dict[str, object] | None:
        """Fetch a single tenant record by ID."""
        if not tenant_id:
            return None

        response = cls._table.get_item(Key={"TenantID": tenant_id})
        return response.get("Item")

    @classmethod
    def get_tenant_token_balance(cls, tenant_id: str | None) -> int:
        """Fetch the current token balance snapshot for one tenant."""
        item = cls.get_item((tenant_id or "").strip())
        if not item:
            return 0
        return cls._determine_token_balance(item)

    @classmethod
    def _get_items_by_tenant_id(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, dict[str, object] | None]:
        """Fetch multiple tenant records concurrently."""
        unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}

        if not unique_ids:
            return {}

        items: dict[str, dict[str, object] | None] = dict.fromkeys(unique_ids)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(cls.get_item, tenant_id): tenant_id for tenant_id in unique_ids}
            for future in as_completed(futures):
                tenant_id = futures[future]
                try:
                    items[tenant_id] = future.result()
                except Exception:
                    # Swallow individual lookup failures; caller can retry if needed.
                    continue

        return items

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
    def get_tenant_token_balances(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, int]:
        """
        Fetch multiple tenant records concurrently and return their token balances.

        Args:
            tenant_ids: Iterable of tenant IDs to inspect.
            max_workers: Maximum number of concurrent lookups.

        Returns:
            Mapping of tenant IDs to their current token balance.
        """
        unique_ids = {tid.strip() for tid in tenant_ids if tid and isinstance(tid, str)}
        balances: dict[str, int] = dict.fromkeys(unique_ids, 0)
        items = cls._get_items_by_tenant_id(unique_ids, max_workers=max_workers)

        for tenant_id, item in items.items():
            if item:
                balances[tenant_id] = cls._determine_token_balance(item)

        return balances
