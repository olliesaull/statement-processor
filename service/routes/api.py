"""JSON API routes -- tenant sync, upload preflight, checkout, and banners.

All routes in this Blueprint return JSON responses.
"""

from flask import Blueprint, jsonify, request, session, url_for

from logger import logger
from tenant_data_repository import TenantDataRepository
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
    from app import _executor  # pylint: disable=import-outside-toplevel
    from sync import sync_data  # pylint: disable=import-outside-toplevel
    from tenant_data_repository import TenantStatus  # pylint: disable=import-outside-toplevel

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
        _executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token)  # TODO: Perhaps worth checking if there is row in DDB/files in S3
        logger.info("Manual tenant sync triggered", tenant_id=tenant_id)
        return jsonify({"started": True}), 202
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        return jsonify({"error": "Failed to trigger sync"}), 500


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


@api_bp.route("/api/checkout/create", methods=["POST"])
@xero_token_required
@route_handler_logging
def checkout_create():
    """Accept billing details, create/reuse Stripe Customer, and create a Checkout Session.

    Uses graduated pricing (PricingConfig) and persistent Stripe customers.
    """
    import stripe  # pylint: disable=import-outside-toplevel
    from flask import redirect, render_template  # pylint: disable=import-outside-toplevel

    from pricing_config import PricingConfig  # pylint: disable=import-outside-toplevel
    from stripe_service import StripeService  # pylint: disable=import-outside-toplevel
    from utils.checkout import (  # pylint: disable=import-outside-toplevel
        create_checkout_session,
        generate_checkout_failure_ref,
        resolve_or_create_stripe_customer,
        validate_billing_fields,
    )

    stripe_service = StripeService()
    tenant_id = session.get("xero_tenant_id")

    token_count = session.get("pending_token_count")
    if not token_count:
        return redirect(url_for("billing.buy_tokens"))

    # Read billing fields from the billing details form.
    billing_name = request.form.get("billing_name", "").strip()
    billing_email = request.form.get("billing_email", "").strip()
    billing_line1 = request.form.get("billing_line1", "").strip()
    billing_line2 = request.form.get("billing_line2", "").strip()
    billing_city = request.form.get("billing_city", "").strip()
    billing_state = request.form.get("billing_state", "").strip()
    billing_postal_code = request.form.get("billing_postal_code", "").strip()
    billing_country = request.form.get("billing_country", "").strip()

    # Validate required fields -- re-render billing form on failure.
    missing = validate_billing_fields(billing_name=billing_name, billing_email=billing_email, billing_line1=billing_line1, billing_postal_code=billing_postal_code, billing_country=billing_country)

    if missing:
        total_pence = PricingConfig.calculate_total_pence(token_count)
        return (
            render_template(
                "billing_details.html",
                token_count=token_count,
                total_pence=total_pence,
                saved=request.form,
                default_email=session.get("xero_user_email", ""),
                default_name=session.get("xero_tenant_name", ""),
                error=f"The following fields are required: {', '.join(missing)}.",
            ),
            400,
        )

    # Determine which tenant this purchase is for (validated against session).
    purchase_tenant_id = session.get("pending_purchase_tenant_id", tenant_id)
    tenants = session.get("xero_tenants", [])
    valid_tenant_ids = {t.get("tenantId") for t in tenants}
    if purchase_tenant_id not in valid_tenant_ids:
        logger.warning("Purchase tenant not in user's tenant list", purchase_tenant_id=purchase_tenant_id)
        return redirect(url_for("billing.buy_tokens"))

    address = {"line1": billing_line1, "line2": billing_line2, "city": billing_city, "state": billing_state, "postal_code": billing_postal_code, "country": billing_country}

    success_url = url_for("billing.checkout_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = url_for("billing.checkout_cancel", _external=True)

    try:
        customer_id = resolve_or_create_stripe_customer(stripe_service, tenant_id=purchase_tenant_id, billing_name=billing_name, billing_email=billing_email, address=address)
        checkout_url = create_checkout_session(stripe_service, customer_id=customer_id, token_count=token_count, tenant_id=purchase_tenant_id, success_url=success_url, cancel_url=cancel_url)
    except stripe.StripeError:
        ref = generate_checkout_failure_ref()
        return redirect(url_for("billing.checkout_failed", ref=ref))

    if not checkout_url:
        ref = generate_checkout_failure_ref()
        return redirect(url_for("billing.checkout_failed", ref=ref))

    session.pop("pending_token_count", None)
    session.pop("pending_purchase_tenant_id", None)

    return redirect(checkout_url, code=303)


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
