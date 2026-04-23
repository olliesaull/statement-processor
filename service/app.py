"""Flask application for the statement processor service.

Creates the Flask app, configures session/CSRF/OAuth, registers Blueprints,
and defines error handlers and context processors.  Route handlers live in
the ``routes/`` package.
"""

import os
import time
from datetime import timedelta
from typing import Any

from flask import Flask, jsonify, request, session
from flask_session import Session
from flask_wtf.csrf import CSRFError, CSRFProtect

from config import CLIENT_ID, CLIENT_SECRET, FLASK_SECRET_KEY, STAGE, redis_client
from core.statement_row_palette import STATEMENT_ROW_CSS_VARIABLES
from logger import logger
from routes.api import api_bp
from routes.auth import auth_bp
from routes.billing import billing_bp
from routes.public import public_bp
from routes.seo import seo_bp
from routes.statements import statements_bp
from routes.tenants import tenants_bp
from routes.webhook import webhook_bp
from tenant_data_repository import TenantDataRepository
from ui.banner_service import get_banners
from utils.template_filters import format_last_sync, format_last_sync_iso

# python3.13 -m gunicorn --reload --bind 0.0.0.0:8080 app:app

# region App configuration and helpers


app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# Extract CSRF tokens from JSON bodies BEFORE CSRFProtect registers its
# before_request handler -- Flask runs hooks in registration order, so this
# must be first.  CloudFront strips custom headers (X-CSRFToken), so
# JavaScript POSTs include the token in the JSON body instead.
@app.before_request
def _extract_csrf_from_json_body():
    """Copy CSRF token from JSON body to WSGI environ for Flask-WTF."""
    if request.is_json and not request.headers.get("X-CSRFToken"):
        data = request.get_json(silent=True)
        if isinstance(data, dict) and data.get("csrf_token"):
            request.environ["HTTP_X_CSRFTOKEN"] = data["csrf_token"]


# Enable CSRF protection globally -- must be AFTER the JSON body hook above.
csrf = CSRFProtect(app)

app.add_template_filter(format_last_sync, "format_last_sync")
app.add_template_filter(format_last_sync_iso, "format_last_sync_iso")


MAX_UPLOAD_MB = os.getenv("MAX_UPLOAD_MB", "10")
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Configure Redis-backed server sessions with only required/useful options.
app.config.update(
    SESSION_TYPE="redis",
    SESSION_REDIS=redis_client,
    SESSION_PERMANENT=False,
    SESSION_COOKIE_SECURE=STAGE != "local",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=1860),
)

Session(app)


# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

# OAuth client and tenant activation helpers live in dedicated modules
# (oauth_client.py, tenant_activation.py) to avoid circular imports with
# route Blueprints that previously had to use deferred `from app import ...`.
from oauth_client import init_oauth  # pylint: disable=wrong-import-position

init_oauth(app)


# endregion

# region Blueprint registration


@app.before_request
def _inject_tenant_logger_context():
    """Add tenant_id to structured logger context for all requests."""
    tenant_id = session.get("xero_tenant_id")
    if tenant_id:
        logger.append_keys(tenant_id=tenant_id)


app.register_blueprint(public_bp)
app.register_blueprint(seo_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(tenants_bp)
app.register_blueprint(statements_bp)
app.register_blueprint(billing_bp)
app.register_blueprint(api_bp)
app.register_blueprint(webhook_bp)
csrf.exempt(webhook_bp)

# endregion

# region Context processors


@app.context_processor
def inject_banners():
    """Make active banners available to all templates.

    Reads the tenant's dismissed banner keys from DynamoDB (cached in the
    session for 60 seconds) and collects banners from all registered
    providers.
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        return {"banners": []}

    # Cache dismissed keys in the session to avoid a DynamoDB read on every page load.
    cache_key = "_dismissed_banners"
    cache_ts_key = "_dismissed_banners_ts"
    now = time.time()

    cached_ts = session.get(cache_ts_key, 0)
    if now - cached_ts < 60:
        # Stored as a list for JSON-safe session serialization.
        dismissed_keys = set(session.get(cache_key, []))
    else:
        try:
            dismissed_keys = TenantDataRepository.get_dismissed_banners(tenant_id)
        except Exception:
            dismissed_keys = set()
        session[cache_key] = list(dismissed_keys)
        session[cache_ts_key] = now

    return {"banners": get_banners(tenant_id, dismissed_keys)}


@app.context_processor
def _inject_statement_row_palette_css() -> dict[str, Any]:
    """Expose statement row CSS variables to every template.

    Args:
        None.

    Returns:
        Template variables containing the statement row CSS variable map.
    """
    # We render CSS variables from one shared Python palette so table colors stay consistent between web UI and the Excel export.
    # Payload is tiny so performance impact of injecting on every page is negligible
    return {"statement_row_css_variables": STATEMENT_ROW_CSS_VARIABLES}


# endregion

# region Error handlers


@app.errorhandler(CSRFError)
def handle_csrf_error(error: CSRFError):
    """Log CSRF failures with request context and return API-safe JSON.

    The upload preflight and tenant sync flows both use JavaScript ``fetch``
    requests. When Flask-WTF rejects those requests, its default HTML 400
    response gives the frontend very little to work with. This handler keeps
    browser routes simple while making API failures explicit and diagnosable.
    """
    session_cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    logger.warning(
        "CSRF validation failed",
        path=request.path,
        method=request.method,
        host=request.host,
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        content_type=request.content_type,
        content_length=request.content_length,
        csrf_error=error.description,
        csrf_header_present=bool(request.headers.get("X-CSRFToken")),
        csrf_form_token_present=bool(request.form.get("csrf_token")),
        cookie_header_present=bool(request.headers.get("Cookie")),
        session_cookie_present=session_cookie_name in request.cookies,
    )

    if request.path.startswith("/api/"):
        return jsonify({"error": "csrf_validation_failed", "message": "Security validation failed. Refresh the page and try again."}), 400

    return "Security validation failed. Refresh the page and try again.", 400


# endregion

# region Miscellaneous routes (stay on main app)


@app.route("/.well-known/<path:path>")
def chrome_devtools_ping(path):
    """Respond to Chrome DevTools well-known probes without logging 404s."""
    return "", 204  # No content, indicates "OK but nothing here"


if STAGE == "local":

    @app.route("/test-login")
    def test_login():
        """Seed the Flask session with fake auth for local browser testing.

        Only exists when STAGE=local. Bypasses Xero OAuth so Claude Code
        (or a developer) can browse authenticated pages without credentials.

        Requires PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars
        pointing to a previously-synced tenant.
        """
        from flask import redirect, url_for  # pylint: disable=import-outside-toplevel

        tenant_id = os.environ.get("PLAYWRIGHT_TENANT_ID")
        tenant_name = os.environ.get("PLAYWRIGHT_TENANT_NAME")

        if not tenant_id or not tenant_name:
            return "Set PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars", 400

        session["xero_oauth2_token"] = {"access_token": "test-token-local", "token_type": "Bearer", "expires_in": 86400, "expires_at": time.time() + 86400}
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_name
        session["xero_tenants"] = [{"tenantId": tenant_id, "tenantName": tenant_name}]
        session["xero_user_email"] = "claude@local-test.dev"
        logger.info("Test login session seeded", tenant_id=tenant_id)

        response = redirect(url_for("tenants.tenant_management"))
        response.set_cookie("cookie_consent", "true", max_age=86400, path="/")
        response.set_cookie("session_is_set", "true", max_age=86400, path="/")
        return response


# endregion
