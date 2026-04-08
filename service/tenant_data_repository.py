"""
Repository helpers for tenant metadata stored in DynamoDB.

Provides:
- A typed ``TenantStatus`` enum
- Lookups for individual tenants and bulk status/token balance checks
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from config import tenant_data_table
from repository_helpers import fetch_items_by_tenant_id


class TenantStatus(StrEnum):
    """Known tenant processing states."""

    FREE = "FREE"
    SYNCING = "SYNCING"
    LOADING = "LOADING"
    LOAD_INCOMPLETE = "LOAD_INCOMPLETE"
    ERASED = "ERASED"


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

    @classmethod
    def get_item(cls, tenant_id: str) -> dict[str, object] | None:
        """Fetch a single tenant record by ID."""
        if not tenant_id:
            return None

        response = cls._table.get_item(Key={"TenantID": tenant_id})
        return response.get("Item")

    @classmethod
    def _get_items_by_tenant_id(cls, tenant_ids: Iterable[str], max_workers: int = 4) -> dict[str, dict[str, object] | None]:
        """Fetch multiple tenant records concurrently."""
        return fetch_items_by_tenant_id(cls.get_item, tenant_ids, max_workers=max_workers)

    @classmethod
    def get_dismissed_banners(cls, tenant_id: str) -> set[str]:
        """Fetch the set of permanently dismissed banner keys for a tenant.

        Returns:
            Set of dismiss_key strings. Empty set if no row or no attribute.
        """
        item = cls.get_item(tenant_id)
        if not item:
            return set()
        raw = item.get("DismissedBanners")
        if isinstance(raw, set):
            return raw
        return set()

    @classmethod
    def dismiss_banner(cls, tenant_id: str, dismiss_key: str) -> None:
        """Permanently dismiss a banner for a tenant.

        Uses DynamoDB ADD on a string set, which is atomic and idempotent.

        Args:
            tenant_id: Tenant dismissing the banner.
            dismiss_key: Unique banner identifier to dismiss.
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="ADD DismissedBanners :dismiss_key", ExpressionAttributeValues={":dismiss_key": {dismiss_key}})

    @classmethod
    def schedule_erasure(cls, tenant_id: str, erasure_epoch_ms: int, current_status: TenantStatus) -> None:
        """Schedule tenant data for erasure at a future time.

        Sets EraseTenantDataTime and transitions status if the tenant was
        mid-load (LOADING -> LOAD_INCOMPLETE) or mid-sync (SYNCING -> FREE).

        Args:
            tenant_id: Tenant being disconnected.
            erasure_epoch_ms: Epoch milliseconds when data should be erased.
            current_status: Tenant's status at time of disconnect.
        """
        update_expr = "SET EraseTenantDataTime = :erasure_time"
        expr_values: dict[str, object] = {":erasure_time": erasure_epoch_ms}

        # Transition interrupted states to a safe resting state before erasure runs.
        status_transitions = {TenantStatus.LOADING: TenantStatus.LOAD_INCOMPLETE, TenantStatus.SYNCING: TenantStatus.FREE}
        new_status = status_transitions.get(current_status)
        if new_status:
            update_expr += ", TenantStatus = :new_status"
            expr_values[":new_status"] = new_status

        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression=update_expr, ExpressionAttributeValues=expr_values)

    @classmethod
    def cancel_erasure(cls, tenant_id: str) -> None:
        """Cancel a pending erasure by removing the scheduled time.

        Called when a tenant reconnects before the erasure Lambda runs.

        Args:
            tenant_id: Tenant reconnecting.
        """
        cls._table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="REMOVE EraseTenantDataTime")

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
