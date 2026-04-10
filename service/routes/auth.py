"""Authentication routes -- Xero OAuth login, callback, and logout.

Handles the full OAuth2/OIDC flow with Xero: initiating login, processing
the callback (token exchange, tenant discovery, session setup), and logout.
"""

import secrets

from authlib.integrations.base_client.errors import OAuthError
from flask import Blueprint, redirect, request, session, url_for

from logger import logger
from utils.auth import clear_session_is_set_cookie, has_cookie_consent, route_handler_logging, save_xero_oauth2_token, scope_str, set_session_is_set_cookie
from utils.email import send_login_notification_email

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login")
@route_handler_logging
def login():
    """Start the Xero OAuth flow and redirect to the authorize URL."""
    # Import here to avoid circular dependency -- oauth is initialized in app.py
    # and not available at module level.
    from app import _absolute_app_url, oauth  # pylint: disable=import-outside-toplevel

    logger.info("Login initiated")
    if not has_cookie_consent():
        logger.info("Login blocked; cookie consent missing")
        return redirect(url_for("public.cookies"))

    # OIDC nonce ties the auth response to this browser session.
    nonce = secrets.token_urlsafe(24)
    session["oauth_nonce"] = nonce

    callback_url = _absolute_app_url(url_for("auth.callback"))
    logger.info("Redirecting to Xero authorization", scope_count=len(scope_str().split()))
    # Authlib stores state/nonce in session and builds the authorize URL.
    # Building the callback from DOMAIN_NAME keeps the OAuth flow aligned with
    # the canonical public host without adding Flask-side host redirects.
    return oauth.xero.authorize_redirect(redirect_uri=callback_url, nonce=nonce)


@auth_bp.route("/callback")
@route_handler_logging
def callback():  # pylint: disable=too-many-return-statements
    """Handle the OAuth callback, validate tokens, and load tenant context."""
    # Import here to avoid circular dependency -- these are initialized in app.py.
    from app import _set_active_tenant, _trigger_initial_sync_if_required, oauth  # pylint: disable=import-outside-toplevel

    if not has_cookie_consent():
        logger.info("OAuth callback blocked; cookie consent missing")
        return redirect(url_for("public.cookies"))

    # Handle user-denied or error cases
    error = request.args.get("error")
    if error is not None:
        error_description = request.args.get("error_description") or error
        logger.error("OAuth error", error_code=400, error_description=error_description, error=error)
        return f"OAuth error: {error_description}", 400, {"Content-Type": "text/plain; charset=utf-8"}

    try:
        tokens = oauth.xero.authorize_access_token()
    except OAuthError as exc:
        error_description = exc.description or exc.error
        logger.error("OAuth error", error_code=400, error_description=error_description, error=exc.error)
        return f"OAuth error: {error_description}", 400, {"Content-Type": "text/plain; charset=utf-8"}

    if not isinstance(tokens, dict):
        logger.error("Invalid token response from Xero", error_code=400)
        return "Invalid token response from Xero", 400

    # id_token is required for OIDC claim + nonce validation.
    if not tokens.get("id_token"):
        logger.error("Missing id_token in OAuth response", error_code=400)
        return "Missing id_token in OAuth response", 400

    nonce = session.pop("oauth_nonce", None)
    # Require the original nonce so we can validate the id_token against it.
    if not nonce:
        logger.error("Missing OAuth nonce in session", error_code=400)
        return "Missing OAuth nonce in session", 400

    try:
        # Validates signature + standard claims and checks nonce matches session.
        # Capture claims so we can extract the user email for Stripe customer creation.
        claims = oauth.xero.parse_id_token(tokens, nonce=nonce)
    except Exception as exc:
        logger.exception("Failed to validate id_token", error=str(exc))
        return "Invalid id_token", 400

    # Store the authenticated user's email and name for Stripe Customer
    # creation and login notifications. The "profile" OIDC scope provides
    # given_name/family_name claims. Authlib has already validated the
    # token above so claims are trustworthy.
    if claims is not None:
        session["xero_user_email"] = claims.get("email", "")
        given = claims.get("given_name", "")
        family = claims.get("family_name", "")
        session["xero_user_name"] = f"{given} {family}".strip() or ""

    save_xero_oauth2_token(tokens)
    access_token = tokens.get("access_token")

    import requests as http_requests  # pylint: disable=import-outside-toplevel

    conn_res = http_requests.get("https://api.xero.com/connections", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)

    try:
        conn_res.raise_for_status()
    except http_requests.exceptions.HTTPError:
        logger.error("Xero connections API request failed", status_code=conn_res.status_code)
        return "Failed to retrieve Xero connections. Please try again.", 400, {"Content-Type": "text/plain; charset=utf-8"}
    connections = conn_res.json()
    if not connections:
        logger.error("No Xero connections found for this user.", error_code=400)
        return "No Xero connections found for this user.", 400

    tenants = [{"tenantId": conn.get("tenantId"), "tenantName": conn.get("tenantName"), "connectionId": conn.get("id")} for conn in connections if conn.get("tenantId")]

    current = session.get("xero_tenant_id")
    tenant_ids = [t["tenantId"] for t in tenants]

    # Store the latest tenant list before updating the active tenant.
    session["xero_tenants"] = tenants

    for tid in tenant_ids:
        _trigger_initial_sync_if_required(tid)

    if current in tenant_ids:
        _set_active_tenant(current)
    elif tenant_ids:
        first_tenant = tenant_ids[0]
        _set_active_tenant(first_tenant)
    else:
        _set_active_tenant(None)

    logger.info("OAuth callback processed", tenants=len(tenants))

    # Fire-and-forget login notification -- never blocks the login flow.
    active_tenant = next((t for t in tenants if t["tenantId"] == session.get("xero_tenant_id")), None)
    send_login_notification_email(
        tenant_name=active_tenant["tenantName"] if active_tenant else "Unknown",
        user_name=session.get("xero_user_name") or session.get("xero_user_email", "Unknown"),
        user_email=session.get("xero_user_email", ""),
    )

    response = redirect(url_for("tenants.tenant_management"))
    return set_session_is_set_cookie(response)


@auth_bp.route("/logout")
@route_handler_logging
def logout():
    """Clear the session and return to the landing page."""
    logger.info("Logout requested", had_tenant=bool(session.get("xero_tenant_id")))
    session.clear()
    response = redirect(url_for("public.index"))
    return clear_session_is_set_cookie(response)
