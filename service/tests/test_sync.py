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

    def test_try_acquire_rejects_second_concurrent_call(self, _sync_scaffold):
        """On False from try_acquire_sync, sync_data logs and returns early."""
        _sync_scaffold["try_acquire_results"] = [False]

        sync.sync_data("tenant-busy", TenantStatus.LOADING)

        # No status updates, no fetching.
        assert _sync_scaffold["status_calls"] == []
        assert _sync_scaffold["index_calls"] == []
        assert _sync_scaffold["mark_reconcile_calls"] == []
