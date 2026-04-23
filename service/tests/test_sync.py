"""Unit tests for sync helpers."""

from unittest.mock import MagicMock

import pytest

import sync
from tenant_data_repository import TenantStatus


def test_check_load_required_grants_welcome_tokens_for_new_tenant(monkeypatch) -> None:
    """New tenants should receive WELCOME_GRANT_TOKENS on first seed."""
    fake_table = MagicMock()
    # Simulate no existing row — get_item returns no Item key.
    fake_table.get_item.return_value = {}
    # put_item succeeds (no ConditionalCheckFailed).
    fake_table.put_item.return_value = {}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    result = sync.check_load_required("new-tenant")

    assert result is True
    mock_billing.adjust_token_balance.assert_called_once_with("new-tenant", 5, source="welcome-grant", price_per_token_pence=0)


def test_check_load_required_does_not_grant_for_existing_tenant(monkeypatch) -> None:
    """Existing tenants should not receive any token grant."""
    fake_table = MagicMock()
    # Simulate existing row.
    fake_table.get_item.return_value = {"Item": {"TenantID": "existing-tenant", "TenantStatus": "FREE"}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)
    monkeypatch.setattr(sync, "_s3_data_exists", lambda _tid: True)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    result = sync.check_load_required("existing-tenant")

    assert result is False
    mock_billing.adjust_token_balance.assert_not_called()


def test_check_load_required_continues_if_grant_fails(monkeypatch) -> None:
    """Welcome grant failure should not block the login flow."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {}
    fake_table.put_item.return_value = {}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)

    mock_billing = MagicMock()
    mock_billing.adjust_token_balance.side_effect = RuntimeError("DDB down")
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    # Should not raise — grant failure is non-fatal.
    result = sync.check_load_required("new-tenant")

    assert result is True


def test_check_load_required_returns_true_for_erased_tenant(monkeypatch) -> None:
    """ERASED tenant should trigger a fresh load and cancel pending erasure."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"TenantID": "erased-tenant", "TenantStatus": "ERASED", "EraseTenantDataTime": 1700000000000}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    mock_repo = MagicMock()
    monkeypatch.setattr(sync, "TenantDataRepository", mock_repo)

    result = sync.check_load_required("erased-tenant")

    assert result is True
    mock_billing.adjust_token_balance.assert_not_called()
    # Erasure cancellation and status reset combined in a single atomic update_item call.
    fake_table.update_item.assert_called_once()
    call_kwargs = fake_table.update_item.call_args
    assert "REMOVE EraseTenantDataTime" in call_kwargs.kwargs.get("UpdateExpression", "")


def test_check_load_required_returns_true_for_load_incomplete_tenant(monkeypatch) -> None:
    """LOAD_INCOMPLETE tenant should trigger a fresh load."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"TenantID": "incomplete-tenant", "TenantStatus": "LOAD_INCOMPLETE"}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    mock_repo = MagicMock()
    monkeypatch.setattr(sync, "TenantDataRepository", mock_repo)

    result = sync.check_load_required("incomplete-tenant")

    assert result is True
    mock_billing.adjust_token_balance.assert_not_called()


def test_check_load_required_returns_false_for_free_with_erasure_pending(monkeypatch) -> None:
    """FREE tenant with pending erasure should cancel erasure but NOT reload."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"TenantID": "free-tenant", "TenantStatus": "FREE", "EraseTenantDataTime": 1700000000000}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)
    monkeypatch.setattr(sync, "_s3_data_exists", lambda _tid: True)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    mock_repo = MagicMock()
    monkeypatch.setattr(sync, "TenantDataRepository", mock_repo)

    result = sync.check_load_required("free-tenant")

    assert result is False
    mock_repo.cancel_erasure.assert_called_once_with("free-tenant")


def test_check_load_required_triggers_reload_when_s3_data_missing(monkeypatch) -> None:
    """FREE tenant with missing S3 data should trigger a fresh LOADING sync."""
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {"TenantID": "orphan-tenant", "TenantStatus": "FREE"}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)
    monkeypatch.setattr(sync, "_s3_data_exists", lambda _tid: False)

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    result = sync.check_load_required("orphan-tenant")

    assert result is True
    # Should set status to LOADING.
    fake_table.update_item.assert_called_once()
    call_kwargs = fake_table.update_item.call_args.kwargs
    assert call_kwargs["ExpressionAttributeValues"][":loading"] == TenantStatus.LOADING


def test_s3_data_exists_returns_true_when_canary_present(monkeypatch) -> None:
    """Should return True when contacts.json exists in S3."""
    fake_s3 = MagicMock()
    fake_s3.head_object.return_value = {}
    monkeypatch.setattr(sync, "s3_client", fake_s3)
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    assert sync._s3_data_exists("t1") is True
    fake_s3.head_object.assert_called_once_with(Bucket="test-bucket", Key="t1/data/contacts.json")


def test_s3_data_exists_returns_false_when_canary_missing(monkeypatch) -> None:
    """Should return False when head_object returns 404 (object missing)."""
    from botocore.exceptions import ClientError

    fake_s3 = MagicMock()
    fake_s3.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
    monkeypatch.setattr(sync, "s3_client", fake_s3)
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    assert sync._s3_data_exists("t1") is False


def test_s3_data_exists_returns_false_for_no_such_key_code(monkeypatch) -> None:
    """Should also return False when error code is NoSuchKey (some S3 implementations)."""
    from botocore.exceptions import ClientError

    fake_s3 = MagicMock()
    fake_s3.head_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "HeadObject")
    monkeypatch.setattr(sync, "s3_client", fake_s3)
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    assert sync._s3_data_exists("t1") is False


def test_s3_data_exists_returns_true_on_other_client_error(monkeypatch) -> None:
    """Non-404 ClientErrors should assume data exists to avoid unnecessary reloads."""
    from botocore.exceptions import ClientError

    fake_s3 = MagicMock()
    fake_s3.head_object.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "Forbidden"}}, "HeadObject")
    monkeypatch.setattr(sync, "s3_client", fake_s3)
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    assert sync._s3_data_exists("t1") is True


def test_s3_data_exists_returns_true_on_non_aws_error(monkeypatch) -> None:
    """Non-AWS errors (network, etc.) should assume data exists to be safe."""
    fake_s3 = MagicMock()
    fake_s3.head_object.side_effect = RuntimeError("S3 timeout")
    monkeypatch.setattr(sync, "s3_client", fake_s3)
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    assert sync._s3_data_exists("t1") is True


@pytest.fixture()
def _sync_scaffold(monkeypatch):
    """Install deterministic stand-ins for every external collaborator used by sync_data.

    Returns a dict the test can reach into to configure stubs and inspect calls.
    """
    scaffold: dict = {
        "try_acquire_results": [True],  # consumed per call
        "contacts_ok": True,
        "credit_notes_ok": True,
        "invoices_ok": True,
        "payments_ok": True,
        "index_calls": [],
        "status_calls": [],
        "mark_reconcile_calls": [],
        "existing_record": None,  # item returned by get_item
    }

    # get_xero_api_client — nothing needs the real client.
    monkeypatch.setattr(sync, "get_xero_api_client", lambda *a, **kw: MagicMock())
    # Cache bump is fire-and-forget.
    monkeypatch.setattr(sync, "bump_tenant_generation", lambda *a, **kw: None)
    # build_per_contact_index — record that it was called.
    monkeypatch.setattr(sync, "build_per_contact_index", lambda tid: scaffold["index_calls"].append(tid))
    # _resolve_modified_since — don't pull from a real record.
    monkeypatch.setattr(sync, "_resolve_modified_since", lambda rec: None)

    # TenantDataRepository
    fake_repo = MagicMock()
    fake_repo.get_item.side_effect = lambda tid: scaffold["existing_record"]

    def _try_acquire(tenant_id, target_status, stale_threshold_ms):  # noqa: ARG001
        if scaffold["try_acquire_results"]:
            return scaffold["try_acquire_results"].pop(0)
        return True

    fake_repo.try_acquire_sync.side_effect = _try_acquire
    fake_repo.mark_reconcile_ready.side_effect = lambda tid: scaffold["mark_reconcile_calls"].append(tid)
    monkeypatch.setattr(sync, "TenantDataRepository", fake_repo)
    scaffold["repo"] = fake_repo

    # update_tenant_status — record calls.
    def _record_status(tenant_id, tenant_status=TenantStatus.FREE, last_sync_time=None):
        scaffold["status_calls"].append({"tenant_id": tenant_id, "status": tenant_status, "last_sync_time": last_sync_time})
        return True

    monkeypatch.setattr(sync, "update_tenant_status", _record_status)

    # Fetcher stubs keyed by function name.
    def _contacts(api, tenant_id, modified_since=None):  # noqa: ARG001
        return scaffold["contacts_ok"]

    def _credit_notes(api, tenant_id, modified_since=None):  # noqa: ARG001
        return scaffold["credit_notes_ok"]

    def _invoices(api, tenant_id, modified_since=None):  # noqa: ARG001
        return scaffold["invoices_ok"]

    def _payments(api, tenant_id, modified_since=None):  # noqa: ARG001
        return scaffold["payments_ok"]

    monkeypatch.setattr(sync, "sync_contacts", _contacts)
    monkeypatch.setattr(sync, "sync_credit_notes", _credit_notes)
    monkeypatch.setattr(sync, "sync_invoices", _invoices)
    monkeypatch.setattr(sync, "sync_payments", _payments)

    return scaffold


class TestSyncDataOrchestration:
    """sync_data follows the contacts-first + heavy-phase choreography."""

    def test_loading_flow_ends_at_free_and_marks_reconcile_ready(self, _sync_scaffold):
        sync.sync_data("tenant-ok", TenantStatus.LOADING)

        # Status transitions: LOADING -> SYNCING (post-contacts) -> FREE.
        statuses = [c["status"] for c in _sync_scaffold["status_calls"]]
        assert statuses == [TenantStatus.SYNCING, TenantStatus.FREE]

        # Index build only runs after all heavy-phase resources succeeded.
        assert _sync_scaffold["index_calls"] == ["tenant-ok"]
        # Reconcile gate flipped.
        assert _sync_scaffold["mark_reconcile_calls"] == ["tenant-ok"]
        # Final FREE carries start_time (not None).
        final_call = _sync_scaffold["status_calls"][-1]
        assert final_call["last_sync_time"] is not None

    def test_heavy_phase_failure_sets_load_incomplete_and_skips_index(self, _sync_scaffold):
        _sync_scaffold["invoices_ok"] = False

        sync.sync_data("tenant-broken", TenantStatus.LOADING)

        # First transition is LOADING -> SYNCING once contacts are done.
        statuses = [c["status"] for c in _sync_scaffold["status_calls"]]
        assert TenantStatus.LOAD_INCOMPLETE in statuses
        # Index build MUST be skipped when heavy phase failed.
        assert _sync_scaffold["index_calls"] == []
        # Reconcile gate must NOT be flipped.
        assert _sync_scaffold["mark_reconcile_calls"] == []

    def test_contacts_failure_sets_load_incomplete_and_skips_heavy_phase(self, _sync_scaffold):
        _sync_scaffold["contacts_ok"] = False
        # The heavy-phase fetchers should never be called — track that.
        heavy_calls: list[str] = []
        monkey = lambda resource: lambda api, tenant_id, modified_since=None: heavy_calls.append(resource) or True  # noqa: E731, ARG005
        # Rebind in scaffold to detect invocation.
        import sync as sync_mod  # local alias for monkey.

        sync_mod.sync_credit_notes = monkey("credit_notes")
        sync_mod.sync_invoices = monkey("invoices")
        sync_mod.sync_payments = monkey("payments")

        sync.sync_data("tenant-no-contacts", TenantStatus.LOADING)

        assert heavy_calls == []
        statuses = [c["status"] for c in _sync_scaffold["status_calls"]]
        assert TenantStatus.LOAD_INCOMPLETE in statuses
        assert _sync_scaffold["mark_reconcile_calls"] == []

    def test_manual_syncing_with_all_success_preserves_reconcile_ready(self, _sync_scaffold):
        """Manual SYNCING flow must NOT re-mark reconcile-ready (already set)."""
        _sync_scaffold["existing_record"] = {"ReconcileReadyAt": 1700000000000, "LastSyncTime": 1700000000000}

        sync.sync_data("tenant-manual-ok", TenantStatus.SYNCING)

        # Reconcile already ready — don't touch it.
        assert _sync_scaffold["mark_reconcile_calls"] == []
        # Still ends at FREE with start_time.
        final_call = _sync_scaffold["status_calls"][-1]
        assert final_call["status"] == TenantStatus.FREE
        assert final_call["last_sync_time"] is not None

    def test_manual_syncing_partial_failure_keeps_free_with_null_sync_time(self, _sync_scaffold):
        """Existing manual-sync-partial-failure behavior preserved for reconcile-ready tenants."""
        _sync_scaffold["existing_record"] = {"ReconcileReadyAt": 1700000000000}
        _sync_scaffold["invoices_ok"] = False

        sync.sync_data("tenant-manual-partial", TenantStatus.SYNCING)

        final_call = _sync_scaffold["status_calls"][-1]
        # Reconcile-ready tenant with partial failure returns to FREE, not LOAD_INCOMPLETE.
        assert final_call["status"] == TenantStatus.FREE
        assert final_call["last_sync_time"] is None
        # Reconcile gate untouched.
        assert _sync_scaffold["mark_reconcile_calls"] == []

    def test_syncing_partial_failure_without_prior_reconcile_ready_sets_load_incomplete(self, _sync_scaffold):
        """First-time sync (SYNCING) that fails mid-flight must land at LOAD_INCOMPLETE.

        This is the primary recovery path that the retry-sync endpoint exists
        to serve: ReconcileReadyAt has never been written, so a partial failure
        must surface the Retry-sync affordance rather than quietly returning
        the user to FREE.
        """
        _sync_scaffold["existing_record"] = None  # no prior ReconcileReadyAt
        _sync_scaffold["invoices_ok"] = False

        sync.sync_data("tenant-first-fail", TenantStatus.SYNCING)

        final_call = _sync_scaffold["status_calls"][-1]
        assert final_call["status"] == TenantStatus.LOAD_INCOMPLETE
        assert _sync_scaffold["mark_reconcile_calls"] == []
        assert _sync_scaffold["index_calls"] == []

    def test_manual_syncing_contacts_failure_preserves_reconcile_ready_tenant(self, _sync_scaffold):
        """Transient contacts failure during manual sync must not downgrade a reconcile-ready tenant.

        A user who can reconcile today must still be able to reconcile tomorrow
        if their manual sync hit a Xero blip on contacts — the LOAD_INCOMPLETE
        transition would yank the reconcile gate shut mid-session. Mirror the
        heavy-phase rule: FREE with null ``last_sync_time``.
        """
        _sync_scaffold["existing_record"] = {"ReconcileReadyAt": 1700000000000}
        _sync_scaffold["contacts_ok"] = False

        sync.sync_data("tenant-manual-contacts-fail", TenantStatus.SYNCING)

        final_call = _sync_scaffold["status_calls"][-1]
        assert final_call["status"] == TenantStatus.FREE
        assert final_call["last_sync_time"] is None
        assert _sync_scaffold["mark_reconcile_calls"] == []
        assert _sync_scaffold["index_calls"] == []

    def test_try_acquire_rejects_second_concurrent_call(self, _sync_scaffold):
        """On False from try_acquire_sync, sync_data logs and returns early."""
        _sync_scaffold["try_acquire_results"] = [False]

        sync.sync_data("tenant-busy", TenantStatus.LOADING)

        # No status updates, no fetching.
        assert _sync_scaffold["status_calls"] == []
        assert _sync_scaffold["index_calls"] == []
        assert _sync_scaffold["mark_reconcile_calls"] == []


@pytest.fixture()
def _progress_scaffold(monkeypatch, tmp_path):
    """Scaffold for testing per-resource progress writes through _sync_resource.

    Captures update_resource_progress calls so tests can assert on the
    sequence of status + counts writes per resource.
    """
    progress_writes: list[dict] = []

    class FakeRepo:
        @staticmethod
        def update_resource_progress(tenant_id, resource, status, records_fetched=None, record_total=None):
            progress_writes.append({"tenant_id": tenant_id, "resource": resource, "status": status, "records_fetched": records_fetched, "record_total": record_total})

    monkeypatch.setattr(sync, "TenantDataRepository", FakeRepo)

    # Avoid S3 + filesystem side effects.
    monkeypatch.setattr(sync, "LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "s3_client", MagicMock())
    monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

    return progress_writes


class TestPerResourceProgressWrites:
    """_sync_resource emits in_progress/complete/failed through update_resource_progress."""

    def test_success_path_writes_in_progress_then_complete(self, _progress_scaffold):
        """A successful page fetch fires in_progress with counts, then complete."""

        def fetcher(tenant_id, api=None, modified_since=None, progress_callback=None):  # noqa: ARG001
            # Simulate two pages.
            if progress_callback is not None:
                progress_callback(50, 100)
                progress_callback(100, 100)
            return [{"id": i} for i in range(100)]

        result = sync._sync_resource(MagicMock(), "tenant-ok", fetcher, sync.XeroType.INVOICES, "start", "done")

        assert result is True
        statuses = [w["status"] for w in _progress_scaffold]
        # Initial in_progress(0, None), two from callback, then complete.
        assert statuses[0] == "in_progress"
        assert _progress_scaffold[0]["records_fetched"] == 0
        assert _progress_scaffold[0]["record_total"] is None
        assert statuses[-1] == "complete"

        # At least the two callback-driven in_progress writes happened.
        in_progress_count = sum(1 for w in _progress_scaffold if w["status"] == "in_progress")
        assert in_progress_count >= 3  # initial + 2 pages

        # Final complete carries the accumulator + total.
        final = _progress_scaffold[-1]
        assert final["status"] == "complete"
        assert final["records_fetched"] == 100
        assert final["record_total"] == 100

    def test_failure_path_writes_in_progress_then_failed(self, _progress_scaffold):
        """When the fetcher raises, progress is marked failed (not complete)."""

        def fetcher(tenant_id, api=None, modified_since=None, progress_callback=None):  # noqa: ARG001
            raise RuntimeError("Xero 500")

        result = sync._sync_resource(MagicMock(), "tenant-boom", fetcher, sync.XeroType.PAYMENTS, "start", "done")

        assert result is False
        statuses = [w["status"] for w in _progress_scaffold]
        assert statuses[0] == "in_progress"
        assert statuses[-1] == "failed"
        assert _progress_scaffold[-1]["resource"] == sync.XeroType.PAYMENTS

    def test_record_total_none_preserved_through_callback(self, _progress_scaffold):
        """If pagination is absent, record_total=None must propagate (indeterminate)."""

        def fetcher(tenant_id, api=None, modified_since=None, progress_callback=None):  # noqa: ARG001
            if progress_callback is not None:
                progress_callback(5, None)
            return [{"id": 0}]

        sync._sync_resource(MagicMock(), "tenant-indeterminate", fetcher, sync.XeroType.CONTACTS, "start", "done")

        page_writes = [w for w in _progress_scaffold if w["status"] == "in_progress" and w["records_fetched"] == 5]
        assert page_writes, "Expected in_progress write with records_fetched=5"
        assert page_writes[0]["record_total"] is None

    def test_progress_write_failure_is_swallowed_and_logged(self, monkeypatch, tmp_path):
        """A DDB blip writing progress must NOT abort the sync (telemetry, not contract)."""
        from botocore.exceptions import ClientError

        # Filesystem + S3 no-ops, same as _progress_scaffold.
        monkeypatch.setattr(sync, "LOCAL_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(sync, "s3_client", MagicMock())
        monkeypatch.setattr(sync, "S3_BUCKET_NAME", "test-bucket")

        class _FailingRepo:
            @staticmethod
            def update_resource_progress(*args, **kwargs):  # noqa: ARG004
                raise ClientError({"Error": {"Code": "InternalServerError", "Message": "ddb blip"}}, "UpdateItem")

        monkeypatch.setattr(sync, "TenantDataRepository", _FailingRepo)

        exceptions_logged: list[str] = []
        monkeypatch.setattr(sync.logger, "exception", lambda msg, **_: exceptions_logged.append(msg))

        def fetcher(tenant_id, api=None, modified_since=None, progress_callback=None):  # noqa: ARG001
            return [{"id": 1}]

        # Should complete despite every update_resource_progress raising.
        result = sync._sync_resource(MagicMock(), "tenant-blip", fetcher, sync.XeroType.INVOICES, "start", "done")

        assert result is True
        # At least one progress-write failure must have been logged for telemetry.
        assert any("Failed to write sync progress" in msg for msg in exceptions_logged)


class TestPerContactIndexProgress:
    """build_per_contact_index is wrapped with PerContactIndexProgress writes."""

    def test_index_build_writes_in_progress_and_complete(self, _sync_scaffold, monkeypatch):
        progress_writes: list[dict] = []

        fake_repo = _sync_scaffold["repo"]

        def _record_progress(tenant_id, resource, status, records_fetched=None, record_total=None):
            progress_writes.append({"tenant_id": tenant_id, "resource": resource, "status": status})

        fake_repo.update_resource_progress = _record_progress

        sync.sync_data("tenant-pci-ok", TenantStatus.LOADING)

        index_writes = [w for w in progress_writes if w["resource"] == "per_contact_index"]
        assert [w["status"] for w in index_writes] == ["in_progress", "complete"]

    def test_index_build_writes_failed_when_exception_raised(self, _sync_scaffold, monkeypatch):
        progress_writes: list[dict] = []

        fake_repo = _sync_scaffold["repo"]
        fake_repo.update_resource_progress = lambda tenant_id, resource, status, records_fetched=None, record_total=None: progress_writes.append(
            {"tenant_id": tenant_id, "resource": resource, "status": status}
        )

        def boom(tenant_id):  # noqa: ARG001
            raise RuntimeError("index crash")

        monkeypatch.setattr(sync, "build_per_contact_index", boom)

        sync.sync_data("tenant-pci-fail", TenantStatus.LOADING)

        index_writes = [w for w in progress_writes if w["resource"] == "per_contact_index"]
        assert "failed" in {w["status"] for w in index_writes}


class TestSyncDataRetry:
    """sync_data(only_run_resources=...) skips completed resources and triggers index rebuild on full success."""

    def _complete_progress(self):
        return {"status": "complete", "records_fetched": 10, "record_total": 10}

    def test_only_run_resources_skips_resources_not_in_set(self, _sync_scaffold, monkeypatch):
        """Resources outside the set must not be fetched, even if not yet complete."""
        heavy_calls: list[str] = []

        def make_recorder(name, ok=True):
            def fn(api, tenant_id, modified_since=None):  # noqa: ARG001
                heavy_calls.append(name)
                return ok

            return fn

        monkeypatch.setattr(sync, "sync_contacts", make_recorder("contacts"))
        monkeypatch.setattr(sync, "sync_credit_notes", make_recorder("credit_notes"))
        monkeypatch.setattr(sync, "sync_invoices", make_recorder("invoices"))
        monkeypatch.setattr(sync, "sync_payments", make_recorder("payments"))

        # Pre-run: invoices failed, everything else already complete. Only invoices
        # is in the retry set, and the "skip complete" guard means the other
        # resources are skipped for two reasons (not in set + already complete).
        pre_run = {
            "ContactsProgress": self._complete_progress(),
            "CreditNotesProgress": self._complete_progress(),
            "InvoicesProgress": {"status": "failed", "records_fetched": 25, "record_total": 100},
            "PaymentsProgress": self._complete_progress(),
        }
        post_run = {**pre_run, "InvoicesProgress": self._complete_progress()}
        _sync_scaffold["existing_record"] = pre_run
        # Ensure the final re-read (for all-complete evaluation) sees invoices as complete.
        records = [pre_run, post_run]
        _sync_scaffold["repo"].get_item.side_effect = lambda tid: records.pop(0) if len(records) > 1 else records[0]  # noqa: ARG005

        sync.sync_data("tenant-retry", TenantStatus.SYNCING, only_run_resources={"invoices"})

        # Only invoices must have been fetched; contacts and the others are scoped out.
        assert heavy_calls == ["invoices"]

    def test_only_run_resources_skips_already_complete_resource(self, _sync_scaffold, monkeypatch):
        """Race safety: a resource listed in only_run but already complete must be skipped."""
        heavy_calls: list[str] = []
        monkeypatch.setattr(sync, "sync_invoices", lambda *a, **kw: heavy_calls.append("invoices") or True)
        monkeypatch.setattr(sync, "sync_credit_notes", lambda *a, **kw: True)
        monkeypatch.setattr(sync, "sync_payments", lambda *a, **kw: True)
        monkeypatch.setattr(sync, "sync_contacts", lambda *a, **kw: True)

        _sync_scaffold["existing_record"] = {"InvoicesProgress": self._complete_progress()}

        sync.sync_data("tenant-already", TenantStatus.SYNCING, only_run_resources={"invoices"})

        # invoices was already complete → skipped.
        assert heavy_calls == []

    def test_retry_full_success_triggers_index_and_mark_reconcile_ready(self, _sync_scaffold):
        """After the retried resources succeed and all resources are complete, index + reconcile must fire."""
        _sync_scaffold["existing_record"] = {"ContactsProgress": self._complete_progress(), "CreditNotesProgress": self._complete_progress(), "PaymentsProgress": self._complete_progress()}
        # Simulate invoices landing as complete after the retry run: the repo re-read
        # returns the in-progress snapshot above PLUS invoices now marked complete.
        fresh_record = {**_sync_scaffold["existing_record"], "InvoicesProgress": self._complete_progress()}
        get_item_calls: list[int] = []

        def _get_item(tid):  # noqa: ARG001
            get_item_calls.append(len(get_item_calls))
            return _sync_scaffold["existing_record"] if not get_item_calls[:-1] else fresh_record

        _sync_scaffold["repo"].get_item.side_effect = _get_item

        sync.sync_data("tenant-retry-done", TenantStatus.SYNCING, only_run_resources={"invoices"})

        # Index built, reconcile flipped (no prior ReconcileReadyAt).
        assert _sync_scaffold["index_calls"] == ["tenant-retry-done"]
        assert _sync_scaffold["mark_reconcile_calls"] == ["tenant-retry-done"]
        # Final status FREE.
        final = _sync_scaffold["status_calls"][-1]
        assert final["status"] == TenantStatus.FREE

    def test_retry_rebuilds_per_contact_index_when_only_index_was_failed(self, _sync_scaffold, monkeypatch):
        """Retry with only_run_resources={'per_contact_index'} routes through the index-rebuild branch.

        Regression: before the ``ALL_SYNC_RESOURCES`` consolidation,
        ``per_contact_index`` was excluded from the retry resource set, so an
        index-only failure returned 409 "Nothing to retry" and trapped the
        tenant in LOAD_INCOMPLETE. Every heavy fetcher must be skipped; only
        the index build runs.
        """
        heavy_calls: list[str] = []
        monkeypatch.setattr(sync, "sync_contacts", lambda *a, **kw: heavy_calls.append("contacts") or True)
        monkeypatch.setattr(sync, "sync_credit_notes", lambda *a, **kw: heavy_calls.append("credit_notes") or True)
        monkeypatch.setattr(sync, "sync_invoices", lambda *a, **kw: heavy_calls.append("invoices") or True)
        monkeypatch.setattr(sync, "sync_payments", lambda *a, **kw: heavy_calls.append("payments") or True)

        _sync_scaffold["existing_record"] = {
            "ContactsProgress": self._complete_progress(),
            "CreditNotesProgress": self._complete_progress(),
            "InvoicesProgress": self._complete_progress(),
            "PaymentsProgress": self._complete_progress(),
            "PerContactIndexProgress": {"status": "failed"},
        }

        sync.sync_data("tenant-index-retry", TenantStatus.SYNCING, only_run_resources={"per_contact_index"})

        # No fetcher may re-run.
        assert heavy_calls == []
        # Index rebuild + reconcile-ready flip + final FREE.
        assert _sync_scaffold["index_calls"] == ["tenant-index-retry"]
        assert _sync_scaffold["mark_reconcile_calls"] == ["tenant-index-retry"]
        final_call = _sync_scaffold["status_calls"][-1]
        assert final_call["status"] == TenantStatus.FREE

    def test_retry_with_partial_failure_sets_load_incomplete(self, _sync_scaffold, monkeypatch):
        """If the retried resource still fails, stay in LOAD_INCOMPLETE."""
        monkeypatch.setattr(sync, "sync_invoices", lambda *a, **kw: False)

        _sync_scaffold["existing_record"] = {
            "ContactsProgress": self._complete_progress(),
            "CreditNotesProgress": self._complete_progress(),
            "PaymentsProgress": self._complete_progress(),
            # invoices still missing/incomplete in the pre-run snapshot.
        }
        _sync_scaffold["repo"].get_item.side_effect = lambda tid: _sync_scaffold["existing_record"]  # noqa: ARG005

        sync.sync_data("tenant-retry-fail", TenantStatus.SYNCING, only_run_resources={"invoices"})

        final = _sync_scaffold["status_calls"][-1]
        assert final["status"] == TenantStatus.LOAD_INCOMPLETE
        assert _sync_scaffold["mark_reconcile_calls"] == []

    def test_already_acquired_skips_try_acquire(self, _sync_scaffold):
        """When the caller holds the lock already, try_acquire_sync must not be invoked."""
        sync.sync_data("tenant-preheld", TenantStatus.SYNCING, already_acquired=True)

        _sync_scaffold["repo"].try_acquire_sync.assert_not_called()


class TestSyncDataStartReset:
    """sync_data should reset progress sub-maps scoped to resources it will run."""

    def test_full_sync_resets_all_five_resources(self, monkeypatch):
        reset_calls: list[tuple] = []
        monkeypatch.setattr(sync.TenantDataRepository, "reset_resource_progress", classmethod(lambda cls, *a, **k: reset_calls.append((a, k))))
        monkeypatch.setattr(sync.TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: True))
        monkeypatch.setattr(sync.TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: None))
        # Raising from get_xero_api_client is a convenient way to abort sync
        # right after the reset so the test stays focused on the contract.
        monkeypatch.setattr(sync, "get_xero_api_client", lambda token: (_ for _ in ()).throw(RuntimeError("abort here")))

        with pytest.raises(RuntimeError):
            sync.sync_data("t-1", TenantStatus.LOADING, oauth_token={})

        assert len(reset_calls) == 1
        args, _kwargs = reset_calls[0]
        tenant_arg, resources_arg = args
        assert tenant_arg == "t-1"
        assert set(resources_arg) == {"contacts", "invoices", "credit_notes", "payments", "per_contact_index"}

    def test_retry_scopes_reset_to_only_run_resources(self, monkeypatch):
        reset_calls: list[tuple] = []
        monkeypatch.setattr(sync.TenantDataRepository, "reset_resource_progress", classmethod(lambda cls, *a, **k: reset_calls.append((a, k))))
        monkeypatch.setattr(sync.TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: True))
        monkeypatch.setattr(sync.TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: None))
        monkeypatch.setattr(sync, "get_xero_api_client", lambda token: (_ for _ in ()).throw(RuntimeError("abort here")))

        with pytest.raises(RuntimeError):
            sync.sync_data("t-2", TenantStatus.SYNCING, oauth_token={}, only_run_resources={"contacts"})

        assert len(reset_calls) == 1
        args, _kwargs = reset_calls[0]
        _tenant, resources_arg = args
        assert set(resources_arg) == {"contacts"}

    def test_skipped_sync_does_not_reset(self, monkeypatch):
        reset_calls: list[tuple] = []
        monkeypatch.setattr(sync.TenantDataRepository, "reset_resource_progress", classmethod(lambda cls, *a, **k: reset_calls.append((a, k))))
        monkeypatch.setattr(sync.TenantDataRepository, "try_acquire_sync", classmethod(lambda cls, *a, **k: False))

        # try_acquire_sync returning False → sync_data returns early.
        sync.sync_data("t-3", TenantStatus.LOADING, oauth_token={})
        assert reset_calls == []
