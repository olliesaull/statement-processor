"""Auth and session helpers for the statement processor service."""

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Response, redirect, request, session, url_for
from werkzeug.exceptions import HTTPException
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore

from config import CLIENT_ID, CLIENT_SECRET
from logger import logger
from tenant_data_repository import TenantStatus
from utils.tenant_status import get_cached_tenant_status

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


def scope_str() -> str:
    """Build the Xero OAuth scope string.

    Args:
        None.

    Returns:
        Space-separated scope string for OAuth requests.
    """
    return " ".join(SCOPES)


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
        return redirect(url_for("login"))


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


def xero_token_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Require a valid (non-expired) Xero token and active tenant for route access.
    This redirects to login when the session token is missing or expired.

    Args:
        f: Route handler to wrap.

    Returns:
        Wrapped route handler with token validation.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        tenant_id = session.get("xero_tenant_id")
        token = get_xero_oauth2_token()
        if not tenant_id or not token:
            logger.info("Missing Xero token or tenant; redirecting", route=request.path, tenant_id=tenant_id)
            return redirect(url_for("login"))

        try:
            expires_at = float(token.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0.0

        if expires_at and time.time() > expires_at:
            # Avoid hitting Xero with expired tokens and surfacing hard 401s.
            logger.info("Xero token expired; redirecting", route=request.path, tenant_id=tenant_id)
            return redirect(url_for("login"))

        return f(*args, **kwargs)

    return decorated_function


def active_tenant_required(
    message: str = "Please select a tenant before continuing.", redirect_endpoint: str = "tenant_management", flash_key: str = "tenant_error"
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
            tenant_id = session.get("xero_tenant_id")
            if tenant_id:
                return f(*args, **kwargs)
            # Use session storage so the message survives the redirect boundary.
            session[flash_key] = message
            return redirect(url_for(redirect_endpoint))

        return wrapped

    return decorator


def block_when_loading(f: Callable[..., Any]) -> Callable[..., Any]:
    """Redirect away from routes while the active tenant is still loading.
    This stores a message in the session when blocking a request.
    This checks the in-process cache first and falls back to DynamoDB for safety.

    Args:
        f: Route handler to wrap.

    Returns:
        Wrapped route handler that blocks during tenant loads.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any) -> Any:
        tenant_id = session.get("xero_tenant_id")
        if tenant_id:
            status = get_cached_tenant_status(tenant_id)
            if status == TenantStatus.LOADING:
                logger.info("Blocking route during load", route=request.path, tenant_id=tenant_id)
                session["tenant_error"] = "Please wait for the initial load to finish before navigating away."
                return redirect(url_for("tenant_management"))

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
        tenant_id = session.get("xero_tenant_id")
        logger.info("Entering route", route=request.path, event_type="USER_TRAIL", path=request.path, tenant_id=tenant_id)

        return function(*args, **kwargs)

    return decorator
