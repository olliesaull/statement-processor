"""Billing routes -- token purchase, billing details, and Stripe checkout pages.

Handles the buy-pages form, billing detail collection, and Stripe checkout
result pages (success, cancel, failed).  All routes require Xero
authentication.

Note: these routes use ``@xero_token_required`` but NOT
``@active_tenant_required``.  This is intentional --
``xero_token_required`` already enforces that ``xero_tenant_id`` is
present in the session (see ``utils/auth.py``), so adding
``@active_tenant_required`` would be redundant.
"""

import stripe
from flask import Blueprint, redirect, render_template, request, session, url_for

from logger import logger
from oauth_client import absolute_app_url
from pricing_config import MAX_TOKENS, MIN_TOKENS, SUBSCRIPTION_TIERS, PricingConfig
from stripe_repository import StripeRepository
from stripe_service import StripeService
from tenant_billing_repository import TenantBillingRepository
from utils.auth import route_handler_logging, xero_token_required
from utils.checkout import create_checkout_session, credit_tokens_from_checkout, generate_checkout_failure_ref, resolve_or_create_stripe_customer, validate_billing_fields

billing_bp = Blueprint("billing", __name__)

# Stripe service instance -- used by checkout routes.
_stripe_service = StripeService()


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


@billing_bp.route("/checkout/create", methods=["POST"])
@xero_token_required
@route_handler_logging
def checkout_create():
    """Accept billing details, create/reuse Stripe Customer, and create a Checkout Session.

    Uses graduated pricing (PricingConfig) and persistent Stripe customers.
    """
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

    # Build absolute URLs from DOMAIN_NAME config rather than the request
    # Host header, avoiding Host header injection (semgrep flask-url-for-external-true).
    success_url = absolute_app_url(url_for("billing.checkout_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = absolute_app_url(url_for("billing.checkout_cancel"))

    try:
        customer_id = resolve_or_create_stripe_customer(_stripe_service, tenant_id=purchase_tenant_id, billing_name=billing_name, billing_email=billing_email, address=address)
        checkout_url = create_checkout_session(_stripe_service, customer_id=customer_id, token_count=token_count, tenant_id=purchase_tenant_id, success_url=success_url, cancel_url=cancel_url)
    except stripe.StripeError:
        ref = generate_checkout_failure_ref()
        return redirect(url_for("billing.checkout_failed", ref=ref))

    if not checkout_url:
        ref = generate_checkout_failure_ref()
        return redirect(url_for("billing.checkout_failed", ref=ref))

    session.pop("pending_token_count", None)
    session.pop("pending_purchase_tenant_id", None)

    return redirect(checkout_url, code=303)


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
    # StripeObject metadata doesn't support .get(); convert to plain dict.
    metadata = stripe_session.metadata.to_dict() if stripe_session.metadata else {}
    session_tenant_id = metadata.get("tenant_id")
    if session_tenant_id != tenant_id:
        logger.warning("Session tenant_id mismatch", session_id=session_id, session_tenant_id=session_tenant_id, auth_tenant_id=tenant_id)
        return redirect(url_for("billing.checkout_failed"))

    raw_token_count = metadata.get("token_count")
    if not raw_token_count:
        logger.error("Missing token_count in Stripe session metadata", session_id=session_id, tenant_id=tenant_id)
        return redirect(url_for("billing.checkout_failed"))
    try:
        token_count = int(raw_token_count)
    except (ValueError, TypeError):
        logger.error("Invalid token_count in Stripe session metadata", session_id=session_id, raw=raw_token_count)
        return redirect(url_for("billing.checkout_failed"))

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


# ---------------------------------------------------------------------------
# Subscription routes
# ---------------------------------------------------------------------------


@billing_bp.route("/subscribe")
@xero_token_required
@route_handler_logging
def subscribe():
    """Show subscription tier cards. Redirect to manage if already subscribed."""
    tenant_id = session.get("xero_tenant_id")
    subscription_state = TenantBillingRepository.get_subscription_state(tenant_id)

    if subscription_state and subscription_state.status in ("active", "past_due"):
        return redirect(url_for("billing.manage_subscription"))

    tiers = list(SUBSCRIPTION_TIERS.values())
    return render_template("subscribe.html", tiers=tiers)


@billing_bp.route("/subscribe/create", methods=["POST"])
@xero_token_required
@route_handler_logging
def subscribe_create():
    """Create a Stripe subscription checkout session for the selected tier."""
    tenant_id = session.get("xero_tenant_id")
    tier_id = request.form.get("tier_id", "").strip()

    tier = SUBSCRIPTION_TIERS.get(tier_id)
    if not tier:
        logger.warning("Invalid tier_id in subscribe request", tier_id=tier_id, tenant_id=tenant_id)
        return redirect(url_for("billing.subscribe"))

    # Check not already subscribed.
    subscription_state = TenantBillingRepository.get_subscription_state(tenant_id)
    if subscription_state and subscription_state.status in ("active", "past_due"):
        return redirect(url_for("billing.manage_subscription"))

    # Resolve or create Stripe customer (reuse persistent customer).
    existing_customer_id = TenantBillingRepository.get_stripe_customer_id(tenant_id)
    if not existing_customer_id:
        customer_id = _stripe_service.create_customer(name=session.get("xero_tenant_name", ""), email=session.get("xero_user_email", ""), address={}, tenant_id=tenant_id)
        TenantBillingRepository.set_stripe_customer_id(tenant_id, customer_id)
    else:
        customer_id = existing_customer_id

    success_url = absolute_app_url(url_for("billing.subscribe_success"))
    cancel_url = absolute_app_url(url_for("billing.subscribe"))

    try:
        checkout_session = _stripe_service.create_subscription_checkout_session(
            customer_id=customer_id, stripe_price_id=tier.stripe_price_id, tenant_id=tenant_id, tier_id=tier_id, token_count=tier.tokens_per_month, success_url=success_url, cancel_url=cancel_url
        )
    except stripe.StripeError:
        logger.exception("Failed to create subscription checkout session", tenant_id=tenant_id, tier_id=tier_id)
        ref = generate_checkout_failure_ref()
        return redirect(url_for("billing.checkout_failed", ref=ref))

    return redirect(checkout_session.url, code=303)


@billing_bp.route("/subscribe/success")
@xero_token_required
@route_handler_logging
def subscribe_success():
    """Simple confirmation — tokens are credited via webhook, not here."""
    return render_template("subscribe_success.html")


@billing_bp.route("/manage-subscription")
@xero_token_required
@route_handler_logging
def manage_subscription():
    """Show subscription status and Stripe Customer Portal link."""
    tenant_id = session.get("xero_tenant_id")
    subscription_state = TenantBillingRepository.get_subscription_state(tenant_id)
    subscription_tier = SUBSCRIPTION_TIERS.get(subscription_state.tier_id) if subscription_state else None

    if not subscription_state or subscription_state.status not in ("active", "past_due"):
        return redirect(url_for("billing.subscribe"))

    token_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)

    # Create Stripe Customer Portal session for self-service management.
    customer_id = TenantBillingRepository.get_stripe_customer_id(tenant_id)
    portal_url = ""
    if customer_id:
        try:
            portal_session = _stripe_service.create_billing_portal_session(customer_id=customer_id, return_url=absolute_app_url(url_for("billing.manage_subscription")))
            portal_url = portal_session.url
        except stripe.StripeError:
            logger.exception("Failed to create billing portal session", tenant_id=tenant_id)

    return render_template("manage_subscription.html", subscription_state=subscription_state, subscription_tier=subscription_tier, token_balance=token_balance, portal_url=portal_url)
