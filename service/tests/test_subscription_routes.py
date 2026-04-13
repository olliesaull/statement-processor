"""Tests for subscription billing routes (subscribe, subscribe_create, manage_subscription).

Covers redirect guards, tier validation, and Stripe error handling.
Auth helpers are monkeypatched so xero_token_required passes on all requests.
"""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest
from cachelib import FileSystemCache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _app():
    """Import the Flask app once and reconfigure it for testing."""
    from flask_session import Session

    import app as app_module

    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_subscribe_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-subscribe")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Return a test client with Xero auth helpers bypassed."""
    import utils.auth

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = "tenant-test-123"
            sess["xero_user_email"] = "test@example.com"
            sess["xero_tenant_name"] = "Test Org Ltd"
        yield c


# ---------------------------------------------------------------------------
# GET /subscribe
# ---------------------------------------------------------------------------


def test_subscribe_shows_tiers_when_no_subscription(client, monkeypatch) -> None:
    """No active subscription should render the subscribe page with tier cards."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: None))

    response = client.get("/subscribe")
    assert response.status_code == 200
    assert b"Choose Your Plan" in response.data


def test_subscribe_redirects_to_manage_when_active(client, monkeypatch) -> None:
    """Active subscription should redirect to manage page."""
    import tenant_billing_repository

    active_state = MagicMock()
    active_state.status = "active"
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: active_state))

    response = client.get("/subscribe")
    assert response.status_code == 302
    assert "/manage-subscription" in response.headers["Location"]


# ---------------------------------------------------------------------------
# POST /subscribe/create
# ---------------------------------------------------------------------------


def test_subscribe_create_invalid_tier_redirects_back(client, monkeypatch) -> None:
    """Invalid tier_id should redirect back to /subscribe."""
    response = client.post("/subscribe/create", data={"tier_id": "tier_9999"})
    assert response.status_code == 302
    assert "/subscribe" in response.headers["Location"]


def test_subscribe_create_already_subscribed_redirects_to_manage(client, monkeypatch) -> None:
    """Already-subscribed user should be redirected to manage page."""
    import tenant_billing_repository

    active_state = MagicMock()
    active_state.status = "active"
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: active_state))

    response = client.post("/subscribe/create", data={"tier_id": "tier_50"})
    assert response.status_code == 302
    assert "/manage-subscription" in response.headers["Location"]


def test_subscribe_create_stripe_error_redirects_to_failed(client, monkeypatch) -> None:
    """Stripe API error during checkout session creation should redirect to failed page."""
    import stripe

    import routes.billing as billing_module
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: None))
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_stripe_customer_id", classmethod(lambda cls, tid: "cus_existing"))

    mock_service = MagicMock()
    mock_service.create_subscription_checkout_session.side_effect = stripe.StripeError("test error")
    monkeypatch.setattr(billing_module, "_stripe_service", mock_service)

    response = client.post("/subscribe/create", data={"tier_id": "tier_50"})
    assert response.status_code == 302
    assert "/checkout/failed" in response.headers["Location"]


# ---------------------------------------------------------------------------
# GET /subscribe/success
# ---------------------------------------------------------------------------


def test_subscribe_success_renders_confirmation(client) -> None:
    """Subscribe success page should render the confirmation."""
    response = client.get("/subscribe/success")
    assert response.status_code == 200
    assert b"all set" in response.data


# ---------------------------------------------------------------------------
# GET /manage-subscription
# ---------------------------------------------------------------------------


def test_manage_subscription_redirects_when_no_subscription(client, monkeypatch) -> None:
    """No active subscription should redirect to /subscribe."""
    import tenant_billing_repository

    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: None))

    response = client.get("/manage-subscription")
    assert response.status_code == 302
    assert "/subscribe" in response.headers["Location"]


def test_manage_subscription_shows_details_when_active(client, monkeypatch) -> None:
    """Active subscription should show subscription details."""
    import stripe

    import routes.billing as billing_module
    import tenant_billing_repository

    active_state = MagicMock()
    active_state.status = "active"
    active_state.tier_id = "tier_50"
    active_state.current_period_end = "2026-05-13T00:00:00+00:00"
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: active_state))
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 42))
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_stripe_customer_id", classmethod(lambda cls, tid: "cus_test"))

    mock_portal = MagicMock()
    mock_portal.url = "https://billing.stripe.com/session/test"
    mock_service = MagicMock()
    mock_service.create_billing_portal_session.return_value = mock_portal
    monkeypatch.setattr(billing_module, "_stripe_service", mock_service)

    response = client.get("/manage-subscription")
    assert response.status_code == 200
    assert b"50 Pages/mo" in response.data
    assert b"42" in response.data


def test_manage_subscription_handles_portal_error(client, monkeypatch) -> None:
    """Portal creation failure should still render the page without a portal link."""
    import stripe

    import routes.billing as billing_module
    import tenant_billing_repository

    active_state = MagicMock()
    active_state.status = "active"
    active_state.tier_id = "tier_50"
    active_state.current_period_end = "2026-05-13T00:00:00+00:00"
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: active_state))
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_tenant_token_balance", classmethod(lambda cls, tid: 10))
    monkeypatch.setattr(tenant_billing_repository.TenantBillingRepository, "get_stripe_customer_id", classmethod(lambda cls, tid: "cus_test"))

    mock_service = MagicMock()
    mock_service.create_billing_portal_session.side_effect = stripe.StripeError("portal error")
    monkeypatch.setattr(billing_module, "_stripe_service", mock_service)

    response = client.get("/manage-subscription")
    assert response.status_code == 200
    assert b"Manage in Stripe" not in response.data
