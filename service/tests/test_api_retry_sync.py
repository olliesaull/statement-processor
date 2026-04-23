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
from pathlib import Path

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
        # Invoices failed during the heavy phase; the index never got a chance
        # to run, so its progress map is pending — both must surface as
        # retryable resources.
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 202
        payload = response.get_json()
        assert payload["started"] is True
        assert payload["resources"] == ["invoices", "per_contact_index"]

        # sync_data must receive the retry subset + already_acquired flag.
        assert len(executor_stub.submitted) == 1
        args, kwargs = executor_stub.submitted[0]
        assert args[0] == TENANT_ID
        assert kwargs.get("only_run_resources") == {"invoices", "per_contact_index"}
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

    def test_crashed_mid_fetch_in_progress_is_retryable(self, client, monkeypatch):
        """Crashed worker leaves a resource as in_progress with a partial count — retry must pick it up.

        Case 3 Stage 3 smoke (2026-04-20): payments fetcher crashed at
        34000/36219 records, PaymentsProgress stayed ``in_progress`` because
        sync_data never reached the ``complete`` update. Before the fix,
        ``_RETRYABLE_STATUSES`` only included pending/failed, so retry-sync
        silently dropped payments from ``only_run_resources`` and the tenant
        was stuck in a loop on the downstream index-build failure. Safety
        against racing a live sync comes from ``try_acquire_sync``'s
        stale-heartbeat gate, not from excluding in_progress here.
        """
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        payments_crashed_row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "SYNCING",
            "ContactsProgress": COMPLETE,
            "CreditNotesProgress": COMPLETE,
            "InvoicesProgress": COMPLETE,
            "PaymentsProgress": {"status": "in_progress", "records_fetched": 34000, "record_total": 36219},
            "PerContactIndexProgress": {"status": "pending"},
        }
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: payments_crashed_row))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 202
        payload = response.get_json()
        assert set(payload["resources"]) == {"payments", "per_contact_index"}
        _, kwargs = executor_stub.submitted[0]
        assert kwargs["only_run_resources"] == {"payments", "per_contact_index"}

    def test_mixed_states_includes_all_retry_candidates(self, client, monkeypatch):
        """complete/failed/in_progress/pending/missing combo resolves to the right retry set."""
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        mixed_row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "SYNCING",
            "ContactsProgress": COMPLETE,
            "CreditNotesProgress": FAILED,
            "InvoicesProgress": {"status": "in_progress", "records_fetched": 500, "record_total": 2000},
            "PaymentsProgress": PENDING,
            # PerContactIndexProgress intentionally omitted — missing should count as retryable.
        }
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: mixed_row))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 202
        payload = response.get_json()
        assert set(payload["resources"]) == {"credit_notes", "invoices", "payments", "per_contact_index"}
        _, kwargs = executor_stub.submitted[0]
        assert kwargs["only_run_resources"] == {"credit_notes", "invoices", "payments", "per_contact_index"}

    def test_index_only_failure_can_be_retried(self, client, monkeypatch):
        """A tenant whose 4 fetchers all succeeded but whose index build failed must be retryable.

        Regression for Codex finding: ``_RETRY_RESOURCES`` originally excluded
        ``per_contact_index``, so an index-only failure returned 409 "Nothing
        to retry" and trapped the tenant in LOAD_INCOMPLETE.
        """
        from tenant_data_repository import TenantDataRepository

        c, executor_stub = client
        index_failed_row = {
            "TenantID": TENANT_ID,
            "TenantStatus": "LOAD_INCOMPLETE",
            "ContactsProgress": COMPLETE,
            "CreditNotesProgress": COMPLETE,
            "InvoicesProgress": COMPLETE,
            "PaymentsProgress": COMPLETE,
            "PerContactIndexProgress": {"status": "failed"},
        }
        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: index_failed_row))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 202
        payload = response.get_json()
        assert payload["resources"] == ["per_contact_index"]
        # Background sync must be invoked with per_contact_index in only_run_resources.
        _, kwargs = executor_stub.submitted[0]
        assert kwargs["only_run_resources"] == {"per_contact_index"}

    def test_releases_lock_when_executor_submit_fails(self, client, monkeypatch):
        """A failed executor.submit must roll back the acquired sync lock.

        Regression for Codex finding: without this, the tenant stays
        "SYNCING with fresh heartbeat" until the 5-minute stale window
        elapses, blocking legitimate retries.
        """
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        released: list[tuple[str, object]] = []

        monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tid: _row_with_failed_invoices()))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, tid, target_status, stale_threshold_ms: True))
        monkeypatch.setattr(TenantDataRepository, "release_sync_lock", classmethod(lambda cls, tid, fallback_status: released.append((tid, fallback_status))))

        def _boom(*args, **kwargs):  # noqa: ARG001
            raise RuntimeError("executor down")

        # Drop in a failing executor so submit raises synchronously.
        api_module.executor.submit = _boom

        response = c.post(f"/api/tenants/{TENANT_ID}/retry-sync")

        assert response.status_code == 500
        # The lock must have been rolled back to LOAD_INCOMPLETE.
        assert released == [(TENANT_ID, api_module.TenantStatus.LOAD_INCOMPLETE)]

    def test_main_js_retains_htmx_response_error_toast_handler(self):
        """The 409 fragment body is only surfaced via the client-side htmx:responseError handler.

        HTMX ignores non-2xx bodies by default, so without this listener the
        retry-conflict UX (error toast) silently regresses. Guard the contract
        at the file level — the handler must stay wired up in ``main.js``.
        """
        main_js = Path(__file__).resolve().parents[1] / "static" / "assets" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        assert 'addEventListener("htmx:responseError"' in content, f"htmx:responseError listener missing from {main_js}"


class TestSyncEndpointHtmxResponse:
    """Existing /api/tenants/<id>/sync keeps JSON on plain callers, fragment on HTMX."""

    def test_htmx_success_returns_panel_fragment(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: _row_with_failed_invoices() for tid in ids}))
        # Mock try_acquire_sync: the endpoint now synchronously acquires before submitting.
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: True))
        submitted: list[tuple] = []
        monkeypatch.setattr("routes.api.executor", type("E", (), {"submit": lambda self, *a, **k: submitted.append((a, k))})())

        response = c.post(f"/api/tenants/{TENANT_ID}/sync", headers={"HX-Request": "true"})

        assert response.status_code == 200
        html = response.data.decode()
        assert 'id="sync-progress-panel"' in html
        # Contract: sync_data must be dispatched with already_acquired=True
        # so it doesn't double-claim the lock this endpoint already holds.
        assert len(submitted) == 1
        _, kwargs = submitted[0]
        assert kwargs.get("already_acquired") is True

    def test_htmx_conflict_returns_409(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "get_many", classmethod(lambda cls, ids: {tid: _row_with_failed_invoices() for tid in ids}))
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: False))

        response = c.post(f"/api/tenants/{TENANT_ID}/sync", headers={"HX-Request": "true"})

        assert response.status_code == 409
        # 409 still returns the fragment body so HTMX can swap something
        # useful rather than erroring out.
        assert 'id="sync-progress-panel"' in response.data.decode()

    def test_non_htmx_sync_keeps_json_202(self, client, monkeypatch):
        from tenant_data_repository import TenantDataRepository

        c, _ = client
        monkeypatch.setattr(TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: True))
        monkeypatch.setattr("routes.api.executor", type("E", (), {"submit": lambda self, *a, **k: None})())

        response = c.post(f"/api/tenants/{TENANT_ID}/sync")
        assert response.status_code == 202
        assert response.get_json()["started"] is True
