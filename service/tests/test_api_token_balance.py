"""Tests for GET /api/tenants/<tenant_id>/token-balance.

Covers authorisation, tenant validation, and successful balance retrieval.
"""

from __future__ import annotations

import tempfile

import pytest
from cachelib import FileSystemCache


@pytest.fixture(scope="module")
def _app():
    """Import the Flask app once and reconfigure for testing."""
    from flask_session import Session

    import app as app_module

    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_token_balance_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-token-balance")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Return a test client with auth bypassed and two tenants in session."""
    import utils.auth

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = "tenant-a"
            sess["xero_tenant_name"] = "Tenant A"
            sess["xero_tenants"] = [{"tenantId": "tenant-a", "tenantName": "Tenant A"}, {"tenantId": "tenant-b", "tenantName": "Tenant B"}]
        yield c


def test_token_balance_returns_balance_for_valid_tenant(client, monkeypatch) -> None:
    """A valid tenant_id in the session returns its token balance."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 142))

    response = client.get("/api/tenants/tenant-a/token-balance")

    assert response.status_code == 200
    assert response.get_json() == {"token_balance": 142}


def test_token_balance_returns_zero_for_tenant_with_no_balance(client, monkeypatch) -> None:
    """A tenant with zero balance returns 0."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 0))

    response = client.get("/api/tenants/tenant-a/token-balance")

    assert response.status_code == 200
    assert response.get_json() == {"token_balance": 0}


def test_token_balance_rejects_tenant_not_in_session(client) -> None:
    """A tenant_id not in the user's session tenant list returns 403."""
    response = client.get("/api/tenants/tenant-unknown/token-balance")

    assert response.status_code == 403
    assert "not authorized" in response.get_json()["error"].lower()


def test_token_balance_rejects_empty_tenant_id(client) -> None:
    """An empty/whitespace tenant_id returns 400."""
    response = client.get("/api/tenants/%20/token-balance")

    assert response.status_code == 400
    assert "required" in response.get_json()["error"].lower()
