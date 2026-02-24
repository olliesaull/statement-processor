"""Tenant status helpers."""

from logger import logger
from tenant_data_repository import TenantDataRepository, TenantStatus


def _parse_tenant_status_value(status: object, tenant_id: str) -> TenantStatus | None:
    """Normalize a raw tenant status value into a TenantStatus enum."""
    if isinstance(status, TenantStatus):
        return status
    if isinstance(status, str):
        try:
            return TenantStatus(status)
        except ValueError:
            logger.warning("Encountered unexpected tenant status value", tenant_id=tenant_id, status=status)
            return None

    logger.warning("Tenant record missing status", tenant_id=tenant_id)
    return None


def get_tenant_status(tenant_id: str) -> TenantStatus | None:
    """Retrieve tenant status directly from DynamoDB."""
    if not tenant_id:
        return None

    record = TenantDataRepository.get_item(tenant_id)
    if not record:
        return None

    return _parse_tenant_status_value(record.get("TenantStatus"), tenant_id)
