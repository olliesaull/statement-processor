"""Route tests for the sync-progress HTMX endpoints.

Covers:
- GET /tenants/sync-progress returning the multi-tenant fragment and
  stopping polling once every session tenant is reconcile-ready + complete.
- GET /statement/<id>/wait returning the single-tenant wait fragment while
  data is syncing, and emitting HX-Redirect once ReconcileReadyAt is set.
- 404 ownership guard on the wait endpoint so one tenant can't poll another
  tenant's statement id.
"""

from __future__ import annotations

import tempfile

import pytest
from cachelib import FileSystemCache
from flask_session import Session

import app as app_module
import routes.statements as statements_module
import utils.auth

TENANT_ID = "tenant-sync-progress-test"
OTHER_TENANT_ID = "tenant-other"
STATEMENT_ID = "stmt-sync-001"
SAMPLE_RECORD = {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID, "ContactName": "Test", "Completed": "false"}

COMPLETE_PROGRESS = {"status": "complete", "records_fetched": 10, "record_total": 10, "updated_at": 1}

READY_ROW = {
    "TenantID": TENANT_ID,
    "TenantStatus": "FREE",
    "ReconcileReadyAt": 1_700_000_000_000,
    "ContactsProgress": COMPLETE_PROGRESS,
    "InvoicesProgress": COMPLETE_PROGRESS,
    "CreditNotesProgress": COMPLETE_PROGRESS,
    "PaymentsProgress": COMPLETE_PROGRESS,
    "PerContactIndexProgress": {"status": "complete", "updated_at": 1},
}

IN_FLIGHT_ROW = {
    "TenantID": TENANT_ID,
    "TenantStatus": "SYNCING",
    "ContactsProgress": COMPLETE_PROGRESS,
    "InvoicesProgress": {"status": "in_progress", "records_fetched": 250, "record_total": 1000},
    "CreditNotesProgress": {"status": "pending"},
    "PaymentsProgress": {"status": "pending"},
    "PerContactIndexProgress": {"status": "pending"},
}


@pytest.fixture(scope="module")
def _app():
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_sync_progress_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-key-sync-progress")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Minimal auth bypass — individual tests stub TenantDataRepository.get_item/get_many."""
    from tenant_data_repository import TenantStatus

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "get_tenant_status", lambda tenant_id: TenantStatus.FREE)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = TENANT_ID
            sess["xero_tenant_name"] = "Acme Ltd"
            sess["xero_tenants"] = [{"tenantId": TENANT_ID, "tenantName": "Acme Ltd"}]
            # Needed by the sync-trigger endpoint; harmless to other tests.
            sess["xero_oauth2_token"] = {"access_token": "fake", "expires_at": 9_999_999_999.0}
        yield c


class TestTenantsSyncProgressEndpoint:
    """/tenants/sync-progress returns the progress panel fragment for session tenants."""

    def test_renders_fragment_for_session_tenants(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: IN_FLIGHT_ROW for tid in ids}))

        response = client.get("/tenants/sync-progress")

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        assert "Acme Ltd" in html
        # In-flight: poll must remain active.
        assert "hx-trigger" in html
        # In-flight invoices: visible percentage count.
        assert "250" in html and "1000" in html

    def test_stops_polling_when_all_tenants_ready(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenants/sync-progress")

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        # All-ready: hx-trigger must be dropped so HTMX stops polling.
        assert "hx-trigger" not in html

    def test_requires_authentication(self, _app, monkeypatch):
        """Without a session, xero_token_required must reject the request."""
        monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
        monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: None)

        with _app.test_client() as c:
            response = c.get("/tenants/sync-progress")
            # UI route without auth -> redirect to login.
            assert response.status_code in (302, 401)


class TestStatementWaitEndpoint:
    """/statement/<id>/wait polls on not-ready and emits HX-Redirect on ready."""

    def test_returns_fragment_when_not_ready(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(statements_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: IN_FLIGHT_ROW))

        response = client.get(f"/statement/{STATEMENT_ID}/wait")

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="statement-reconcile-not-ready"' in html
        assert f"/statement/{STATEMENT_ID}/wait" in html
        # No HX-Redirect header on the not-ready path.
        assert "HX-Redirect" not in response.headers

    def test_emits_hx_redirect_on_ready(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(statements_module, "get_statement_record", lambda *a, **kw: SAMPLE_RECORD)
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: READY_ROW))

        response = client.get(f"/statement/{STATEMENT_ID}/wait")

        assert response.status_code == 200
        assert response.headers.get("HX-Redirect") == f"/statement/{STATEMENT_ID}"
        # Body must be empty so HTMX navigates without trying to swap content.
        assert response.data == b""

    def test_returns_404_when_statement_not_owned_by_tenant(self, client, monkeypatch):
        """Ownership guard: unauthorised statement_ids must not be poll-able."""
        from tenant_data_repository import TenantDataRepository

        # get_statement_record returns None when the statement_id doesn't belong to tenant.
        monkeypatch.setattr(statements_module, "get_statement_record", lambda *a, **kw: None)
        # ReconcileReadyAt is irrelevant to the 404 — it must fire before that check.
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: READY_ROW))

        response = client.get(f"/statement/{STATEMENT_ID}/wait")

        assert response.status_code == 404


class TestTriggerTenantSyncHtmx:
    """HTMX-flavoured trigger_tenant_sync returns a syncing fragment."""

    def test_htmx_request_acquires_lock_and_submits_already_acquired(self, client, monkeypatch):
        """Contract: POST acquires the sync lock synchronously, then submits
        sync_data with already_acquired=True so it doesn't double-claim.

        Fragment rendering (pill copy, disabled button) is covered separately
        in TestIncrementalSyncingRender — here we just verify the plumbing.
        """
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: IN_FLIGHT_ROW for tid in ids}))
        acquire_calls: list = []
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: (acquire_calls.append((a, k)) or True)))
        submitted: list[tuple] = []
        monkeypatch.setattr("routes.api.executor", type("E", (), {"submit": lambda self, *a, **k: submitted.append((a, k))})())

        response = client.post(f"/api/tenants/{TENANT_ID}/sync", headers={"HX-Request": "true"})

        assert response.status_code == 200
        assert 'id="sync-progress-panel"' in response.data.decode()
        assert len(acquire_calls) == 1
        assert len(submitted) == 1
        _, kwargs = submitted[0]
        assert kwargs.get("already_acquired") is True

    def test_htmx_request_returns_409_on_conflict(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: IN_FLIGHT_ROW for tid in ids}))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: False))

        response = client.post(f"/api/tenants/{TENANT_ID}/sync", headers={"HX-Request": "true"})
        assert response.status_code == 409

    def test_non_htmx_request_returns_202_json(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: True))
        monkeypatch.setattr("routes.api.executor", type("E", (), {"submit": lambda self, *a, **k: None})())

        response = client.post(f"/api/tenants/{TENANT_ID}/sync")
        assert response.status_code == 202
        assert response.json == {"started": True}


class TestIncrementalSyncingRender:
    """Reconcile-ready tenant mid-incremental-sync shows syncing pill + disabled Sync button."""

    def test_incremental_syncing_shows_syncing_pill_and_disabled_button(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        import time
        now = int(time.time() * 1000)
        incremental_row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "SYNCING",
            "ReconcileReadyAt": 1_000_000,
            "LastHeartbeatAt": now - 500,
            "LastSyncTime": 2_000_000,
            "ContactsProgress": COMPLETE_PROGRESS,
            "InvoicesProgress": COMPLETE_PROGRESS,
            "CreditNotesProgress": COMPLETE_PROGRESS,
            "PaymentsProgress": COMPLETE_PROGRESS,
            "PerContactIndexProgress": {"status": "complete", "updated_at": 1},
        }
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: incremental_row for tid in ids}))

        response = client.get("/tenants/sync-progress", headers={"HX-Request": "true"})
        assert response.status_code == 200
        body = response.data.decode()

        # Pill: "Syncing..." present; Ready not present.
        assert "Syncing" in body
        assert ">Ready<" not in body
        # Action button: disabled with Syncing label.
        assert "disabled" in body
        # No first-sync progress bar.
        assert 'role="progressbar"' not in body
        # aria-label reflects the visible syncing state, not the CSS class.
        assert "state syncing-incremental" in body
        assert "state complete" not in body


class TestPanelPollingWiring:
    """When no tenant requires polling, the panel must carry no HTMX wiring."""

    def test_fully_complete_tenant_renders_no_hx_attributes(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenants/sync-progress", headers={"HX-Request": "true"})
        body = response.data.decode()

        # Isolate the <ul id="sync-progress-panel"> opening tag.
        import re
        match = re.search(r'<ul[^>]*id="sync-progress-panel"[^>]*>', body)
        assert match, "panel element missing"
        opening = match.group(0)
        assert "hx-get" not in opening
        assert "hx-trigger" not in opening
        assert "hx-swap" not in opening
