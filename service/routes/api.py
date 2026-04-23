"""JSON API routes -- tenant sync, upload preflight, and banners.

All routes in this Blueprint return JSON responses unless the caller
identifies as HTMX (``HX-Request: true``) for the sync endpoints — those
return the rendered ``sync_progress_panel.html`` fragment instead so the
tenant management UI can swap the panel in place.
"""

import time
from typing import Any

from flask import Blueprint, jsonify, request, session, url_for

from logger import logger
from sync import sync_data
from tenant_activation import executor
from tenant_billing_repository import TenantBillingRepository
from tenant_data_repository import ALL_SYNC_RESOURCES, SYNC_STALE_THRESHOLD_MS, ProgressStatus, TenantDataRepository, TenantStatus, _progress_attribute_name
from utils.auth import route_handler_logging, xero_token_required
from utils.statement_upload_validation import build_statement_upload_preflight
from utils.sync_progress import is_retry_recommended, render_sync_progress_fragment

api_bp = Blueprint("api", __name__)

# ``IN_PROGRESS`` is retryable because crashed-mid-fetch resources never reach
# the complete/failed write. Safe because ``try_acquire_sync``'s stale-heartbeat
# gate runs first — see decision log entry 2026-04-20 ("_RETRYABLE_STATUSES
# includes IN_PROGRESS") for the full rationale.
_RETRYABLE_STATUSES: frozenset[str] = frozenset({ProgressStatus.PENDING, ProgressStatus.FAILED, ProgressStatus.IN_PROGRESS})


def _is_htmx_request() -> bool:
    """True when the request was made by HTMX (``HX-Request: true`` header)."""
    return request.headers.get("HX-Request") == "true"


def _render_sync_progress_fragment() -> str:
    """Render the sync-progress panel for the current session.

    Gathers session + billing + retry context, issues the single BatchGetItem
    for tenant rows, and delegates to ``utils.sync_progress.render_sync_progress_fragment``.
    Sync and Retry-sync both swap the same HTMX-target shape as the poll endpoint.

    Defensive fallbacks mirror ``tenants.sync_progress``: a transient DDB read
    error returns an empty/None result so the fragment still renders rather
    than propagating a 500 back to HTMX on a 3s poll interval.
    """
    session_tenants = session.get("xero_tenants") or []
    tenant_ids = [t.get("tenantId") for t in session_tenants if isinstance(t, dict) and t.get("tenantId")]
    current_tenant_id = session.get("xero_tenant_id")
    try:
        tenant_rows = TenantDataRepository.get_many(tenant_ids) if tenant_ids else {}
    except Exception as exc:
        logger.exception("Failed to load tenant rows for sync-progress fragment", tenant_ids=tenant_ids, error=exc)
        tenant_rows = {}
    try:
        tenant_token_balances = TenantBillingRepository.get_tenant_token_balances(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant token balances for sync-progress fragment", tenant_ids=tenant_ids, error=exc)
        tenant_token_balances = {}
    try:
        subscription_state = TenantBillingRepository.get_subscription_state(current_tenant_id) if current_tenant_id else None
    except Exception as exc:
        logger.exception("Failed to load subscription state for sync-progress fragment", current_tenant_id=current_tenant_id, error=exc)
        subscription_state = None
    is_active_subscription = bool(subscription_state) and subscription_state.status == "active"
    now_ms = int(time.time() * 1000)
    return render_sync_progress_fragment(
        session_tenants,
        tenant_rows=tenant_rows,
        current_tenant_id=current_tenant_id,
        tenant_token_balances=tenant_token_balances,
        is_active_subscription=is_active_subscription,
        needs_retry_by_id={tid: is_retry_recommended(tenant_rows.get(tid), now_ms=now_ms) for tid in tenant_ids},
        now_ms=now_ms,
    )


def _collect_retry_resources(tenant_item: dict[str, Any] | None) -> set[str]:
    """Return the set of resources that are pending or failed (retry candidates).

    Missing progress maps count as ``pending`` — e.g. legacy rows before the
    schema additions landed. Empty set means nothing to retry, which the
    endpoint translates to 409.

    No staleness guard on ``IN_PROGRESS`` here: the retry-sync endpoint calls
    ``try_acquire_sync`` before this function, and that DDB condition rejects
    a live sync's fresh heartbeat. By the time we select resources, the tenant
    has already cleared the lock — so every ``IN_PROGRESS`` entry is crashed.
    ``is_retry_recommended`` (the UI button gate) is separate and does need
    an explicit staleness check because it runs without touching the lock.
    """
    tenant_item = tenant_item or {}
    retryable: set[str] = set()
    for resource in ALL_SYNC_RESOURCES:
        progress = tenant_item.get(_progress_attribute_name(resource))
        if not isinstance(progress, dict):
            # Missing entirely — treat as retryable.
            retryable.add(resource)
            continue
        status = str(progress.get("status") or ProgressStatus.PENDING)
        if status in _RETRYABLE_STATUSES:
            retryable.add(resource)
    return retryable


@api_bp.route("/api/tenant-statuses", methods=["GET"])
@xero_token_required
def tenant_status():
    """Return tenant sync statuses from DynamoDB."""
    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = [t.get("tenantId") for t in tenant_records if isinstance(t, dict)]
    try:
        tenant_statuses = TenantDataRepository.get_tenant_statuses(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant sync status", tenant_ids=tenant_ids, error=exc)
        return jsonify({"error": "Unable to determine sync status"}), 500

    return jsonify(tenant_statuses), 200


@api_bp.route("/api/tenants/<tenant_id>/sync", methods=["POST"])
@xero_token_required
def trigger_tenant_sync(tenant_id: str):
    """Trigger a background sync for the specified tenant.

    Synchronously acquires the sync lock (flipping ``TenantStatus`` to
    ``SYNCING``) before submitting ``sync_data`` to the executor so the
    HTTP response already reflects the in-flight state. HTMX callers
    receive the rendered sync-progress fragment; non-HTMX callers get
    the legacy 202 JSON response. A concurrent start returns 409.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return jsonify({"error": "TenantID is required"}), 400

    # Only allow syncs for tenants already connected in this session.
    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = {t.get("tenantId") for t in tenant_records if isinstance(t, dict)}
    if tenant_id not in tenant_ids:
        logger.info("Manual sync denied; tenant not authorized", tenant_id=tenant_id)
        return jsonify({"error": "Tenant not authorized"}), 403

    oauth_token = session.get("xero_oauth2_token")
    if not oauth_token:
        logger.warning("Manual sync denied; missing OAuth token", tenant_id=tenant_id)
        return jsonify({"error": "Missing OAuth token"}), 400

    # Acquire the sync lock synchronously so the returned fragment already
    # reflects TenantStatus=SYNCING. sync_data then runs with
    # already_acquired=True to avoid double-claiming.
    acquired = TenantDataRepository.try_acquire_sync(tenant_id, target_status=TenantStatus.SYNCING, stale_threshold_ms=SYNC_STALE_THRESHOLD_MS)
    if not acquired:
        logger.info("Manual sync rejected; another sync in flight", tenant_id=tenant_id)
        if _is_htmx_request():
            return _render_sync_progress_fragment(), 409
        return jsonify({"error": "Sync already in flight"}), 409

    try:
        executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token, already_acquired=True)
        logger.info("Manual tenant sync triggered", tenant_id=tenant_id)
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        # Release the lock we just acquired so the tenant isn't stuck in
        # SYNCING with a fresh heartbeat until the stale threshold elapses.
        try:
            TenantDataRepository.release_sync_lock(tenant_id, fallback_status=TenantStatus.FREE)
        except Exception as release_exc:
            logger.exception("Failed to release sync lock after submission failure", tenant_id=tenant_id, error=release_exc)
        if _is_htmx_request():
            return _render_sync_progress_fragment(), 500
        return jsonify({"error": "Failed to trigger sync"}), 500

    if _is_htmx_request():
        return _render_sync_progress_fragment()
    return jsonify({"started": True}), 202


@api_bp.route("/api/tenants/<tenant_id>/retry-sync", methods=["POST"])
@xero_token_required
def retry_tenant_sync(tenant_id: str):
    """Retry sync for pending or failed resources on ``LOAD_INCOMPLETE`` tenants.

    Atomically claims the sync lock via ``try_acquire_sync`` to return 409 on
    overlapping starts (the poll endpoint/panel will show the current sync's
    in-progress state regardless). Spawns ``sync_data`` with
    ``already_acquired=True`` and ``only_run_resources`` narrowed to the
    pending/failed subset so completed data isn't re-fetched.

    Returns:
        - 202 JSON / HTML fragment on success (HTMX-aware).
        - 403 when the tenant isn't in the caller's session.
        - 409 when another sync is already in flight (fresh heartbeat).
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return jsonify({"error": "TenantID is required"}), 400

    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = {t.get("tenantId") for t in tenant_records if isinstance(t, dict)}
    if tenant_id not in tenant_ids:
        logger.info("Retry sync denied; tenant not authorized", tenant_id=tenant_id)
        return jsonify({"error": "Tenant not authorized"}), 403

    oauth_token = session.get("xero_oauth2_token")
    if not oauth_token:
        logger.warning("Retry sync denied; missing OAuth token", tenant_id=tenant_id)
        return jsonify({"error": "Missing OAuth token"}), 400

    item = TenantDataRepository.get_item(tenant_id)
    resources_to_retry = _collect_retry_resources(item)
    if not resources_to_retry:
        logger.info("Retry sync denied; no pending or failed resources", tenant_id=tenant_id)
        return jsonify({"error": "Nothing to retry"}), 409

    # Acquire the sync lock synchronously so the endpoint can reflect a 409 on
    # overlap, then hand off to the executor with ``already_acquired=True`` so
    # the background thread doesn't double-claim the lock.
    acquired = TenantDataRepository.try_acquire_sync(tenant_id, target_status=TenantStatus.SYNCING, stale_threshold_ms=SYNC_STALE_THRESHOLD_MS)
    if not acquired:
        logger.info("Retry sync rejected; another sync in flight", tenant_id=tenant_id)
        if _is_htmx_request():
            # UI still needs the latest panel state even on rejection.
            return _render_sync_progress_fragment(), 409
        return jsonify({"error": "Sync already in flight"}), 409

    try:
        executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token, only_run_resources=resources_to_retry, already_acquired=True)
        logger.info("Retry sync triggered", tenant_id=tenant_id, resources=sorted(resources_to_retry))
    except Exception as exc:
        logger.exception("Failed to submit retry sync", tenant_id=tenant_id, error=exc)
        # Release the lock we just acquired — without this the tenant would
        # stay "SYNCING with fresh heartbeat" until the stale-threshold window
        # elapses, blocking legitimate retries for 5 minutes.
        try:
            TenantDataRepository.release_sync_lock(tenant_id, fallback_status=TenantStatus.LOAD_INCOMPLETE)
        except Exception as release_exc:
            logger.exception("Failed to release sync lock after submission failure", tenant_id=tenant_id, error=release_exc)
        if _is_htmx_request():
            return _render_sync_progress_fragment(), 500
        return jsonify({"error": "Failed to trigger retry"}), 500

    if _is_htmx_request():
        return _render_sync_progress_fragment()
    return jsonify({"started": True, "resources": sorted(resources_to_retry)}), 202


@api_bp.route("/api/tenants/<tenant_id>/token-balance", methods=["GET"])
@xero_token_required
@route_handler_logging
def tenant_token_balance(tenant_id: str):
    """Return the current token balance for a single tenant."""
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return jsonify({"error": "TenantID is required"}), 400

    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = {t.get("tenantId") for t in tenant_records if isinstance(t, dict)}
    if tenant_id not in tenant_ids:
        logger.info("Token balance denied; tenant not authorized", tenant_id=tenant_id)
        return jsonify({"error": "Tenant not authorized"}), 403

    balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    return jsonify({"token_balance": balance}), 200


@api_bp.route("/api/upload-statements/preflight", methods=["POST"])
@xero_token_required
@route_handler_logging
def upload_statements_preflight():
    """Count uploaded PDF pages on the server before the real upload is submitted."""
    tenant_id = (session.get("xero_tenant_id") or "").strip()
    if not tenant_id:
        logger.info("Upload preflight rejected; tenant missing")
        return jsonify({"error": "TenantID is required"}), 400

    files = [uploaded_file for uploaded_file in request.files.getlist("statements") if uploaded_file and uploaded_file.filename]
    if not files:
        logger.info("Upload preflight rejected; no files supplied", tenant_id=tenant_id)
        return jsonify({"error": "At least one statement PDF is required"}), 400

    preflight_result = build_statement_upload_preflight(tenant_id, files)
    logger.info(
        "Upload preflight evaluated",
        tenant_id=tenant_id,
        files=len(preflight_result.files),
        total_pages=preflight_result.total_pages,
        available_tokens=preflight_result.available_tokens,
        sufficient=preflight_result.is_sufficient,
        can_submit=preflight_result.can_submit,
        shortfall=preflight_result.shortfall,
    )
    payload = preflight_result.to_response_payload()
    # When the user can't afford the upload, surface a direct link to the token
    # purchase page. Injected here (not in to_response_payload) to keep the
    # validation model free of Flask URL-routing knowledge.
    if preflight_result.shortfall > 0:
        payload["buy_tokens_url"] = url_for("billing.buy_tokens")
    return jsonify(payload), 200


@api_bp.route("/api/banner/dismiss", methods=["POST"])
@xero_token_required
def api_dismiss_banner():
    """Permanently dismiss a banner for the active tenant.

    Expects JSON: {"dismiss_key": "<key>"}. Writes to the
    DismissedBanners string set on TenantData and updates
    the session cache so the banner disappears immediately.
    """
    tenant_id = session.get("xero_tenant_id")

    data = request.get_json(silent=True) or {}
    dismiss_key = data.get("dismiss_key", "")
    if not isinstance(dismiss_key, str) or not dismiss_key.strip():
        return jsonify({"error": "dismiss_key is required"}), 400

    dismiss_key = dismiss_key.strip()

    try:
        TenantDataRepository.dismiss_banner(tenant_id, dismiss_key)
    except Exception:
        logger.exception("Failed to dismiss banner", tenant_id=tenant_id, dismiss_key=dismiss_key)
        return jsonify({"error": "internal_error"}), 500

    # Update the session cache so the banner disappears immediately
    # without waiting for the 60s cache expiry.
    cached: list[str] = session.get("_dismissed_banners", [])
    if dismiss_key not in cached:
        cached.append(dismiss_key)
    session["_dismissed_banners"] = cached

    return "", 204
