"""Unit tests for tenant billing repository helpers."""

from decimal import Decimal

from tenant_billing_repository import TenantBillingRepository


def test_get_tenant_token_balances_normalizes_missing_and_decimal_values(monkeypatch) -> None:
    """Token balance lookups should coerce DynamoDB-style values into integers."""

    rows = {"tenant-a": {"TokenBalance": Decimal("27")}, "tenant-b": {"TokenBalance": 14}, "tenant-c": {}}

    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: rows.get(tenant_id)))

    balances = TenantBillingRepository.get_tenant_token_balances(["tenant-a", "tenant-b", "tenant-c"])

    assert balances == {"tenant-a": 27, "tenant-b": 14, "tenant-c": 0}


def test_get_tenant_token_balance_defaults_to_zero_when_row_missing(monkeypatch) -> None:
    """Single-tenant token lookups should treat a missing row as zero balance."""

    monkeypatch.setattr(TenantBillingRepository, "get_item", classmethod(lambda cls, tenant_id: None))

    assert TenantBillingRepository.get_tenant_token_balance("tenant-a") == 0
