"""Tests for the billing details purchase flow.

Covers POST /buy-tokens, GET /billing-details, and the billing validation
guard in POST /api/checkout/create. Auth helpers are monkeypatched so the
xero_token_required decorator passes on all requests. CSRF is disabled.
Sessions are managed via Flask-Session configured with a filesystem backend
so no Redis connection is required.
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _app():
    """Import the Flask app once and reconfigure it for testing.

    Using scope="module" avoids repeated SSM calls (which happen at config
    import time) while keeping the test session isolated.
    """
    from flask_session import Session

    import app as app_module

    # Override session backend — filesystem avoids the Redis dependency that
    # the production app uses, so tests run without a running Valkey/Redis.
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_billing_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="filesystem", SESSION_FILE_DIR=tmpdir, SECRET_KEY="test-secret-key-billing-details")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Return a test client with Xero auth helpers bypassed for each test.

    Monkeypatching the individual auth helper functions works because the
    xero_token_required decorator calls them at request time (not at
    decoration/import time), so patches applied here intercept the actual calls.
    """
    import utils.auth

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)

    with _app.test_client() as c:
        # Seed a minimal authenticated session so routes see a valid tenant.
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = "tenant-test-123"
            sess["xero_user_email"] = "test@example.com"
            sess["xero_tenant_name"] = "Test Org Ltd"
        yield c


# ---------------------------------------------------------------------------
# POST /buy-tokens
# ---------------------------------------------------------------------------


def test_buy_tokens_post_valid_stores_token_count_in_session_and_redirects(client, monkeypatch) -> None:
    """A valid token count must be stored in session and redirect to /billing-details."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 0))

    response = client.post("/buy-tokens", data={"token_count": "50"})

    assert response.status_code == 302
    assert "/billing-details" in response.headers["Location"]

    # Verify session key was written.
    with client.session_transaction() as sess:
        assert sess.get("pending_token_count") == 50


def test_buy_tokens_post_non_numeric_returns_400(client, monkeypatch) -> None:
    """A non-numeric token_count must return 400 and re-render buy_tokens.html."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 0))

    response = client.post("/buy-tokens", data={"token_count": "notanumber"})

    assert response.status_code == 400
    assert b"valid number" in response.data


def test_buy_tokens_post_below_minimum_returns_400(client, monkeypatch) -> None:
    """A token count below the minimum must return 400 and re-render buy_tokens.html."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 0))

    # STRIPE_MIN_TOKENS defaults to 10; submit 1.
    response = client.post("/buy-tokens", data={"token_count": "1"})

    assert response.status_code == 400
    assert b"between" in response.data


def test_buy_tokens_post_above_maximum_returns_400(client, monkeypatch) -> None:
    """A token count above the maximum must return 400 and re-render buy_tokens.html."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 0))

    # STRIPE_MAX_TOKENS defaults to 10000; submit 99999.
    response = client.post("/buy-tokens", data={"token_count": "99999"})

    assert response.status_code == 400
    assert b"between" in response.data


# ---------------------------------------------------------------------------
# GET /billing-details
# ---------------------------------------------------------------------------


def test_billing_details_get_redirects_when_no_pending_token_count(client, monkeypatch) -> None:
    """GET /billing-details must redirect to /buy-tokens when session key is absent."""
    # Ensure session does NOT contain pending_token_count.
    with client.session_transaction() as sess:
        sess.pop("pending_token_count", None)

    response = client.get("/billing-details")

    assert response.status_code == 302
    assert "/buy-tokens" in response.headers["Location"]


def test_billing_details_get_renders_form_with_pending_token_count(client, monkeypatch) -> None:
    """GET /billing-details must render the form when session key is present."""
    with client.session_transaction() as sess:
        sess["pending_token_count"] = 25

    response = client.get("/billing-details")

    assert response.status_code == 200
    assert b"billing_name" in response.data or b"Billing Details" in response.data


# ---------------------------------------------------------------------------
# POST /api/checkout/create — billing validation
# ---------------------------------------------------------------------------


def test_checkout_create_missing_required_billing_fields_returns_400(client, monkeypatch) -> None:
    """Missing required billing fields must return 400 and keep pending_token_count in session."""
    with client.session_transaction() as sess:
        sess["pending_token_count"] = 30

    # Submit with all required fields missing — validation returns 400 before
    # any Stripe API call is made, so no Stripe patching is needed here.
    response = client.post("/api/checkout/create", data={})

    assert response.status_code == 400
    assert b"required" in response.data

    # Session key must survive so the user can correct and resubmit.
    with client.session_transaction() as sess:
        assert sess.get("pending_token_count") == 30


def test_checkout_create_redirects_to_buy_tokens_when_no_pending_count(client) -> None:
    """POST /api/checkout/create must redirect to /buy-tokens when session key is absent."""
    with client.session_transaction() as sess:
        sess.pop("pending_token_count", None)

    response = client.post("/api/checkout/create", data={"billing_name": "Acme", "billing_email": "a@b.com", "billing_line1": "1 St", "billing_postal_code": "EC1A", "billing_country": "GB"})

    assert response.status_code == 302
    assert "/buy-tokens" in response.headers["Location"]
