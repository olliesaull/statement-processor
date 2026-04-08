"""Unit tests for tenant metadata repository helpers."""

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
