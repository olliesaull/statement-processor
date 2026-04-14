"""JSON API routes -- tenant sync, upload preflight, and banners.

All routes in this Blueprint return JSON responses.
"""

from flask import Blueprint, jsonify, request, session, url_for

from logger import logger
from sync import sync_data
from tenant_activation import executor
from tenant_billing_repository import TenantBillingRepository
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.auth import route_handler_logging, xero_token_required
from utils.statement_upload_validation import build_statement_upload_preflight

api_bp = Blueprint("api", __name__)


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
    """Trigger a background sync for the specified tenant."""
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
        return jsonify({"started": True}), 202
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        return jsonify({"error": "Failed to trigger sync"}), 500


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
