"""Billing routes -- token purchase, billing details, and Stripe checkout pages.

Handles the buy-pages form, billing detail collection, and Stripe checkout
result pages (success, cancel, failed).  All routes require Xero
authentication.
"""

import stripe
from flask import Blueprint, redirect, render_template, request, session, url_for

from logger import logger
from pricing_config import MAX_TOKENS, MIN_TOKENS, PricingConfig
from stripe_repository import StripeRepository
from stripe_service import StripeService
from tenant_billing_repository import TenantBillingRepository
from utils.auth import route_handler_logging, xero_token_required
from utils.checkout import credit_tokens_from_checkout

billing_bp = Blueprint("billing", __name__)

# Stripe service instance -- used by checkout routes.
_stripe_service = StripeService()


@billing_bp.before_request
def _inject_tenant_logger_context():
    """Add tenant_id to structured logger context for all billing routes."""
    tenant_id = session.get("xero_tenant_id")
    if tenant_id:
        logger.append_keys(tenant_id=tenant_id)


@billing_bp.route("/buy-pages")
@xero_token_required
@route_handler_logging
def buy_tokens():
    """Render the token purchase form with current balance and graduated pricing info."""
    tenant_id = session.get("xero_tenant_id")
    tenants = session.get("xero_tenants", [])
    # Pre-select tenant from query param if provided (does NOT switch session tenant).
    preselected_tenant_id = request.args.get("tenant_id", tenant_id)
    # Show balance for the preselected tenant, not necessarily the active one.
    valid_tenant_ids = {t.get("tenantId") for t in tenants}
    balance_tenant_id = preselected_tenant_id if preselected_tenant_id in valid_tenant_ids else tenant_id
    token_balance = TenantBillingRepository.get_tenant_token_balance(balance_tenant_id)
    return render_template(
        "buy_tokens.html",
        token_balance=token_balance,
        min_tokens=MIN_TOKENS,
        max_tokens=MAX_TOKENS,
        pricing_tiers_json=PricingConfig.tiers_as_json(),
        tenants=tenants,
        preselected_tenant_id=preselected_tenant_id,
        current_tenant_id=tenant_id,
        error=None,
    )


@billing_bp.route("/buy-pages", methods=["POST"])
@xero_token_required
@route_handler_logging
def buy_tokens_post():
    """Validate token count and selected tenant, store in session, redirect to billing."""
    token_count_raw = request.form.get("token_count", "").strip()
    selected_tenant_id = request.form.get("selected_tenant_id", "").strip()

    # Validate the selected tenant belongs to this user.
    tenants = session.get("xero_tenants", [])
    valid_tenant_ids = {t.get("tenantId") for t in tenants}
    if selected_tenant_id not in valid_tenant_ids:
        selected_tenant_id = session.get("xero_tenant_id")

    try:
        token_count = int(token_count_raw)
    except (ValueError, TypeError):
        token_balance = TenantBillingRepository.get_tenant_token_balance(selected_tenant_id)
        return (
            render_template(
                "buy_tokens.html",
                token_balance=token_balance,
                error="Please enter a valid number of pages.",
                min_tokens=MIN_TOKENS,
                max_tokens=MAX_TOKENS,
                pricing_tiers_json=PricingConfig.tiers_as_json(),
                tenants=tenants,
                preselected_tenant_id=selected_tenant_id,
                current_tenant_id=session.get("xero_tenant_id"),
            ),
            400,
        )

    if not MIN_TOKENS <= token_count <= MAX_TOKENS:
        token_balance = TenantBillingRepository.get_tenant_token_balance(selected_tenant_id)
        return (
            render_template(
                "buy_tokens.html",
                token_balance=token_balance,
                error=f"Please enter between {MIN_TOKENS} and {MAX_TOKENS} pages.",
                min_tokens=MIN_TOKENS,
                max_tokens=MAX_TOKENS,
                pricing_tiers_json=PricingConfig.tiers_as_json(),
                tenants=tenants,
                preselected_tenant_id=selected_tenant_id,
                current_tenant_id=session.get("xero_tenant_id"),
            ),
            400,
        )

    session["pending_token_count"] = token_count
    session["pending_purchase_tenant_id"] = selected_tenant_id

    # Switch active tenant to the one being purchased for, so billing details
    # pre-fill correctly and checkout_success can verify the session tenant.
    if selected_tenant_id != session.get("xero_tenant_id"):
        selected_tenant = next((t for t in tenants if t.get("tenantId") == selected_tenant_id), None)
        if selected_tenant:
            session["xero_tenant_id"] = selected_tenant_id
            session["xero_tenant_name"] = selected_tenant.get("tenantName", "")
            logger.info("Switched active tenant for purchase", tenant_id=selected_tenant_id)

    return redirect(url_for("billing.billing_details"))


@billing_bp.route("/billing-details")
@xero_token_required
@route_handler_logging
def billing_details():
    """Render billing form with graduated pricing total and optional pre-fill from Stripe."""
    token_count = session.get("pending_token_count")
    if not token_count:
        return redirect(url_for("billing.buy_tokens"))

    total_pence = PricingConfig.calculate_total_pence(token_count)

    return render_template("billing_details.html", token_count=token_count, total_pence=total_pence, default_email=session.get("xero_user_email", ""), default_name=session.get("xero_tenant_name", ""))


@billing_bp.route("/checkout/success")
@xero_token_required
@route_handler_logging
def checkout_success():
    """Verify payment, credit tokens idempotently, and show confirmation.

    Retrieves the Stripe session to verify ``payment_status == "paid"`` and
    confirm the session belongs to the authenticated tenant before crediting
    tokens. Idempotency is enforced via ``StripeEventStoreTable`` so a page
    refresh shows the success screen without re-crediting.
    """
    tenant_id = session.get("xero_tenant_id")
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return redirect(url_for("billing.checkout_failed"))

    # Idempotency check -- already processed? Show success without re-crediting.
    if StripeRepository.is_session_processed(session_id):
        record = StripeRepository.get_processed_session(session_id)
        if record:
            new_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
            return render_template("checkout_success.html", tokens_credited=int(record["TokensCredited"]), new_balance=new_balance)
        # record is None: tiny race window between is_session_processed and
        # get_processed_session -- fall through to normal processing path.

    # Retrieve session from Stripe and verify payment status.
    try:
        stripe_session = _stripe_service.retrieve_session(session_id)
    except stripe.StripeError:
        logger.exception("Failed to retrieve Stripe session", session_id=session_id)
        return redirect(url_for("billing.checkout_failed"))

    if stripe_session.payment_status != "paid":
        logger.info("Stripe session not paid", session_id=session_id, payment_status=stripe_session.payment_status)
        return redirect(url_for("billing.checkout_failed"))

    # Security: verify the session belongs to the authenticated tenant.
    # Prevents a user who obtains another tenant's session_id from crediting
    # the wrong account.
    # Stripe metadata is a StripeObject, not a plain dict -- use bracket access.
    session_tenant_id = stripe_session.metadata["tenant_id"] if "tenant_id" in stripe_session.metadata else None
    if session_tenant_id != tenant_id:
        logger.warning("Session tenant_id mismatch", session_id=session_id, session_tenant_id=session_tenant_id, auth_tenant_id=tenant_id)
        return redirect(url_for("billing.checkout_failed"))

    token_count = int(stripe_session.metadata["token_count"])

    # Credit tokens and record the Stripe session as processed.
    new_balance = credit_tokens_from_checkout(session_id=session_id, tenant_id=tenant_id, token_count=token_count)
    return render_template("checkout_success.html", tokens_credited=token_count, new_balance=new_balance)


@billing_bp.route("/checkout/cancel")
@xero_token_required
@route_handler_logging
def checkout_cancel():
    """Render the checkout cancellation page.

    Stripe redirects here when the user clicks "Back" on the hosted checkout
    page. No tokens are credited and no Stripe session is stored.
    """
    return render_template("checkout_cancel.html")


@billing_bp.route("/checkout/failed")
@xero_token_required
@route_handler_logging
def checkout_failed():
    """Render the checkout failure page with an optional reference ID.

    Shown when Stripe session creation fails or when the success route
    detects an unexpected payment state. The ``ref`` query param is a hex
    string generated at the point of failure to help correlate log entries.
    """
    ref = request.args.get("ref", "")
    return render_template("checkout_failed.html", ref=ref)
