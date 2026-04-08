"""Unit tests for sync helpers."""

from unittest.mock import MagicMock

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
    mock_billing.adjust_token_balance.assert_called_once_with("new-tenant", 5, source="welcome-grant")


def test_check_load_required_does_not_grant_for_existing_tenant(monkeypatch) -> None:
    """Existing tenants should not receive any token grant."""
    fake_table = MagicMock()
    # Simulate existing row.
    fake_table.get_item.return_value = {"Item": {"TenantID": "existing-tenant", "TenantStatus": "FREE"}}
    monkeypatch.setattr(sync, "tenant_data_table", fake_table)

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

    mock_billing = MagicMock()
    monkeypatch.setattr(sync, "BillingService", mock_billing)

    mock_repo = MagicMock()
    monkeypatch.setattr(sync, "TenantDataRepository", mock_repo)

    result = sync.check_load_required("free-tenant")

    assert result is False
    mock_repo.cancel_erasure.assert_called_once_with("free-tenant")
