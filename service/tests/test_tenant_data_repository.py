"""Unit tests for tenant metadata repository helpers."""

from tenant_data_repository import TenantDataRepository, TenantStatus


def test_get_tenant_statuses_defaults_missing_rows_to_free(monkeypatch) -> None:
    """Missing tenant rows should keep the default FREE status."""

    rows = {"tenant-a": {"TenantStatus": "SYNCING"}, "tenant-b": None}

    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: rows.get(tenant_id)))

    statuses = TenantDataRepository.get_tenant_statuses(["tenant-a", "tenant-b"])

    assert statuses == {"tenant-a": TenantStatus.SYNCING, "tenant-b": TenantStatus.FREE}
