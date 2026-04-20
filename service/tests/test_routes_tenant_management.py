"""Tests for the /tenant_management page render.

Covers the Sync / Retry-sync button conditional and the inclusion of the
sync-progress panel on initial load.
"""

from __future__ import annotations

import tempfile
import time

import pytest
from cachelib import FileSystemCache
from flask_session import Session

import app as app_module
import utils.auth

TENANT_ID = "tenant-tm-test"

COMPLETE_PROGRESS = {"status": "complete", "records_fetched": 10, "record_total": 10, "updated_at": 1}
FAILED_INVOICES = {"status": "failed", "records_fetched": 5, "record_total": 10}

READY_ROW = {
    "TenantID": TENANT_ID,
    "TenantStatus": "FREE",
    "ReconcileReadyAt": 1_700_000_000_000,
    "ContactsProgress": COMPLETE_PROGRESS,
    "InvoicesProgress": COMPLETE_PROGRESS,
    "CreditNotesProgress": COMPLETE_PROGRESS,
    "PaymentsProgress": COMPLETE_PROGRESS,
    "PerContactIndexProgress": {"status": "complete"},
}

LOAD_INCOMPLETE_ROW = {
    "TenantID": TENANT_ID,
    "TenantStatus": "LOAD_INCOMPLETE",
    "ContactsProgress": COMPLETE_PROGRESS,
    "InvoicesProgress": FAILED_INVOICES,
    "CreditNotesProgress": COMPLETE_PROGRESS,
    "PaymentsProgress": COMPLETE_PROGRESS,
}


@pytest.fixture(scope="module")
def _app():
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_tm_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-tm")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    from tenant_data_repository import TenantStatus

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "get_tenant_status", lambda tenant_id: TenantStatus.FREE)

    # Stub billing lookups used by the route (they hit DynamoDB otherwise).
    from tenant_billing_repository import TenantBillingRepository

    monkeypatch.setattr(TenantBillingRepository, "get_tenant_token_balances", classmethod(lambda cls, ids: {tid: 100 for tid in ids}))
    monkeypatch.setattr(TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: None))

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = TENANT_ID
            sess["xero_oauth2_token"] = {"expires_at": 9_999_999_999.0}
            sess["xero_tenants"] = [{"tenantId": TENANT_ID, "tenantName": "Acme Ltd"}]
        yield c


class TestTenantManagementSyncButton:
    """Sync button renders as Retry sync on LOAD_INCOMPLETE / failure state."""

    def test_renders_sync_button_when_fully_ready(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        # Sync button targets the plain sync endpoint.
        assert f'hx-post="/api/tenants/{TENANT_ID}/sync"' in html
        assert ">Sync<" in html
        # Retry button must not be rendered.
        assert f"/api/tenants/{TENANT_ID}/retry-sync" not in html

    def test_renders_retry_button_when_load_incomplete(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: LOAD_INCOMPLETE_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert f'hx-post="/api/tenants/{TENANT_ID}/retry-sync"' in html
        assert ">Retry sync<" in html
        # Plain sync button must not be rendered simultaneously.
        assert f'hx-post="/api/tenants/{TENANT_ID}/sync"' not in html

    def test_renders_retry_button_for_stuck_syncing_with_stale_heartbeat(self, client, monkeypatch):
        """Stuck-SYNCING (crashed worker, stale heartbeat) must surface Retry sync.

        Regression for Case 3 Stage 3 smoke — without this, operators saw the
        plain Sync button against a worker whose heartbeat was already past
        the stale threshold; clicking it silently no-ops because sync_data
        bails before touching any resources.
        """
        from tenant_data_repository import SYNC_STALE_THRESHOLD_MS, TenantDataRepository

        now_ms = int(time.time() * 1000)
        stuck_syncing_row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "SYNCING",
            "LastHeartbeatAt": now_ms - (SYNC_STALE_THRESHOLD_MS + 60_000),  # 1 min past stale
            "ContactsProgress": COMPLETE_PROGRESS,
            "InvoicesProgress": COMPLETE_PROGRESS,
            "CreditNotesProgress": COMPLETE_PROGRESS,
            "PaymentsProgress": {"status": "in_progress", "records_fetched": 34000, "record_total": 36219},
            "PerContactIndexProgress": {"status": "pending"},
        }
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: stuck_syncing_row for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert f'hx-post="/api/tenants/{TENANT_ID}/retry-sync"' in html
        assert ">Retry sync<" in html


class TestTenantManagementProgressPanel:
    """Initial render embeds the sync-progress panel so polling starts immediately."""

    def test_includes_sync_progress_panel_on_load(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        # Ready state: polling stopped -> no hx-trigger attribute on the panel.
        assert "hx-trigger" not in html

    def test_panel_keeps_hx_trigger_when_any_tenant_incomplete(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: LOAD_INCOMPLETE_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        assert "hx-trigger" in html
