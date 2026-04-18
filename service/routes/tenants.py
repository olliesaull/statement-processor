"""Tenant management routes -- selection, disconnection, and overview.

Provides the tenant management page where users view connected Xero
tenants, switch the active tenant, and disconnect tenants (with optional
data erasure scheduling).
"""

import os
import shutil
import time

from flask import Blueprint, redirect, render_template, request, session, url_for

from config import LOCAL_DATA_DIR
from logger import logger
from pricing_config import SUBSCRIPTION_TIERS
from tenant_activation import set_active_tenant
from tenant_billing_repository import TenantBillingRepository
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.auth import clear_session_is_set_cookie, route_handler_logging, xero_token_required
from utils.sync_progress import build_progress_view, render_sync_progress_fragment, should_poll
from utils.tenant_status import get_tenant_status

tenants_bp = Blueprint("tenants", __name__)


@tenants_bp.route("/tenant_management")
@route_handler_logging
@xero_token_required
def tenant_management():
    """Render tenant management, consuming one-time messages from session."""
    tenants = session.get("xero_tenants") or []
    current_tenant_id = session.get("xero_tenant_id")
    current_tenant = None
    tenant_ids: list[str] = []
    for tenant in tenants:
        if not isinstance(tenant, dict):
            continue
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        tenant_ids.append(tenant_id)
        if tenant_id == current_tenant_id:
            current_tenant = tenant
    # Messages are popped so they only display once.
    message = session.pop("tenant_message", None)
    error = session.pop("tenant_error", None)

    tenant_token_balances: dict[str, int] = {}
    try:
        tenant_token_balances = TenantBillingRepository.get_tenant_token_balances(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant token balances", tenant_ids=tenant_ids, error=exc)

    ct_token_balance = tenant_token_balances.get(current_tenant_id, 0) if current_tenant_id else 0

    # NOTE: This is a separate GetItem from the batch balance read above. Could be
    # combined but the extra read is negligible at current scale and keeps the
    # balance-lookup and subscription-state responsibilities cleanly separated. — reviewed 2026-04-13
    subscription_state = TenantBillingRepository.get_subscription_state(current_tenant_id) if current_tenant_id else None
    subscription_tier = SUBSCRIPTION_TIERS.get(subscription_state.tier_id) if subscription_state else None

    # Progress views for the sync-progress panel + Sync/Retry button conditional.
    # Single BatchGetItem keeps this one network call even for large tenant lists.
    try:
        tenant_rows = TenantDataRepository.get_many(tenant_ids) if tenant_ids else {}
    except Exception as exc:
        logger.exception("Failed to load tenant rows for progress panel", tenant_ids=tenant_ids, error=exc)
        tenant_rows = {}
    tenant_views = build_progress_view(tenants, tenant_rows)
    tenant_views_by_id = {view.tenant_id: view for view in tenant_views}
    polling = should_poll(tenant_views)

    logger.info("Rendering tenant_management page", current_tenant_id=current_tenant_id, tenant_ids=tenant_ids, current_tenant_token_balance=ct_token_balance)

    return render_template(
        "tenant_management.html",
        tenants=tenants,
        current_tenant=current_tenant,
        ct_token_balance=ct_token_balance,
        tenant_token_balances=tenant_token_balances,
        message=message,
        error=error,
        subscription_state=subscription_state,
        subscription_tier=subscription_tier,
        tenant_views=tenant_views,
        tenant_views_by_id=tenant_views_by_id,
        polling=polling,
        TenantStatus=TenantStatus,
    )


@tenants_bp.route("/tenants/sync-progress")
@route_handler_logging
@xero_token_required
def sync_progress():
    """Return the multi-tenant sync-progress HTMX fragment for session tenants.

    Read session tenants only — no cross-tenant reads — then BatchGetItem the
    rows in a single DynamoDB round-trip. The rendered partial embeds
    ``hx-trigger`` while at least one tenant is still syncing; once every
    tenant is reconcile-ready, polling stops.
    """
    session_tenants = session.get("xero_tenants") or []
    tenant_ids = [t.get("tenantId") for t in session_tenants if isinstance(t, dict) and t.get("tenantId")]
    logger.info("Rendering sync-progress fragment", tenant_ids=tenant_ids)
    return render_sync_progress_fragment(session_tenants)


@tenants_bp.route("/tenants/select", methods=["POST"])
@xero_token_required
@route_handler_logging
def select_tenant():
    """Persist the selected tenant in session and return to management view."""
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    logger.info("Tenant selection submitted", tenant_id=tenant_id, available=len(tenants))

    if tenant_id and any(t.get("tenantId") == tenant_id for t in tenants):
        # Update the active tenant and display a success message.
        set_active_tenant(tenant_id)
        tenant_name = session.get("xero_tenant_name") or tenant_id
        session["tenant_message"] = f"Switched to tenant: {tenant_name}."
        logger.info("Tenant switched", tenant_id=tenant_id, tenant_name=tenant_name)
    else:
        session["tenant_error"] = "Unable to select tenant. Please try again."
        logger.info("Tenant selection failed", tenant_id=tenant_id)

    return redirect(url_for("tenants.tenant_management"))


@tenants_bp.route("/tenants/disconnect", methods=["POST"])
@xero_token_required
@route_handler_logging
def disconnect_tenant():
    """Disconnect a tenant from Xero, schedule data erasure, and update session state."""
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    tenant = next((t for t in tenants if t.get("tenantId") == tenant_id), None)
    management_url = url_for("tenants.tenant_management")

    if not tenant:
        session["tenant_error"] = "Tenant not found in session."
        return redirect(management_url)

    # Validate erasure_days: default to 14 (progressive enhancement fallback).
    allowed_erasure_days = {0, 14, 365}
    raw_erasure = request.form.get("erasure_days")
    if raw_erasure is None:
        erasure_days = 14
    else:
        try:
            erasure_days = int(raw_erasure)
        except (ValueError, TypeError):
            erasure_days = -1
        if erasure_days not in allowed_erasure_days:
            session["tenant_error"] = "Invalid data deletion option. Please try again."
            return redirect(management_url)

    connection_id = tenant.get("connectionId")
    oauth_token = session.get("xero_oauth2_token")
    access_token = oauth_token.get("access_token") if isinstance(oauth_token, dict) else None
    logger.info("Tenant disconnect submitted", tenant_id=tenant_id, has_connection=bool(connection_id), erasure_days=erasure_days)

    if connection_id and access_token:
        import requests as http_requests  # pylint: disable=import-outside-toplevel

        try:
            resp = http_requests.delete(f"https://api.xero.com/connections/{connection_id}", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            if resp.status_code not in (200, 204):
                logger.error("Failed to disconnect tenant", tenant_id=tenant_id, status_code=resp.status_code, body=resp.text)
                session["tenant_error"] = "Unable to disconnect tenant from Xero."
                return redirect(management_url)
        except Exception as exc:
            logger.exception("Exception disconnecting tenant", tenant_id=tenant_id, error=exc)
            session["tenant_error"] = "An error occurred while disconnecting the tenant."
            return redirect(management_url)

    # Schedule data erasure in DynamoDB.
    erasure_epoch_ms = int(time.time() * 1000) + (erasure_days * 86_400 * 1000)
    current_status = get_tenant_status(tenant_id)
    try:
        TenantDataRepository.schedule_erasure(tenant_id, erasure_epoch_ms, current_status or TenantStatus.FREE)
        logger.info("Scheduled tenant data erasure", tenant_id=tenant_id, erasure_days=erasure_days)
    except Exception:
        logger.exception("Failed to schedule erasure -- disconnect continues", tenant_id=tenant_id)

    # Delete local cache.
    local_cache_path = os.path.join(LOCAL_DATA_DIR, tenant_id)
    shutil.rmtree(local_cache_path, ignore_errors=True)

    # Remove tenant from session.
    updated = [t for t in tenants if t.get("tenantId") != tenant_id]
    session["xero_tenants"] = updated

    if session.get("xero_tenant_id") == tenant_id:
        next_tenant_id = updated[0]["tenantId"] if updated else None
        set_active_tenant(next_tenant_id)

    logger.info("Tenant disconnected", tenant_id=tenant_id, remaining=len(updated), erasure_days=erasure_days)

    if not updated:
        # Last tenant disconnected -- log the user out.
        session.clear()
        response = redirect(url_for("public.index", logged_out=1))
        return clear_session_is_set_cookie(response)

    session["tenant_message"] = "Tenant disconnected."
    return redirect(management_url)
