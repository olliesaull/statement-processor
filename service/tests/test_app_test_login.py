"""Tests for the /test-login route.

The route only exists when STAGE=local (set via .env in the service directory,
which is loaded by config.py at import time). It seeds the Flask session with
fake Xero credentials and redirects to /tenant_management, allowing browser
tests to bypass OAuth.
"""

from __future__ import annotations

import tempfile

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _app():
    """Import the Flask app once and reconfigure it for testing.

    Using scope="module" avoids repeated SSM calls (which happen at config
    import time) while keeping the test session isolated from other modules.
    The session backend is overridden to filesystem so no Redis/Valkey
    connection is required.
    """
    from flask_session import Session

    import app as app_module

    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_test_login_")
    app_module.app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SESSION_TYPE="filesystem",
        SESSION_FILE_DIR=tmpdir,
        SECRET_KEY="test-secret-key-test-login",
    )
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app):
    """Return a bare test client with no pre-seeded session.

    /test-login has no auth decorator, so no monkeypatching of auth helpers
    is needed — the route is accessible without any session state.
    """
    with _app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# GET /test-login
# ---------------------------------------------------------------------------


def test_test_login_returns_302_and_seeds_session(client, monkeypatch) -> None:
    """GET /test-login must redirect to /tenant_management and seed session keys.

    Verifies:
    - HTTP 302 response
    - Location header points to /tenant_management
    - Session contains xero_oauth2_token, xero_tenant_id, xero_tenant_name,
      xero_tenants, and xero_user_email with values derived from the env vars
    """
    # Provide the required env vars that the route reads at request time.
    monkeypatch.setenv("PLAYWRIGHT_TENANT_ID", "test-tenant-abc")
    monkeypatch.setenv("PLAYWRIGHT_TENANT_NAME", "Test Tenant Ltd")

    response = client.get("/test-login")

    # Route must redirect — follow_redirects is False (default) so we see the
    # raw 302 rather than the rendered destination page.
    assert response.status_code == 302
    assert "/tenant_management" in response.headers["Location"]

    # Confirm every expected session key was written with the correct values.
    with client.session_transaction() as sess:
        assert "xero_oauth2_token" in sess, "xero_oauth2_token must be seeded"
        assert sess["xero_tenant_id"] == "test-tenant-abc"
        assert sess["xero_tenant_name"] == "Test Tenant Ltd"
        assert sess["xero_tenants"] == [{"tenantId": "test-tenant-abc", "tenantName": "Test Tenant Ltd"}]
        assert sess["xero_user_email"] == "claude@local-test.dev"
