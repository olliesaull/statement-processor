"""Tests for auth and session helpers in utils/auth.py.

Covers token sanitization, cookie consent, session cookies,
unauthorized-redirect logic, and the xero_token_required,
active_tenant_required, block_when_loading, and route_handler_logging
decorators.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from flask import Flask, session

import utils.auth as auth_module
from tenant_data_repository import TenantStatus
from utils.auth import (
    COOKIE_CONSENT_COOKIE_NAME,
    SESSION_IS_SET_COOKIE_MAX_AGE_SECONDS,
    SESSION_IS_SET_COOKIE_NAME,
    RedirectToLogin,
    _sanitize_xero_token,
    active_tenant_required,
    block_when_loading,
    clear_session_is_set_cookie,
    has_cookie_consent,
    raise_for_unauthorized,
    route_handler_logging,
    set_session_is_set_cookie,
    xero_token_required,
)

# ---------------------------------------------------------------------------
# Minimal Flask test app
# ---------------------------------------------------------------------------


@pytest.fixture()
def app():
    """Minimal Flask app for testing request context and decorators."""
    test_app = Flask(__name__)
    test_app.config["SECRET_KEY"] = "test-secret"
    test_app.config["SESSION_TYPE"] = "filesystem"
    test_app.config["SESSION_COOKIE_SECURE"] = False

    # Register stub routes so url_for resolves the Blueprint-prefixed
    # endpoint names used by the auth decorators.
    from flask import Blueprint  # pylint: disable=import-outside-toplevel

    auth_stub = Blueprint("auth", __name__)

    @auth_stub.route("/login")
    def login():
        return "login page"

    public_stub = Blueprint("public", __name__)

    @public_stub.route("/cookies")
    def cookies():
        return "cookies page"

    tenants_stub = Blueprint("tenants", __name__)

    @tenants_stub.route("/tenant_management")
    def tenant_management():
        return "tenant management"

    test_app.register_blueprint(auth_stub)
    test_app.register_blueprint(public_stub)
    test_app.register_blueprint(tenants_stub)

    return test_app


# ---------------------------------------------------------------------------
# _sanitize_xero_token
# ---------------------------------------------------------------------------


class TestSanitizeXeroToken:
    """Filter token dicts to known Xero SDK fields."""

    def test_keeps_known_fields(self):
        token = {"access_token": "abc", "refresh_token": "def", "expires_in": 1800, "expires_at": 9999999999.0, "token_type": "Bearer", "scope": "openid", "id_token": "jwt-here"}
        result = _sanitize_xero_token(token)
        assert result == token

    def test_strips_unknown_fields(self):
        """Authlib OIDC userinfo fields should be removed."""
        token = {"access_token": "abc", "userinfo": {"email": "test@x.com"}, "extra_field": True}
        result = _sanitize_xero_token(token)
        assert "userinfo" not in result
        assert "extra_field" not in result
        assert result["access_token"] == "abc"

    def test_returns_none_for_none_input(self):
        assert _sanitize_xero_token(None) is None

    def test_returns_none_for_non_dict(self):
        assert _sanitize_xero_token("not-a-dict") is None

    def test_returns_empty_dict_for_empty_input(self):
        assert _sanitize_xero_token({}) == {}


# ---------------------------------------------------------------------------
# has_cookie_consent
# ---------------------------------------------------------------------------


class TestHasCookieConsent:
    """Check the cookie_consent cookie from the request."""

    def test_returns_true_when_consent_cookie_is_true(self, app):
        with app.test_request_context(headers={"Cookie": f"{COOKIE_CONSENT_COOKIE_NAME}=true"}):
            assert has_cookie_consent() is True

    def test_returns_true_case_insensitive(self, app):
        with app.test_request_context(headers={"Cookie": f"{COOKIE_CONSENT_COOKIE_NAME}=True"}):
            # "True" → strip/lower → "true"
            assert has_cookie_consent() is True

    def test_returns_false_when_cookie_missing(self, app):
        with app.test_request_context():
            assert has_cookie_consent() is False

    def test_returns_false_when_cookie_is_false(self, app):
        with app.test_request_context(headers={"Cookie": f"{COOKIE_CONSENT_COOKIE_NAME}=false"}):
            assert has_cookie_consent() is False

    def test_returns_false_when_cookie_is_empty(self, app):
        with app.test_request_context(headers={"Cookie": f"{COOKIE_CONSENT_COOKIE_NAME}="}):
            assert has_cookie_consent() is False


# ---------------------------------------------------------------------------
# set_session_is_set_cookie / clear_session_is_set_cookie
# ---------------------------------------------------------------------------


class TestSessionIsSetCookie:
    """UI helper cookie for showing the logout link."""

    def test_set_cookie_adds_expected_values(self, app):
        with app.test_request_context():
            from flask import make_response

            resp = make_response("ok")
            result = set_session_is_set_cookie(resp)
            # The cookie should be present in Set-Cookie header
            cookie_header = result.headers.getlist("Set-Cookie")
            cookie_str = "; ".join(cookie_header)
            assert SESSION_IS_SET_COOKIE_NAME in cookie_str
            assert "true" in cookie_str

    def test_set_cookie_returns_same_response(self, app):
        with app.test_request_context():
            from flask import make_response

            resp = make_response("ok")
            result = set_session_is_set_cookie(resp)
            assert result is resp

    def test_clear_cookie_removes_session_cookie(self, app):
        with app.test_request_context():
            from flask import make_response

            resp = make_response("ok")
            result = clear_session_is_set_cookie(resp)
            cookie_header = result.headers.getlist("Set-Cookie")
            cookie_str = "; ".join(cookie_header)
            assert SESSION_IS_SET_COOKIE_NAME in cookie_str
            # Clearing sets max-age to 0 or expires in the past
            assert "Max-Age=0" in cookie_str or "expires" in cookie_str.lower()


# ---------------------------------------------------------------------------
# raise_for_unauthorized
# ---------------------------------------------------------------------------


class TestRaiseForUnauthorized:
    """Redirect to login on 401/403 from Xero SDK errors."""

    def test_raises_redirect_on_401_status(self, app):
        with app.test_request_context():
            error = type("FakeError", (), {"status": 401})()
            with pytest.raises(RedirectToLogin):
                raise_for_unauthorized(error)

    def test_raises_redirect_on_403_status_code(self, app):
        with app.test_request_context():
            error = type("FakeError", (), {"status_code": 403})()
            with pytest.raises(RedirectToLogin):
                raise_for_unauthorized(error)

    def test_raises_redirect_on_response_status(self, app):
        """Check nested response object's status field."""
        with app.test_request_context():
            resp_obj = type("FakeResponse", (), {"status": 401})()
            error = type("FakeError", (), {"response": resp_obj})()
            with pytest.raises(RedirectToLogin):
                raise_for_unauthorized(error)

    def test_no_raise_on_500(self, app):
        """Non-auth error codes should not trigger redirect."""
        with app.test_request_context():
            error = type("FakeError", (), {"status": 500})()
            raise_for_unauthorized(error)  # Should not raise

    def test_no_raise_when_no_status_attributes(self, app):
        """Plain exceptions without status attrs are ignored."""
        with app.test_request_context():
            error = ValueError("something broke")
            raise_for_unauthorized(error)

    def test_handles_non_numeric_status(self, app):
        """Non-numeric status values are safely skipped."""
        with app.test_request_context():
            error = type("FakeError", (), {"status": "not-a-number"})()
            raise_for_unauthorized(error)  # Should not raise


# ---------------------------------------------------------------------------
# RedirectToLogin
# ---------------------------------------------------------------------------


class TestRedirectToLogin:
    """The custom HTTP exception for auth redirects."""

    def test_code_is_302(self):
        assert RedirectToLogin.code == 302

    def test_get_response_returns_redirect(self, app):
        with app.test_request_context():
            exc = RedirectToLogin()
            resp = exc.get_response()
            assert resp.status_code == 302
            assert "/login" in resp.headers.get("Location", "")


# ---------------------------------------------------------------------------
# xero_token_required decorator
# ---------------------------------------------------------------------------


class TestXeroTokenRequired:
    """Decorator enforcing cookie consent + valid Xero token."""

    def _make_app_with_protected_route(self, app):
        """Register a protected route and return the app."""

        @app.route("/protected")
        @xero_token_required
        def protected():
            return "ok"

        @app.route("/api/data")
        @xero_token_required
        def api_data():
            return '{"data": "ok"}'

        return app

    def test_redirects_to_cookies_without_consent(self, app):
        """UI route without cookie consent redirects to /cookies."""
        self._make_app_with_protected_route(app)
        with app.test_client() as client:
            resp = client.get("/protected")
            assert resp.status_code == 302
            assert "/cookies" in resp.headers["Location"]

    def test_api_returns_401_without_consent(self, app):
        """API route without cookie consent returns 401 JSON."""
        self._make_app_with_protected_route(app)
        with app.test_client() as client:
            resp = client.get("/api/data")
            assert resp.status_code == 401
            data = resp.get_json()
            assert data["error"] == "cookie_consent_required"

    def test_redirects_to_login_without_token(self, app, monkeypatch):
        """Missing token + tenant redirects UI to /login."""
        self._make_app_with_protected_route(app)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/protected")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_api_returns_401_without_token(self, app, monkeypatch):
        """API route without token returns 401 JSON."""
        self._make_app_with_protected_route(app)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/api/data")
            assert resp.status_code == 401
            data = resp.get_json()
            assert data["error"] == "auth_required"

    def test_redirects_when_token_expired(self, app, monkeypatch):
        """Expired token triggers login redirect."""
        self._make_app_with_protected_route(app)
        expired_token = {"access_token": "abc", "expires_at": time.time() - 3600}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = expired_token
                sess["xero_tenant_id"] = "tenant-123"
            resp = client.get("/protected")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]

    def test_api_returns_401_when_token_expired(self, app, monkeypatch):
        """API route with expired token returns 401."""
        self._make_app_with_protected_route(app)
        expired_token = {"access_token": "abc", "expires_at": time.time() - 3600}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = expired_token
                sess["xero_tenant_id"] = "tenant-123"
            resp = client.get("/api/data")
            assert resp.status_code == 401

    def test_passes_through_with_valid_token(self, app, monkeypatch):
        """Valid, non-expired token allows the handler to execute."""
        self._make_app_with_protected_route(app)
        valid_token = {"access_token": "abc", "expires_at": time.time() + 3600}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = valid_token
                sess["xero_tenant_id"] = "tenant-123"
            resp = client.get("/protected")
            assert resp.status_code == 200
            assert b"ok" in resp.data

    def test_api_passes_through_with_valid_token(self, app, monkeypatch):
        """Valid token on API route returns handler result directly."""
        self._make_app_with_protected_route(app)
        valid_token = {"access_token": "abc", "expires_at": time.time() + 3600}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = valid_token
                sess["xero_tenant_id"] = "tenant-123"
            resp = client.get("/api/data")
            assert resp.status_code == 200

    def test_sets_session_cookie_on_ui_success(self, app):
        """Successful UI route sets the session_is_set cookie."""
        self._make_app_with_protected_route(app)
        valid_token = {"access_token": "abc", "expires_at": time.time() + 3600}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = valid_token
                sess["xero_tenant_id"] = "tenant-123"
            resp = client.get("/protected")
            cookie_header = resp.headers.getlist("Set-Cookie")
            cookie_str = "; ".join(cookie_header)
            assert SESSION_IS_SET_COOKIE_NAME in cookie_str

    def test_marks_decorated_function_with_requires_auth(self, app):
        """The decorator sets _requires_auth=True on the wrapped function."""

        @xero_token_required
        def dummy():
            pass

        assert dummy._requires_auth is True

    def test_handles_non_numeric_expires_at(self, app):
        """Non-numeric expires_at defaults to 0 (treated as missing)."""
        self._make_app_with_protected_route(app)
        token = {"access_token": "abc", "expires_at": "not-a-number"}
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_oauth2_token"] = token
                sess["xero_tenant_id"] = "tenant-123"
            # expires_at=0 means "no expiry check" — passes through
            resp = client.get("/protected")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# active_tenant_required decorator
# ---------------------------------------------------------------------------


class TestActiveTenantRequired:
    """Decorator that requires a tenant_id in the session."""

    def _make_app_with_tenant_route(self, app):
        @app.route("/needs-tenant")
        @active_tenant_required()
        def needs_tenant():
            return "tenant ok"

        return app

    def test_redirects_to_cookies_without_consent(self, app):
        self._make_app_with_tenant_route(app)
        with app.test_client() as client:
            resp = client.get("/needs-tenant")
            assert resp.status_code == 302
            assert "/cookies" in resp.headers["Location"]

    def test_redirects_to_tenant_management_without_tenant(self, app):
        self._make_app_with_tenant_route(app)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/needs-tenant")
            assert resp.status_code == 302
            assert "/tenant_management" in resp.headers["Location"]

    def test_stores_flash_message_in_session(self, app):
        self._make_app_with_tenant_route(app)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            client.get("/needs-tenant")
            with client.session_transaction() as sess:
                assert "tenant_error" in sess

    def test_passes_through_with_tenant(self, app):
        self._make_app_with_tenant_route(app)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            resp = client.get("/needs-tenant")
            assert resp.status_code == 200
            assert b"tenant ok" in resp.data

    def test_custom_message_and_redirect(self, app):
        """Custom message and redirect_endpoint are respected."""

        @app.route("/custom-tenant")
        @active_tenant_required(message="Pick a tenant!", redirect_endpoint="auth.login", flash_key="custom_flash")
        def custom_tenant():
            return "custom ok"

        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/custom-tenant")
            assert resp.status_code == 302
            assert "/login" in resp.headers["Location"]
            with client.session_transaction() as sess:
                assert sess.get("custom_flash") == "Pick a tenant!"

    def test_sets_requires_auth_attribute(self, app):
        @active_tenant_required()
        def dummy():
            pass

        assert dummy._requires_auth is True


# ---------------------------------------------------------------------------
# block_when_loading decorator
# ---------------------------------------------------------------------------


class TestBlockWhenLoading:
    """Decorator that blocks routes while tenant is loading."""

    def _make_app_with_blocking_route(self, app, monkeypatch):
        @app.route("/blocked")
        @block_when_loading
        def blocked():
            return "allowed"

        return app

    def test_redirects_to_cookies_without_consent(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        with app.test_client() as client:
            resp = client.get("/blocked")
            assert resp.status_code == 302
            assert "/cookies" in resp.headers["Location"]

    def test_blocks_when_tenant_is_loading(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        monkeypatch.setattr(auth_module, "get_tenant_status", lambda tid: TenantStatus.LOADING)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            resp = client.get("/blocked")
            assert resp.status_code == 302
            assert "/tenant_management" in resp.headers["Location"]

    def test_blocks_when_tenant_is_load_incomplete(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        monkeypatch.setattr(auth_module, "get_tenant_status", lambda tid: TenantStatus.LOAD_INCOMPLETE)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            resp = client.get("/blocked")
            assert resp.status_code == 302

    def test_blocks_when_tenant_is_erased(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        monkeypatch.setattr(auth_module, "get_tenant_status", lambda tid: TenantStatus.ERASED)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            resp = client.get("/blocked")
            assert resp.status_code == 302

    def test_allows_when_tenant_is_free(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        monkeypatch.setattr(auth_module, "get_tenant_status", lambda tid: TenantStatus.FREE)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            resp = client.get("/blocked")
            assert resp.status_code == 200
            assert b"allowed" in resp.data

    def test_allows_when_no_tenant_in_session(self, app, monkeypatch):
        """No tenant_id means no status check — pass through."""
        self._make_app_with_blocking_route(app, monkeypatch)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/blocked")
            assert resp.status_code == 200

    def test_stores_error_message_in_session(self, app, monkeypatch):
        self._make_app_with_blocking_route(app, monkeypatch)
        monkeypatch.setattr(auth_module, "get_tenant_status", lambda tid: TenantStatus.LOADING)
        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            with client.session_transaction() as sess:
                sess["xero_tenant_id"] = "tenant-abc"
            client.get("/blocked")
            with client.session_transaction() as sess:
                assert "tenant_error" in sess


# ---------------------------------------------------------------------------
# route_handler_logging decorator
# ---------------------------------------------------------------------------


class TestRouteHandlerLogging:
    """Decorator that logs entry into route handlers."""

    def test_calls_handler_and_returns_result(self, app):
        @app.route("/logged")
        @route_handler_logging
        def logged():
            return "logged ok"

        with app.test_client() as client:
            client.set_cookie(COOKIE_CONSENT_COOKIE_NAME, "true", domain="localhost")
            resp = client.get("/logged")
            assert resp.status_code == 200
            assert b"logged ok" in resp.data

    def test_works_without_cookie_consent(self, app):
        """Logging still works without consent — tenant_id will be None."""

        @app.route("/logged-no-consent")
        @route_handler_logging
        def logged_no_consent():
            return "logged ok"

        with app.test_client() as client:
            resp = client.get("/logged-no-consent")
            assert resp.status_code == 200
