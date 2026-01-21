"""Auth and session helpers for the statement processor service."""

import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import redirect, request, session, url_for
from werkzeug.exceptions import HTTPException
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore

from config import CLIENT_ID, CLIENT_SECRET, logger
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
    """Return only fields accepted by the Xero SDK token updater."""
    if not isinstance(token, dict):
        return None
    # Authlib includes OIDC userinfo in the token dict; the Xero SDK rejects unknown keys.
    return {key: value for key, value in token.items() if key in _XERO_TOKEN_FIELDS}


def get_xero_oauth2_token() -> dict | None:
    """Return the token dict the SDK expects, or None if not set."""
    return _sanitize_xero_token(session.get("xero_oauth2_token"))


def save_xero_oauth2_token(token: dict) -> None:
    """Persist the whole token dict in the session (or your DB)."""
    session["xero_oauth2_token"] = token


def get_xero_api_client(oauth_token: dict | None = None) -> AccountingApi:
    """Create a thread-safe AccountingApi client, optionally seeded with a specific token."""
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
    """Return Xero OAuth scopes as a space-separated string."""
    return " ".join(SCOPES)


class RedirectToLogin(HTTPException):
    """HTTP exception that produces a redirect to the login route."""

    code = 302

    def __init__(self) -> None:
        """Initialize the redirect exception with a default description."""
        super().__init__(description="Redirecting to login")

    def get_response(self, environ=None, scope=None):
        """Return a redirect response to the login route."""
        return redirect(url_for("login"))


def raise_for_unauthorized(error: Exception) -> None:
    """Redirect the user to login if the Xero API returned 401/403."""
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
    """Ensure a valid (non-expired) Xero token and active tenant before route access."""

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any):
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
            logger.info("Xero token expired; redirecting", route=request.path, tenant_id=tenant_id)
            return redirect(url_for("login"))

        return f(*args, **kwargs)

    return decorated_function


def active_tenant_required(
    message: str = "Please select a tenant before continuing.", redirect_endpoint: str = "tenant_management", flash_key: str = "tenant_error"
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Ensure the user has an active tenant selected; otherwise redirect with a message."""

    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            tenant_id = session.get("xero_tenant_id")
            if tenant_id:
                return f(*args, **kwargs)
            session[flash_key] = message
            return redirect(url_for(redirect_endpoint))

        return wrapped

    return decorator


def block_when_loading(f: Callable[..., Any]) -> Callable[..., Any]:
    """
    Redirect users away from routes while their active tenant is still loading.
    Uses the in-process cache first and falls back to DynamoDB for safety.
    """

    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any):
        tenant_id = session.get("xero_tenant_id")
        if tenant_id:
            status = get_cached_tenant_status(tenant_id)
            if status == TenantStatus.LOADING:
                logger.info("Blocking route during load", route=request.path, tenant_id=tenant_id)
                session["tenant_error"] = "Please wait for the initial load to finish before navigating away."
                return redirect(url_for("tenant_management"))

        return f(*args, **kwargs)

    return decorated_function


def route_handler_logging(function):
    """Decorator that logs entry into route handlers."""

    @wraps(function)
    def decorator(*args, **kwargs):
        tenant_id = session.get("xero_tenant_id")
        logger.info("Entering route", route=request.path, event_type="USER_TRAIL", path=request.path, tenant_id=tenant_id)

        return function(*args, **kwargs)

    return decorator
