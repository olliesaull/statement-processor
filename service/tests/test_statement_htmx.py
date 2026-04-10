"""Tests for HTMX partial response behaviour on the statement detail route.

Verifies that:
- Normal GET requests return the full page (with DOCTYPE).
- HTMX GET requests (HX-Request: true) return only the partial.
- Normal POST complete_item redirects (302).
- HTMX POST complete_item returns the partial (200, no DOCTYPE).
"""

import tempfile
from unittest.mock import MagicMock

import pytest
from cachelib import FileSystemCache
from flask_session import Session

import app as app_module
import utils.auth

TENANT_ID = "tenant-htmx-test"
STATEMENT_ID = "stmt-htmx-001"
SAMPLE_ITEMS = [{"statement_item_id": "item-1", "columns": {"Number": "INV-001", "Date": "2025-01-15", "Amount": "100.00"}}]
SAMPLE_STATEMENT_JSON = {"statement_items": SAMPLE_ITEMS, "header_mapping": {"Number": "number", "Date": "date", "Amount": "amount"}}
SAMPLE_RECORD = {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID, "ContactName": "Test Contact", "ContactID": "contact-001", "Completed": "false", "TokenReservationStatus": "released"}


@pytest.fixture(scope="module")
def _app():
    """Module-scoped Flask app fixture.

    Reconfigures the app once per module to avoid repeated SSM/config calls
    while keeping the session backend isolated from production Redis.
    """
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_htmx_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-htmx")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Function-scoped test client with auth bypass and stubbed data layer.

    Monkeypatches auth helpers (checked at request time by decorators) and all
    data-layer calls made by the statement route, so no real AWS/Xero
    connections are required.
    """
    # Bypass auth decorators — these are called at request time, not import time.
    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)

    # Bypass block_when_loading — it calls get_tenant_status which hits DynamoDB.
    # Patching in utils.auth namespace because that's where it's imported.
    # TenantStatus.FREE is the normal post-load state that allows route access.
    from tenant_data_repository import TenantStatus

    monkeypatch.setattr(utils.auth, "get_tenant_status", lambda tenant_id: TenantStatus.FREE)

    # Stub data layer calls used by the statement route.
    # All of these are imported at the top of app.py with `from ... import X`,
    # so they must be patched in the app_module namespace (not the source module).
    monkeypatch.setattr(app_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
    monkeypatch.setattr(app_module, "fetch_json_statement", lambda *a, **kw: SAMPLE_STATEMENT_JSON)
    monkeypatch.setattr(app_module, "get_xero_data_by_contact", lambda *a, **kw: {"invoices": [], "credit_notes": [], "payments": []})
    monkeypatch.setattr(app_module, "get_statement_item_status_map", lambda *a, **kw: {})
    monkeypatch.setattr(app_module, "_persist_classification_updates", lambda **kw: None)

    # Stub statement view cache — always miss so the pipeline runs.
    monkeypatch.setattr(app_module, "get_cached_statement_view", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "cache_statement_view", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "invalidate_statement_view_cache", lambda *a, **kw: None)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = TENANT_ID
            sess["xero_user_email"] = "test@example.com"
            sess["xero_tenant_name"] = "Test Org Ltd"
        yield c


class TestStatementHtmxPartialResponse:
    """When HX-Request header is present, return the partial (no base layout)."""

    def test_normal_get_returns_full_page(self, client):
        """A standard GET (no HX-Request header) must render the full HTML page with DOCTYPE."""
        response = client.get(f"/statement/{STATEMENT_ID}")
        assert response.status_code == 200
        html = response.data.decode()
        assert "<!doctype html>" in html.lower()
        assert 'id="statement-content"' in html

    def test_htmx_get_returns_partial_only(self, client):
        """A GET with HX-Request: true must render only the partial, without DOCTYPE."""
        response = client.get(f"/statement/{STATEMENT_ID}", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        assert "<!doctype html>" not in html.lower()
        assert 'id="statement-content"' in html


class TestStatementPostHtmxResponse:
    """When HX-Request header is present on POST, return partial instead of redirect."""

    def test_normal_post_complete_item_redirects(self, client, monkeypatch):
        """A standard POST complete_item (no HX-Request) must redirect (302)."""
        monkeypatch.setattr(app_module, "set_statement_item_completed", lambda *a, **kw: None)
        response = client.post(f"/statement/{STATEMENT_ID}", data={"action": "complete_item", "statement_item_id": "item-1", "items_view": "incomplete", "show_payments": "true", "page": "1"})
        assert response.status_code == 302

    def test_htmx_post_complete_item_returns_partial(self, client, monkeypatch):
        """A POST complete_item with HX-Request: true must return the partial (200, no DOCTYPE)."""
        monkeypatch.setattr(app_module, "set_statement_item_completed", lambda *a, **kw: None)
        response = client.post(
            f"/statement/{STATEMENT_ID}",
            data={"action": "complete_item", "statement_item_id": "item-1", "items_view": "incomplete", "show_payments": "true", "page": "1"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        html = response.data.decode()
        assert "<!doctype html>" not in html.lower()
        assert 'id="statement-content"' in html


class TestStatementsListHtmxPartialResponse:
    """When HX-Request header is present on /statements, return partial only."""

    def test_normal_get_returns_full_page(self, client, monkeypatch):
        """A standard GET (no HX-Request) must render the full HTML page with DOCTYPE."""
        monkeypatch.setattr(app_module, "get_incomplete_statements", lambda *a, **kw: [])
        monkeypatch.setattr(app_module, "get_completed_statements", lambda *a, **kw: [])
        response = client.get("/statements")
        assert response.status_code == 200
        html = response.data.decode()
        assert "<!doctype html>" in html.lower() or "<!DOCTYPE html>" in html
        assert 'id="statements-content"' in html

    def test_htmx_get_returns_partial_only(self, client, monkeypatch):
        """A GET with HX-Request: true must render only the partial, without DOCTYPE."""
        monkeypatch.setattr(app_module, "get_incomplete_statements", lambda *a, **kw: [])
        monkeypatch.setattr(app_module, "get_completed_statements", lambda *a, **kw: [])
        response = client.get("/statements", headers={"HX-Request": "true"})
        assert response.status_code == 200
        html = response.data.decode()
        assert "<!doctype html>" not in html.lower()
        assert 'id="statements-content"' in html


class TestDeleteStatementHtmxResponse:
    """When HX-Request header is present on delete, return empty 200 with HX-Trigger."""

    def test_normal_delete_redirects(self, client, monkeypatch):
        """A standard POST delete (no HX-Request) must redirect (302)."""
        monkeypatch.setattr(app_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
        monkeypatch.setattr(app_module, "delete_statement_data", lambda *a, **kw: None)
        response = client.post(f"/statement/{STATEMENT_ID}/delete")
        assert response.status_code == 302

    def test_htmx_delete_returns_empty_200_with_trigger(self, client, monkeypatch):
        """A POST delete with HX-Request: true must return empty 200 with HX-Trigger header."""
        monkeypatch.setattr(app_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
        monkeypatch.setattr(app_module, "delete_statement_data", lambda *a, **kw: None)
        response = client.post(f"/statement/{STATEMENT_ID}/delete", headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "listUpdated"
        assert response.data == b""


class TestStatementsCountEndpoint:
    """The /statements/count endpoint returns the current statement count HTML."""

    def test_returns_count_html(self, client, monkeypatch):
        """The endpoint must return an HTML fragment with the statement count."""
        sample_statements = [{"StatementID": "s1", "ContactName": "A", "Completed": "false"}, {"StatementID": "s2", "ContactName": "B", "Completed": "false"}]
        monkeypatch.setattr(app_module, "get_incomplete_statements", lambda *a, **kw: sample_statements)
        monkeypatch.setattr(app_module, "get_completed_statements", lambda *a, **kw: [])
        response = client.get("/statements/count")
        assert response.status_code == 200
        html = response.data.decode()
        assert "2 statements" in html
