"""Auth and session helpers for the statement processor service.

Provides:
- Xero OAuth token management (get/save, client factory).
- Cookie consent and session helpers.
- Decorator stack for protecting Flask routes:
    xero_token_required  — validates token expiry; redirects or 401s.
    active_tenant_required — ensures a tenant is selected.
    block_when_loading   — blocks routes while the tenant is loading.
    reconcile_ready_required — gates routes until the initial sync completes.
    route_handler_logging — structured audit-trail log entry.
"""

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Response, current_app, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore

from config import CLIENT_ID, CLIENT_SECRET
from logger import logger
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.sync_progress import build_tenant_progress_view
from utils.tenant_status import get_tenant_status

# region Constants

SCOPES = [
    "offline_access",
    "openid",
    "profile",
    "email",
    "accounting.transactions",
    "accounting.reports.read",
    "accounting.journals.read",
    "accounting.settings",
    "accounting.contacts",
    "accounting.attachments",
    "assets",
    "projects",
    "files.read",
]
_XERO_TOKEN_FIELDS = {"access_token", "refresh_token", "expires_in", "expires_at", "token_type", "scope", "id_token"}
COOKIE_CONSENT_COOKIE_NAME = "cookie_consent"
SESSION_IS_SET_COOKIE_NAME = "session_is_set"
SESSION_IS_SET_COOKIE_MAX_AGE_SECONDS = 31 * 60

# endregion

# region Token helpers


def _sanitize_xero_token(token: dict | None) -> dict | None:
    """Filter a token payload to fields accepted by the Xero SDK.

    Args:
        token: Raw token payload from Authlib or the session.

    Returns:
        Sanitized token dict, or None when the input is not a dict.
    """
    if not isinstance(token, dict):
        return None
    # Authlib includes OIDC userinfo in the token dict; the Xero SDK rejects unknown keys.
    return {key: value for key, value in token.items() if key in _XERO_TOKEN_FIELDS}


def get_xero_oauth2_token() -> dict | None:
    """Fetch the Xero OAuth token from the session.

    Args:
        None.

    Returns:
        Sanitized token dict, or None if not set.
    """
    return _sanitize_xero_token(session.get("xero_oauth2_token"))


def save_xero_oauth2_token(token: dict) -> None:
    """Store the Xero OAuth token in the session.
    This mutates the Flask session for the current request.

    Args:
        token: Token payload from Authlib/Xero.

    Returns:
        None.
    """
    session["xero_oauth2_token"] = token


def get_xero_api_client(oauth_token: dict | None = None) -> AccountingApi:
    """Build an AccountingApi client configured for Xero OAuth.
    This may update the session or provided token dict when the SDK refreshes tokens.

    Args:
        oauth_token: Optional token dict to seed the client instead of the session.

    Returns:
        Configured AccountingApi client.
    """
    if oauth_token is None:
        token_getter = get_xero_oauth2_token
        token_saver = save_xero_oauth2_token
    else:

        def token_getter() -> dict | None:
            return _sanitize_xero_token(oauth_token)

        def token_saver(new_token: dict) -> None:
            oauth_token.update(new_token)

    api_client = ApiClient(Configuration(oauth2_token=OAuth2Token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)), pool_threads=1, oauth2_token_getter=token_getter, oauth2_token_saver=token_saver)

    if oauth_token:
        sanitized_token = _sanitize_xero_token(oauth_token)
        if sanitized_token:
            api_client.set_oauth2_token(sanitized_token)

    return AccountingApi(api_client)


# endregion

# region Cookie and session helpers


def scope_str() -> str:
    """Build the Xero OAuth scope string.

    Args:
        None.

    Returns:
        Space-separated scope string for OAuth requests.
    """
    return " ".join(SCOPES)


def has_cookie_consent() -> bool:
    """Check whether the browser has accepted essential cookie usage.

    Args:
        None.

    Returns:
        True when the consent cookie is present and set to "true", otherwise False.
    """
    cookie_value = str(request.cookies.get(COOKIE_CONSENT_COOKIE_NAME) or "").strip().lower()
    return cookie_value == "true"


def set_session_is_set_cookie(response: Response) -> Response:
    """Set the UI helper cookie used to show a logout link in the navbar.

    Args:
        response: Response that should carry the cookie update.

    Returns:
        The same response with the session state cookie set.
    """
    secure = bool(current_app.config.get("SESSION_COOKIE_SECURE", True))
    response.set_cookie(key=SESSION_IS_SET_COOKIE_NAME, value="true", max_age=SESSION_IS_SET_COOKIE_MAX_AGE_SECONDS, path="/", samesite="Lax", secure=secure)
    return response


def clear_session_is_set_cookie(response: Response) -> Response:
    """Remove the UI helper cookie used to show a logout link in the navbar.

    Args:
        response: Response that should clear the cookie.

    Returns:
        The same response with the session state cookie removed.
    """
    response.delete_cookie(key=SESSION_IS_SET_COOKIE_NAME, path="/")
    return response


# endregion

# region Auth exceptions


class RedirectToLogin(HTTPException):
    """
    Represent a redirect-to-login HTTP exception for auth failures.

    This exception belongs to the auth routing layer and is raised to short-circuit
    handlers with a 302 redirect.

    Attributes:
        code: HTTP status code for the redirect.
    """

    code = 302

    def __init__(self) -> None:
        """Initialize the redirect exception with a default description.

        Args:
            None.

        Returns:
            None.
        """
        super().__init__(description="Redirecting to login")

    def get_response(self, _environ: dict[str, Any] | None = None, _scope: dict[str, Any] | None = None) -> Response:
        """Return a redirect response to the login route.

        Args:
            environ: Optional WSGI environ mapping.
            scope: Optional ASGI scope mapping.

        Returns:
            Redirect response to the login route.
        """
        return redirect(url_for("auth.login"))


def raise_for_unauthorized(error: Exception) -> None:
    """Redirect to login when the Xero API reports unauthorized access.

    Args:
        error: Exception from the Xero SDK or wrapped HTTP layers.

    Returns:
        None.

    Raises:
        RedirectToLogin: When the error carries a 401 or 403 status code.
    """
    # Errors bubble up from different SDK layers, so check common status fields.
    potential_statuses = []
    for attr in ("status", "status_code", "code"):
        potential_statuses.append(getattr(error, attr, None))

    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status", "status_code", "code"):
            potential_statuses.append(getattr(response, attr, None))

    for status in potential_statuses:
        try:
            status_code = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue

        if status_code in {401, 403}:
            logger.info("Xero API returned unauthorized/forbidden; redirecting to login", status_code=status_code)
            raise RedirectToLogin()


# endregion

# region Route decorators


def xero_token_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Require a valid (non-expired) Xero token and active tenant for route access.
    This redirects to login for UI routes and returns 401 JSON for API routes
    when the session token is missing or expired.

    Args:
        f: Route handler to wrap.

    Returns:
        Wrapped route handler with token validation.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:  # pylint: disable=too-many-return-statements
        is_api_request = request.path.startswith("/api/")
        if not has_cookie_consent():
            logger.info("Cookie consent missing; blocking protected route", route=request.path, is_api_request=is_api_request)
            if is_api_request:
                response = jsonify({"error": "cookie_consent_required", "redirect": url_for("public.cookies")})
                response.status_code = 401
                return response
            return redirect(url_for("public.cookies"))

        tenant_id = session.get("xero_tenant_id")
        token = get_xero_oauth2_token()
        if not tenant_id or not token:
            logger.info("Missing Xero token or tenant; redirecting", route=request.path, tenant_id=tenant_id)
            if is_api_request:
                response = jsonify({"error": "auth_required"})
                response.status_code = 401
                return clear_session_is_set_cookie(response)
            return clear_session_is_set_cookie(redirect(url_for("auth.login")))

        try:
            expires_at = float(token.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0.0

        if expires_at and time.time() > expires_at:
            # Avoid hitting Xero with expired tokens and surfacing hard 401s.
            logger.info("Xero token expired; redirecting", route=request.path, tenant_id=tenant_id)
            if is_api_request:
                response = jsonify({"error": "auth_required"})
                response.status_code = 401
                return clear_session_is_set_cookie(response)
            return clear_session_is_set_cookie(redirect(url_for("auth.login")))

        result = f(*args, **kwargs)
        if is_api_request:
            return result
        # Don't re-set the session cookie if the handler cleared the session
        # (e.g., disconnect_tenant on last tenant removal).
        response = make_response(result)
        if session.get("xero_tenant_id"):
            return set_session_is_set_cookie(response)
        return response

    decorated_function._requires_auth = True  # type: ignore[attr-defined]  # pylint: disable=protected-access
    return decorated_function


def active_tenant_required(
    message: str = "Please select a tenant before continuing.", redirect_endpoint: str = "tenants.tenant_management", flash_key: str = "tenant_error"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Require an active tenant selection before the route handler runs.
    This stores a message in the session before redirecting to the tenant picker.

    Args:
        message: Message to display when no tenant is active.
        redirect_endpoint: Endpoint name to redirect to when missing a tenant.
        flash_key: Session key used to store the message.

    Returns:
        Decorator that enforces tenant selection.
    """

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not has_cookie_consent():
                return redirect(url_for("public.cookies"))
            tenant_id = session.get("xero_tenant_id")
            if tenant_id:
                return f(*args, **kwargs)
            # Use session storage so the message survives the redirect boundary.
            session[flash_key] = message
            return redirect(url_for(redirect_endpoint))

        wrapped._requires_auth = True  # type: ignore[attr-defined]  # pylint: disable=protected-access
        return wrapped

    return decorator


def block_when_loading(f: Callable[..., Any]) -> Callable[..., Any]:
    """Redirect away from routes while the active tenant is still loading.
    This stores a message in the session when blocking a request.
    This checks DynamoDB for the tenant status.

    Args:
        f: Route handler to wrap.

    Returns:
        Wrapped route handler that blocks during tenant loads.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        if not has_cookie_consent():
            return redirect(url_for("public.cookies"))
        tenant_id = session.get("xero_tenant_id")
        if tenant_id:
            status = get_tenant_status(tenant_id)
            # Also block on ERASED and LOAD_INCOMPLETE as a defensive measure.
            # In practice these are unlikely to be reached — disconnected tenants
            # are removed from the session, and reconnection resets status to
            # LOADING before any protected route runs. The check costs nothing
            # (same DynamoDB call) and guards against unexpected session state.
            if status in (TenantStatus.LOADING, TenantStatus.LOAD_INCOMPLETE, TenantStatus.ERASED):
                logger.info("Blocking route during load", route=request.path, tenant_id=tenant_id, status=str(status))
                session["tenant_error"] = "Please wait for the initial load to finish before navigating away."
                return redirect(url_for("tenants.tenant_management"))

        return f(*args, **kwargs)

    return decorated_function


def reconcile_ready_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Gate a route until the tenant's initial sync has fully reconciled.

    Reads ``ReconcileReadyAt`` from the TenantData row. When the attribute is
    unset the tenant is still in the post-contacts heavy phase (or has never
    completed a full sync), so we render ``statement.html`` in the "not ready"
    branch — which embeds an HTMX poller that hits ``/statement/<id>/wait`` and
    triggers an ``HX-Redirect`` once the data lands.

    This decorator is deliberately the innermost of the route stack:

        @route("/statement/<statement_id>")
        @active_tenant_required(...)    # 1. tenant selection
        @xero_token_required            # 2. OAuth token validity
        @route_handler_logging          # 3. audit trail entry
        @block_when_loading             # 4. LOADING / LOAD_INCOMPLETE gate
        @reconcile_ready_required       # 5. reconcile-data gate (this one)
        def statement(...): ...

    Ordering matters: the outer decorators guarantee there is a session tenant
    and a valid token before we touch ``TenantDataRepository``. ``block_when_loading``
    continues to handle the initial contacts-phase gate, so this decorator only
    fires while ``TenantStatus=SYNCING`` with no ``ReconcileReadyAt`` yet —
    narrowly scoped to the heavy-phase window.

    Args:
        f: Route handler to wrap. Expected to accept ``statement_id`` as a kwarg.

    Returns:
        Wrapped route handler that short-circuits into the not-ready view
        whenever reconciliation data is unavailable.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        tenant_id = session.get("xero_tenant_id")
        item = TenantDataRepository.get_item(tenant_id) if tenant_id else None
        reconcile_ready_at = item.get("ReconcileReadyAt") if item else None
        if reconcile_ready_at is None:
            statement_id = kwargs.get("statement_id")
            tenant_name = session.get("xero_tenant_name") or tenant_id or ""
            tenant_view = build_tenant_progress_view(tenant_id or "", tenant_name, item)
            logger.info("Rendering reconcile-not-ready view", tenant_id=tenant_id, statement_id=statement_id, route=request.path)
            return render_template(
                "statement.html",
                reconcile_not_ready=True,
                statement_id=statement_id,
                tenant_id=tenant_id,
                tenant_view=tenant_view,
                page_heading=f"Statement {statement_id}" if statement_id else "Statement",
            )
        return f(*args, **kwargs)

    return decorated_function


def route_handler_logging(function: Callable[..., Any]) -> Callable[..., Any]:
    """Log entry into route handlers.
    This writes an audit-style entry to the structured logger.

    Args:
        function: Route handler to wrap.

    Returns:
        Wrapped route handler with entry logging.
    """

    @wraps(function)
    def decorator(*args: Any, **kwargs: Any) -> Any:
        tenant_id = session.get("xero_tenant_id") if has_cookie_consent() else None
        logger.info("Entering route", route=request.path, event_type="USER_TRAIL", path=request.path, tenant_id=tenant_id)

        return function(*args, **kwargs)

    return decorator


# endregion
