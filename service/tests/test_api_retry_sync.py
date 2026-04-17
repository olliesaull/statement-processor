"""Route tests for /api/tenants/<id>/retry-sync and HTMX behaviour on /sync.

Covers:
- 202 JSON on happy path (non-HTMX caller).
- 403 when the tenant isn't in the session.
- 409 when try_acquire_sync rejects.
- 409 when there are no pending/failed resources to retry.
- HTMX responses return the rendered sync_progress_panel.html fragment.
- /api/tenants/<id>/sync endpoint returns the same fragment on HTMX.
"""

from __future__ import annotations

import tempfile

import pytest
from cachelib import FileSystemCache
from flask_session import Session

import app as app_module
import routes.api as api_module
import utils.auth

TENANT_ID = "tenant-retry-api-test"
OTHER_TENANT_ID = "other-tenant"

COMPLETE = {"status": "complete", "records_fetched": 10, "record_total": 10}
FAILED = {"status": "failed", "records_fetched": 5, "record_total": 10}
PENDING = {"status": "pending"}


def _row_with_failed_invoices() -> dict:
    return {"TenantID": TENANT_ID, "TenantStatus": "LOAD_INCOMPLETE", "ContactsProgress": COMPLETE, "CreditNotesProgress": COMPLETE, "InvoicesProgress": FAILED, "PaymentsProgress": COMPLETE}


def _fully_complete_row() -> dict:
    return {
        "TenantID": TENANT_ID,
        "TenantStatus": "FREE",
        "ReconcileReadyAt": 1_700_000_000_000,
        "ContactsProgress": COMPLETE,
        "CreditNotesProgress": COMPLETE,
        "InvoicesProgress": COMPLETE,
        "PaymentsProgress": COMPLETE,
        "PerContactIndexProgress": {"status": "complete"},
    }


@pytest.fixture(scope="module")
def _app():
    tmpdir = tempfile.mkdtemp(prefix="flask_test_sessions_retry_")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SESSION_TYPE="cachelib", SESSION_CACHELIB=FileSystemCache(tmpdir), SECRET_KEY="test-secret-retry")
    Session(app_module.app)
    return app_module.app


@pytest.fixture()
def client(_app, monkeypatch):
    """Authenticated client with an in-memory executor (runs submit inline)."""
    from tenant_data_repository import TenantStatus

    monkeypatch.setattr(utils.auth, "has_cookie_consent", lambda: True)
    monkeypatch.setattr(utils.auth, "get_xero_oauth2_token", lambda: {"access_token": "abc", "expires_at": 9_999_999_999.0})
    monkeypatch.setattr(utils.auth, "set_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "clear_session_is_set_cookie", lambda r: r)
    monkeypatch.setattr(utils.auth, "get_tenant_status", lambda tenant_id: TenantStatus.FREE)

    # Replace the background executor with an inline dummy so submit() doesn't
    # actually fire sync_data against DynamoDB/Xero in the test.
    class _InlineExecutor:
        def __init__(self):
            self.submitted: list[tuple[tuple, dict]] = []

        def submit(self, fn, *args, **kwargs):  # noqa: ARG002
            self.submitted.append((args, kwargs))
            return None

    fake_executor = _InlineExecutor()
    monkeypatch.setattr(api_module, "executor", fake_executor)

    with _app.test_client() as c:
        with c.session_transaction() as sess:
            sess["xero_tenant_id"] = TENANT_ID
            sess["xero_oauth2_token"] = {"access_token": "abc", "expires_at": 9_999_999_999.0}
            sess["xero_tenants"] = [{"tenantId": TENANT_ID, "tenantName": "Acme Ltd"}]
        yield c, fake_executor


class TestRetrySyncHappyPath:
    """Successful retry returns 202 and fires sync_data with the failed subset."""

    def test_returns_202_with_json_on_success(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 202
        payload = response.get_json()
        assert payload["started"] is True
        assert payload["resources"] == ["invoices"]

        # sync_data must receive the retry subset + already_acquired flag.
        assert len(executor_stub.submitted) == 1
        args, kwargs = executor_stub.submitted[0]
        assert args[0] == TENANT_ID
        assert kwargs.get("only_run_resources") == {"invoices"}
        assert kwargs.get("already_acquired") is True


class TestRetrySyncAuthorization:
    """Retry-sync enforces tenant ownership via session."""

    def test_returns_403_when_tenant_not_in_session(self, client):
        c, _ = client
        response = c.post(f"/api/tenants/{OTHER_TENANT_ID}/retry-sync")
        assert response.status_code == 403
        assert response.get_json()["error"] == "Tenant not authorized"


class TestRetrySyncConflict:
    """Retry-sync translates try_acquire_sync False into 409."""

    def test_returns_409_when_try_acquire_rejects(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: False))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 409
        assert response.get_json()["error"] == "Sync already in flight"
        # Must not submit anything.
        assert executor_stub.submitted == []

    def test_returns_409_when_no_resources_to_retry(self, client, monkeypatch):
        """Fully complete tenant has no pending/failed resources -> 409."""
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _fully_complete_row()))
        # try_acquire_sync must not even be reached.
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: pytest.fail("try_acquire_sync should not be called")))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 409
        assert response.get_json()["error"] == "Nothing to retry"
        assert executor_stub.submitted == []


class TestRetrySyncHtmxResponse:
    """HTMX callers get the sync-progress panel fragment instead of JSON."""

    def test_htmx_success_returns_panel_fragment(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: _row_with_failed_invoices() for tid in ids}))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync", headers={"HX-Request": "true"})

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        assert response.content_type.startswith("text/html")

    def test_htmx_conflict_still_returns_fragment_with_409(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: False))
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: _row_with_failed_invoices() for tid in ids}))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync", headers={"HX-Request": "true"})

        assert response.status_code == 409
        assert 'id="sync-progress-panel"' in response.data.decode()


class TestSyncEndpointHtmxResponse:
    """Existing /api/tenants/<id>/sync keeps JSON on plain callers, fragment on HTMX."""

    def test_htmx_success_returns_panel_fragment(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: _row_with_failed_invoices() for tid in ids}))

        response = c.post(f"/api/tenants/{TENANT_ID}/sync", headers={"HX-Request": "true"})

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html

    def test_non_htmx_sync_keeps_json_202(self, client):
        c, _ = client
        response = c.post(f"/api/tenants/{TENANT_ID}/sync")
        assert response.status_code == 202
        assert response.get_json()["started"] is True
