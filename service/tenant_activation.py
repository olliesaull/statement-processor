"""Tenant activation helpers — switching, session state, and background sync.

Extracted from ``app.py`` to break the circular import between ``app``
and ``routes/tenants`` / ``routes/api``.
"""

from concurrent.futures import ThreadPoolExecutor

from flask import session

from logger import logger
from sync import check_load_required, sync_data
from tenant_data_repository import TenantStatus

executor = ThreadPoolExecutor(max_workers=5)


def trigger_initial_sync_if_required(tenant_id: str | None) -> None:
    """Kick off an initial load if the tenant has no cached data yet.

    Args:
        tenant_id: The Xero tenant to check.  No-ops when ``None``.
    """
    if not tenant_id:
        return

    if check_load_required(tenant_id):
        oauth_token = session.get("xero_oauth2_token")
        if not oauth_token:
            logger.warning("Skipping background sync; missing OAuth token", tenant_id=tenant_id)
        else:
            executor.submit(sync_data, tenant_id, TenantStatus.LOADING, oauth_token)


def set_active_tenant(tenant_id: str | None) -> None:
    """Persist the selected tenant in the session and trigger sync if needed.

    Args:
        tenant_id: Xero tenant ID to activate, or ``None`` to clear.
    """
    tenants = session.get("xero_tenants", []) or []
    tenant_map = {t.get("tenantId"): t for t in tenants if t.get("tenantId")}
    if tenant_id and tenant_id in tenant_map:
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_map[tenant_id].get("tenantName")
        trigger_initial_sync_if_required(tenant_id)
    else:
        session.pop("xero_tenant_id", None)
        session.pop("xero_tenant_name", None)
