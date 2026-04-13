"""Tests for the Stripe webhook route.

Covers signature verification and event dispatch. CSRF is disabled
because the webhook endpoint is exempt (Stripe authenticates via
signature verification, not session cookies).
"""

from __future__ import annotations

import json
import tempfile

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

    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_webhook_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-webhook")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app):
    """Return a test client (no auth needed for webhook endpoint)."""
    with _app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# POST /api/stripe/webhook
# ---------------------------------------------------------------------------


def test_missing_signature_header_returns_400(client, monkeypatch) -> None:
    """Missing Stripe-Signature header should return 400."""
    response = client.post("/api/stripe/webhook", data=b"{}", content_type="application/json")
    assert response.status_code == 400


def test_invalid_signature_returns_400(client, monkeypatch) -> None:
    """Invalid signature should return 400."""
    response = client.post("/api/stripe/webhook", data=b'{"type": "invoice.paid"}', content_type="application/json", headers={"Stripe-Signature": "t=123,v1=invalid_sig"})
    assert response.status_code == 400


def test_valid_signature_dispatches_event_and_returns_200(client, monkeypatch) -> None:
    """Valid signature should dispatch to handler and return 200."""
    import stripe

    import routes.webhook as webhook_module

    test_event = {"type": "invoice.paid", "data": {"object": {"id": "in_test"}}}

    # Mock construct_event to return the test event dict.
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda payload, sig, secret: test_event)

    # Mock the handler to track calls.
    handled_events: list[dict] = []
    monkeypatch.setattr(webhook_module._webhook_handler, "handle_event", lambda event: handled_events.append(event))

    response = client.post("/api/stripe/webhook", data=json.dumps(test_event), content_type="application/json", headers={"Stripe-Signature": "t=123,v1=valid_sig"})

    assert response.status_code == 200
    assert len(handled_events) == 1
    assert handled_events[0]["type"] == "invoice.paid"
