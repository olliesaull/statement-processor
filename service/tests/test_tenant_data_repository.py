"""Unit tests for tenant metadata repository helpers."""

from decimal import Decimal

from tenant_data_repository import TenantDataRepository, TenantStatus


def test_get_tenant_statuses_defaults_missing_rows_to_free(monkeypatch) -> None:
    """Missing tenant rows should keep the default FREE status."""

    rows = {"tenant-a": {"TenantStatus": "SYNCING"}, "tenant-b": None}

    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: rows.get(tenant_id)))

    statuses = TenantDataRepository.get_tenant_statuses(["tenant-a", "tenant-b"])

    assert statuses == {"tenant-a": TenantStatus.SYNCING, "tenant-b": TenantStatus.FREE}


def test_get_tenant_token_balances_normalizes_missing_and_decimal_values(monkeypatch) -> None:
    """Token balance lookups should coerce DynamoDB-style values into integers."""

    rows = {"tenant-a": {"TokenBalance": Decimal("27")}, "tenant-b": {"TokenBalance": 14}, "tenant-c": {}}

    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: rows.get(tenant_id)))

    balances = TenantDataRepository.get_tenant_token_balances(["tenant-a", "tenant-b", "tenant-c"])

    assert balances == {"tenant-a": 27, "tenant-b": 14, "tenant-c": 0}


def test_get_tenant_token_balance_defaults_to_zero_when_row_missing(monkeypatch) -> None:
    """Single-tenant token lookups should treat a missing row as zero balance."""

    monkeypatch.setattr(TenantDataRepository, "get_item", classmethod(lambda cls, tenant_id: None))

    assert TenantDataRepository.get_tenant_token_balance("tenant-a") == 0
