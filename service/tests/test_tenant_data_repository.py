"""Unit tests for tenant metadata repository helpers."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tenant_data_repository import TenantDataRepository, TenantStatus


def test_get_tenant_statuses_defaults_missing_rows_to_free(monkeypatch) -> None:
    """Missing tenant rows should keep the default FREE status."""

    rows = {"tenant-a": {"TenantStatus": "SYNCING"}, "tenant-b": None}

    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: rows.get(tenant_id)))

    statuses = TenantDataRepository.get_tenant_statuses(["tenant-a", "tenant-b"])

    assert statuses == {"tenant-a": TenantStatus.SYNCING, "tenant-b": TenantStatus.FREE}


def test_get_dismissed_banners_returns_set_from_item(monkeypatch) -> None:
    """Should extract DismissedBanners string set from the tenant row."""
    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: {"TenantID": tenant_id, "DismissedBanners": {"welcome-grant", "other-key"}}))
    result = TenantDataRepository.get_dismissed_banners("tenant-1")
    assert result == {"welcome-grant", "other-key"}


def test_get_dismissed_banners_returns_empty_set_when_missing(monkeypatch) -> None:
    """Should return empty set when attribute is absent."""
    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: {"TenantID": tenant_id}))
    result = TenantDataRepository.get_dismissed_banners("tenant-1")
    assert result == set()


def test_get_dismissed_banners_returns_empty_set_when_no_row(monkeypatch) -> None:
    """Should return empty set when the tenant has no row at all."""
    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: None))
    result = TenantDataRepository.get_dismissed_banners("tenant-1")
    assert result == set()


def test_dismiss_banner_calls_update_item(monkeypatch) -> None:
    """Should call DynamoDB update_item with ADD on the string set."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

    TenantDataRepository.dismiss_banner("tenant-1", "welcome-grant")

    assert len(calls) == 1
    assert calls[0]["Key"] == {"TenantID": "tenant-1"}
    assert "ADD DismissedBanners" in calls[0]["UpdateExpression"]
    assert calls[0]["ExpressionAttributeValues"][":dismiss_key"] == {"welcome-grant"}


def test_tenant_status_enum_has_erased_and_load_incomplete() -> None:
    """Enum should include ERASED and LOAD_INCOMPLETE values."""
    assert TenantStatus.ERASED == "ERASED"
    assert TenantStatus.LOAD_INCOMPLETE == "LOAD_INCOMPLETE"


def test_determine_status_recognises_erased() -> None:
    """ERASED status string should parse to the enum member."""
    item = {"TenantStatus": "ERASED"}
    result = TenantDataRepository._determine_status(item)
    assert result == TenantStatus.ERASED


def test_determine_status_recognises_load_incomplete() -> None:
    """LOAD_INCOMPLETE status string should parse to the enum member."""
    item = {"TenantStatus": "LOAD_INCOMPLETE"}
    result = TenantDataRepository._determine_status(item)
    assert result == TenantStatus.LOAD_INCOMPLETE


def test_schedule_erasure_sets_attributes(monkeypatch) -> None:
    """schedule_erasure should SET EraseTenantDataTime and conditionally update status."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

    TenantDataRepository.schedule_erasure("tenant-1", erasure_epoch_ms=1700000000000, current_status=TenantStatus.LOADING)

    assert len(calls) == 1
    assert calls[0]["Key"] == {"TenantID": "tenant-1"}
    assert ":erasure_time" in calls[0]["ExpressionAttributeValues"]
    assert calls[0]["ExpressionAttributeValues"].get(":new_status") == TenantStatus.LOAD_INCOMPLETE


def test_schedule_erasure_keeps_free_status(monkeypatch) -> None:
    """FREE status should not be changed during erasure scheduling."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

    TenantDataRepository.schedule_erasure("tenant-1", erasure_epoch_ms=1700000000000, current_status=TenantStatus.FREE)

    assert len(calls) == 1
    assert ":new_status" not in calls[0].get("ExpressionAttributeValues", {})


class TestScheduleErasureSyncingBranches:
    """SYNCING transitions depend on ReconcileReadyAt: null -> LOAD_INCOMPLETE, set -> FREE.

    The gate preserves ``LOAD_INCOMPLETE`` when a user disconnects mid-heavy-phase of the
    initial load (before ``ReconcileReadyAt`` was written) so the UI keeps the Retry-sync
    affordance; falls back to ``FREE`` for later incremental/manual syncs.
    """

    @staticmethod
    def _install_fake_table(monkeypatch, item: dict | None):
        update_calls: list[dict] = []

        class FakeTable:
            @staticmethod
            def get_item(**_):
                return {"Item": item} if item is not None else {}

            @staticmethod
            def update_item(**kwargs):
                update_calls.append(kwargs)

        monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())
        return update_calls

    def test_syncing_without_reconcile_ready_transitions_to_load_incomplete(self, monkeypatch):
        """Initial heavy-phase disconnect must keep LOAD_INCOMPLETE (Retry-sync path)."""
        calls = self._install_fake_table(monkeypatch, item={"TenantID": "tenant-1", "TenantStatus": "SYNCING"})

        TenantDataRepository.schedule_erasure("tenant-1", erasure_epoch_ms=1700000000000, current_status=TenantStatus.SYNCING)

        assert len(calls) == 1
        assert calls[0]["ExpressionAttributeValues"][":new_status"] == TenantStatus.LOAD_INCOMPLETE

    def test_syncing_with_reconcile_ready_transitions_to_free(self, monkeypatch):
        """Post-initial incremental sync disconnect returns to FREE (unchanged behaviour)."""
        calls = self._install_fake_table(monkeypatch, item={"TenantID": "tenant-1", "TenantStatus": "SYNCING", "ReconcileReadyAt": 1700000000000})

        TenantDataRepository.schedule_erasure("tenant-1", erasure_epoch_ms=1700000000000, current_status=TenantStatus.SYNCING)

        assert len(calls) == 1
        assert calls[0]["ExpressionAttributeValues"][":new_status"] == TenantStatus.FREE

    def test_syncing_without_tenant_row_transitions_to_load_incomplete(self, monkeypatch):
        """Defensive: missing row during SYNCING disconnect treated as never-reconciled."""
        calls = self._install_fake_table(monkeypatch, item=None)

        TenantDataRepository.schedule_erasure("tenant-1", erasure_epoch_ms=1700000000000, current_status=TenantStatus.SYNCING)

        assert len(calls) == 1
        assert calls[0]["ExpressionAttributeValues"][":new_status"] == TenantStatus.LOAD_INCOMPLETE


def test_cancel_erasure_removes_attribute(monkeypatch) -> None:
    """cancel_erasure should REMOVE EraseTenantDataTime."""
    calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

    TenantDataRepository.cancel_erasure("tenant-1")

    assert len(calls) == 1
    assert "REMOVE EraseTenantDataTime" in calls[0]["UpdateExpression"]


def _fake_table_capturing_calls(monkeypatch):
    """Install a fake table whose update_item/batch_get_item record kwargs."""
    update_calls: list[dict] = []

    class FakeTable:
        @staticmethod
        def update_item(**kwargs):
            update_calls.append(kwargs)

    monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())
    return update_calls


class TestUpdateResourceProgress:
    """update_resource_progress writes per-resource progress map plus heartbeat."""

    def test_writes_progress_map_with_counts(self, monkeypatch):
        calls = _fake_table_capturing_calls(monkeypatch)
        TenantDataRepository.update_resource_progress("tenant-1", "invoices", "in_progress", records_fetched=100, record_total=500)

        assert len(calls) == 1
        call = calls[0]
        assert call["Key"] == {"TenantID": "tenant-1"}
        # Attribute name should be the PascalCase Progress field.
        assert "InvoicesProgress" in call["UpdateExpression"] or ":progress" in call["UpdateExpression"]
        values = call["ExpressionAttributeValues"]
        progress = values[":progress"]
        assert progress["status"] == "in_progress"
        assert progress["records_fetched"] == 100
        assert progress["record_total"] == 500
        assert "updated_at" in progress
        # LastHeartbeatAt also updated.
        assert ":heartbeat" in values
        assert isinstance(values[":heartbeat"], int)

    def test_per_contact_index_resource_maps_to_pascal_case(self, monkeypatch):
        calls = _fake_table_capturing_calls(monkeypatch)
        TenantDataRepository.update_resource_progress("tenant-1", "per_contact_index", "in_progress")

        assert len(calls) == 1
        call = calls[0]
        # per_contact_index -> PerContactIndexProgress
        assert "PerContactIndex" in call["ExpressionAttributeNames"].get("#progress", "")
        progress = call["ExpressionAttributeValues"][":progress"]
        assert progress["status"] == "in_progress"
        # per_contact_index has no counts — omit the count keys.
        assert "records_fetched" not in progress
        assert "record_total" not in progress

    def test_record_total_none_is_preserved_as_null(self, monkeypatch):
        """When Xero can't return a total, record_total=None must be written, not omitted.

        Downstream UI relies on record_total=null to render indeterminate progress.
        """
        calls = _fake_table_capturing_calls(monkeypatch)
        TenantDataRepository.update_resource_progress("tenant-1", "invoices", "in_progress", records_fetched=50, record_total=None)

        progress = calls[0]["ExpressionAttributeValues"][":progress"]
        assert progress["records_fetched"] == 50
        assert progress["record_total"] is None


class TestMarkReconcileReady:
    """mark_reconcile_ready writes both ReconcileReadyAt and LastFullLoadCompletedAt."""

    def test_writes_both_timestamps(self, monkeypatch):
        calls = _fake_table_capturing_calls(monkeypatch)
        TenantDataRepository.mark_reconcile_ready("tenant-1")

        assert len(calls) == 1
        call = calls[0]
        expr = call["UpdateExpression"]
        assert "ReconcileReadyAt" in expr
        assert "LastFullLoadCompletedAt" in expr
        # Both should be the same timestamp value.
        values = call["ExpressionAttributeValues"]
        assert values[":reconcile_ready_at"] == values[":completed_at"]
        assert isinstance(values[":reconcile_ready_at"], int)


class TestGetMany:
    """get_many batches tenant lookups via BatchGetItem."""

    def test_returns_item_per_tenant(self, monkeypatch):
        responses = {"Responses": {"TenantData": [{"TenantID": "t1", "TenantStatus": "FREE"}, {"TenantID": "t2", "TenantStatus": "LOADING"}]}, "UnprocessedKeys": {}}

        fake_client = MagicMock()
        fake_client.batch_get_item.return_value = responses

        monkeypatch.setattr("tenant_data_repository.ddb", MagicMock(batch_get_item=fake_client.batch_get_item))

        # Table name must be resolvable — monkeypatch the module-level name used by get_many.
        class FakeTable:
            name = "TenantData"

        monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

        result = TenantDataRepository.get_many(["t1", "t2", "t3"])

        assert result["t1"]["TenantStatus"] == "FREE"
        assert result["t2"]["TenantStatus"] == "LOADING"
        assert result["t3"] is None

    def test_empty_list_returns_empty_dict(self, monkeypatch):
        result = TenantDataRepository.get_many([])
        assert result == {}

    def test_caps_at_100_keys(self, monkeypatch):
        """BatchGetItem has a hard 100-key cap per call."""
        responses = {"Responses": {"TenantData": []}, "UnprocessedKeys": {}}
        fake_client = MagicMock()
        fake_client.batch_get_item.return_value = responses

        monkeypatch.setattr("tenant_data_repository.ddb", MagicMock(batch_get_item=fake_client.batch_get_item))

        class FakeTable:
            name = "TenantData"

        monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

        ids = [f"t{i}" for i in range(150)]
        result = TenantDataRepository.get_many(ids)

        # All 150 tenants must still appear as keys (missing ones → None).
        assert len(result) == 150
        # The batch_get_item must have been called (at most twice; 100 + 50).
        assert fake_client.batch_get_item.call_count >= 1


class TestTryAcquireSync:
    """try_acquire_sync uses ConditionExpression to prevent double-starts."""

    def _install_fake_table(self, monkeypatch, raise_on_conditional: bool = False):
        state = {"raise_on_conditional": raise_on_conditional}
        calls: list[dict] = []

        class FakeTable:
            @staticmethod
            def update_item(**kwargs):
                calls.append(kwargs)
                if state["raise_on_conditional"]:
                    raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}}, "UpdateItem")

        monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())
        return calls

    def test_acquires_when_tenant_row_absent(self, monkeypatch):
        calls = self._install_fake_table(monkeypatch, raise_on_conditional=False)

        result = TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.LOADING, stale_threshold_ms=300_000)

        assert result is True
        assert len(calls) == 1
        # ConditionExpression must be present.
        assert "ConditionExpression" in calls[0]
        # TenantStatus and LastHeartbeatAt must both be SET.
        expr = calls[0]["UpdateExpression"]
        assert "TenantStatus" in expr or calls[0]["ExpressionAttributeNames"].get("#ts") == "TenantStatus"
        assert "LastHeartbeatAt" in expr

    def test_acquires_on_free(self, monkeypatch):
        calls = self._install_fake_table(monkeypatch, raise_on_conditional=False)

        result = TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.SYNCING, stale_threshold_ms=300_000)

        assert result is True

    def test_acquires_on_load_incomplete(self, monkeypatch):
        """Retry-sync must be allowed when a prior load failed."""
        calls = self._install_fake_table(monkeypatch, raise_on_conditional=False)

        result = TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.LOADING, stale_threshold_ms=300_000)

        assert result is True

    def test_rejects_when_conditional_check_fails(self, monkeypatch):
        """Fresh heartbeat on an in-flight sync should block a new start."""
        self._install_fake_table(monkeypatch, raise_on_conditional=True)

        result = TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.LOADING, stale_threshold_ms=300_000)

        assert result is False

    def test_other_client_errors_propagate(self, monkeypatch):
        """Unrelated errors (permissions, throttling) should NOT be swallowed."""

        class FakeTable:
            @staticmethod
            def update_item(**_):
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "UpdateItem")

        monkeypatch.setattr(TenantDataRepository, "_table", FakeTable())

        with pytest.raises(ClientError):
            TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.LOADING, stale_threshold_ms=300_000)

    def test_condition_expression_includes_stale_threshold(self, monkeypatch):
        """Condition must reference :stale_threshold = now - duration for the heartbeat comparison."""
        calls = self._install_fake_table(monkeypatch, raise_on_conditional=False)

        # Freeze time so :stale_threshold is deterministic.
        monkeypatch.setattr("tenant_data_repository._now_ms", lambda: 1_700_000_000_000)
        duration_ms = 5 * 60 * 1000
        TenantDataRepository.try_acquire_sync("tenant-1", target_status=TenantStatus.LOADING, stale_threshold_ms=duration_ms)

        condition = calls[0]["ConditionExpression"]
        assert "LastHeartbeatAt" in condition
        values = calls[0]["ExpressionAttributeValues"]
        # Threshold = now_ms() - duration_ms.
        assert values.get(":stale_threshold") == 1_700_000_000_000 - duration_ms
        # The condition must allow FREE and LOAD_INCOMPLETE.
        assert ":free" in values
        assert ":load_incomplete" in values
