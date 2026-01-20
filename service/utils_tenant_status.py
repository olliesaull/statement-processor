"""Tenant status helpers."""

import cache_provider
from config import logger
from tenant_data_repository import TenantDataRepository, TenantStatus


def _parse_tenant_status_value(status: object, tenant_id: str) -> TenantStatus | None:
    """Normalize a raw tenant status value into a TenantStatus enum."""
    if isinstance(status, TenantStatus):
        return status
    if isinstance(status, str):
        try:
            return TenantStatus(status)
        except ValueError:
            logger.warning(
                "Encountered unexpected tenant status value",
                tenant_id=tenant_id,
                status=status,
            )
            return None

    logger.warning("Tenant record missing status", tenant_id=tenant_id)
    return None


def get_cached_tenant_status(tenant_id: str) -> TenantStatus | None:
    """Retrieve tenant status from cache, falling back to DynamoDB if missing."""
    if not tenant_id:
        return None

    cache_instance = cache_provider.get_cache()
    cached_value = cache_instance.get(f"{tenant_id}_status") if cache_instance else None
    if cached_value:
        try:
            return TenantStatus(cached_value)
        except ValueError:
            return None

    record = TenantDataRepository.get_item(tenant_id)
    if not record:
        return None

    status_enum = _parse_tenant_status_value(record.get("TenantStatus"), tenant_id)
    if status_enum is not None:
        cache_provider.set_tenant_status_cache(tenant_id, status_enum)
    return status_enum
