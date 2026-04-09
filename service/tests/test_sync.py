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
