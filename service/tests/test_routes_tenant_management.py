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

        row = {**READY_ROW, "LastSyncTime": 1_712_000_000_000}
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: row for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert f'id="card-{TENANT_ID}"' in html
        assert "tenant-card is-complete is-current" in html
        assert ">Ready<" in html
        assert f'hx-post="/api/tenants/{TENANT_ID}/sync"' in html
        assert ">Sync<" in html
        assert f"/api/tenants/{TENANT_ID}/retry-sync" not in html

    def test_renders_retry_button_when_load_incomplete(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: LOAD_INCOMPLETE_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert response.status_code == 200
        html = response.data.decode()
        assert "tenant-card is-failed" in html
        assert ">Retry sync<" in html
        assert "Sync failed" in html
        assert f"/api/tenants/{TENANT_ID}/retry-sync" in html

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


class TestTenantManagementFinalisingPill:
    """Card pill flips to 'Finalising...' when all four fetchers complete but per_contact_index isn't."""

    def test_finalising_pill_when_index_still_building(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        done = {"status": "complete", "records_fetched": 10, "record_total": 10, "updated_at": 1}
        row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "SYNCING",
            "ContactsProgress": done,
            "CreditNotesProgress": done,
            "InvoicesProgress": done,
            "PaymentsProgress": done,
            "PerContactIndexProgress": {"status": "in_progress"},
        }
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: row for tid in ids}))

        response = client.get("/tenant_management")

        html = response.data.decode()
        assert "Finalising" in html
        assert "is-finalising" in html
        # Ready pill must not appear for this single-tenant fixture; the
        # simple page-wide assertion is safe because only one card is rendered.
        assert "Ready" not in html


class TestTenantManagementLastSyncMetric:
    """Last sync metric renders formatted timestamp, or 'First sync...' when null."""

    def test_first_sync_shown_when_no_last_sync_time(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        row = {**READY_ROW}
        row.pop("LastSyncTime", None)
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: row for tid in ids}))

        response = client.get("/tenant_management")

        html = response.data.decode()
        assert "First sync" in html

    def test_formatted_timestamp_shown_when_present(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        # 2024-01-15 14:30:00 UTC
        row = {**READY_ROW, "LastSyncTime": 1_705_329_000_000}
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: row for tid in ids}))

        response = client.get("/tenant_management")

        html = response.data.decode()
        assert "Jan 15, 14:30" in html


class TestTenantManagementEmptyState:
    """Empty-state card renders when the partial is asked with no tenants.

    Tested via the fragment renderer directly; the /tenant_management route
    is gated by ``@xero_token_required`` which assumes an active tenant, so
    exercising the empty branch through the route would only prove the
    auth redirect works.
    """

    def test_empty_state_in_fragment(self, _app):
        from utils.sync_progress import render_sync_progress_fragment

        with _app.test_request_context("/"):
            html = render_sync_progress_fragment([], tenant_rows={}, current_tenant_id=None, tenant_token_balances={}, is_active_subscription=False, needs_retry_by_id={})

        assert "tenant-card-empty" in html
        assert "No tenants connected yet" in html


class TestTenantManagementSubscriptionWarning:
    """`data-has-subscription` flips only when the current tenant's subscription is active.

    Regression guard: before this refactor the flag required
    ``subscription_state.status == 'active'``. An intermediate version flagged
    any tenant with a tier_id (including cancelled / past_due / incomplete),
    which would surface the "active subscription" warning on the disconnect
    modal for users without one.
    """

    def _set_active_subscription(self, monkeypatch, status: str) -> None:
        from tenant_billing_repository import SubscriptionState, TenantBillingRepository

        state = SubscriptionState(tier_id="tier_200", status=status, stripe_subscription_id="sub_x", current_period_end="2026-05-01", tokens_credited_this_period=0, cancel_at="")
        monkeypatch.setattr(TenantBillingRepository, "get_subscription_state", classmethod(lambda cls, tid: state))

    def test_has_subscription_true_when_status_active(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        self._set_active_subscription(monkeypatch, "active")
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert 'data-has-subscription="true"' in response.data.decode()

    def test_has_subscription_false_when_status_canceled(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        self._set_active_subscription(monkeypatch, "canceled")
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenant_management")

        html = response.data.decode()
        assert 'data-has-subscription="true"' not in html
        assert 'data-has-subscription="false"' in html

    def test_has_subscription_false_when_no_subscription(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: READY_ROW for tid in ids}))

        response = client.get("/tenant_management")

        assert 'data-has-subscription="false"' in response.data.decode()
