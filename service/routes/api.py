"""JSON API routes -- tenant sync, upload preflight, and banners.

All routes in this Blueprint return JSON responses unless the caller
identifies as HTMX (``HX-Request: true``) for the sync endpoints — those
return the rendered ``sync_progress_panel.html`` fragment instead so the
tenant management UI can swap the panel in place.
"""

from flask import Blueprint, jsonify, render_template, request, session, url_for

from logger import logger
from sync import sync_data
from tenant_activation import executor
from tenant_billing_repository import TenantBillingRepository
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.auth import route_handler_logging, xero_token_required
from utils.statement_upload_validation import build_statement_upload_preflight
from utils.sync_progress import build_progress_view, should_poll

api_bp = Blueprint("api", __name__)

_SYNC_STALE_THRESHOLD_MS = 5 * 60 * 1000
_RETRYABLE_STATUSES = {"pending", "failed"}
_RETRY_RESOURCES = ("contacts", "credit_notes", "invoices", "payments")
_PROGRESS_ATTR_NAMES = {"contacts": "ContactsProgress", "credit_notes": "CreditNotesProgress", "invoices": "InvoicesProgress", "payments": "PaymentsProgress"}


def _is_htmx_request() -> bool:
    """True when the request was made by HTMX (``HX-Request: true`` header)."""
    return request.headers.get("HX-Request") == "true"


def _render_sync_progress_fragment():
    """Render the sync-progress panel for session tenants.

    Shared by both sync endpoints so Sync and Retry-sync buttons return the
    same HTMX-swap-target shape as the poll endpoint in
    ``tenants.sync_progress``.
    """
    session_tenants = session.get("xero_tenants") or []
    tenant_ids = [t.get("tenantId") for t in session_tenants if isinstance(t, dict) and t.get("tenantId")]
    rows = TenantDataRepository.get_many(tenant_ids) if tenant_ids else {}
    tenant_views = build_progress_view(session_tenants, rows)
    polling = should_poll(tenant_views)
    return render_template("partials/sync_progress_panel.html", tenant_views=tenant_views, polling=polling, TenantStatus=TenantStatus)


def _collect_retry_resources(tenant_item: dict | None) -> set[str]:
    """Return the set of resources that are pending or failed (retry candidates).

    Missing progress maps count as ``pending`` — e.g. legacy rows before the
    schema additions landed. Empty set means nothing to retry, which the
    endpoint translates to 409.
    """
    tenant_item = tenant_item or {}
    retryable: set[str] = set()
    for resource in _RETRY_RESOURCES:
        progress = tenant_item.get(_PROGRESS_ATTR_NAMES[resource])
        if not isinstance(progress, dict):
            # Missing entirely — treat as retryable.
            retryable.add(resource)
            continue
        status = str(progress.get("status") or "pending")
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

    HTMX callers receive the rendered sync-progress fragment (with polling
    reinstated) so the UI panel swaps in place; non-HTMX callers get the
    legacy 202 JSON response.
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

    try:
        # Fire-and-forget: sync runs in the background.
        executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token)  # TODO: Perhaps worth checking if there is row in DDB/files in S3
        logger.info("Manual tenant sync triggered", tenant_id=tenant_id)
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        if _is_htmx_request():
            return _render_sync_progress_fragment(), 500
        return jsonify({"error": "Failed to trigger sync"}), 500

    if _is_htmx_request():
        # The sync_data thread will update DDB within a second; returning the
        # fragment immediately with hx-trigger reinstated restores polling so
        # the UI reflects the new "in progress" state on the next 3s tick.
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
    acquired = TenantDataRepository.try_acquire_sync(tenant_id, target_status=TenantStatus.SYNCING, stale_threshold_ms=_SYNC_STALE_THRESHOLD_MS)
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
