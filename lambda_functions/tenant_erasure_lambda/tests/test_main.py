"""Unit tests for the tenant erasure Lambda handler."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, call

import main


def _make_tenant(tenant_id: str, status: str = "FREE", erasure_time: int | None = None) -> dict:
    """Build a fake DynamoDB tenant item."""
    item: dict = {"TenantID": tenant_id, "TenantStatus": status}
    if erasure_time is not None:
        item["EraseTenantDataTime"] = erasure_time
    return item


def test_handler_erases_eligible_tenant(monkeypatch) -> None:
    """Tenant past erasure time with FREE status should be erased."""
    now_ms = int(time.time() * 1000)
    tenant = _make_tenant("t1", "FREE", now_ms - 1000)

    fake_data_table = MagicMock()
    fake_data_table.scan.return_value = {"Items": [tenant]}
    fake_data_table.update_item.return_value = {}
    monkeypatch.setattr(main, "tenant_data_table", fake_data_table)

    fake_stmt_table = MagicMock()
    fake_stmt_table.query.return_value = {"Items": []}
    monkeypatch.setattr(main, "tenant_statements_table", fake_stmt_table)

    fake_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    fake_s3.get_paginator.return_value = paginator
    monkeypatch.setattr(main, "s3_client", fake_s3)

    result = main.lambda_handler({}, None)

    assert result["erased"] == 1
    assert result["failed"] == 0
    fake_data_table.update_item.assert_called_once()


def test_handler_skips_active_tenant(monkeypatch) -> None:
    """Tenant with LOADING status should be skipped."""
    now_ms = int(time.time() * 1000)
    tenant = _make_tenant("t1", "LOADING", now_ms - 1000)

    fake_data_table = MagicMock()
    fake_data_table.scan.return_value = {"Items": [tenant]}
    monkeypatch.setattr(main, "tenant_data_table", fake_data_table)
    monkeypatch.setattr(main, "tenant_statements_table", MagicMock())
    monkeypatch.setattr(main, "s3_client", MagicMock())

    result = main.lambda_handler({}, None)

    assert result["skipped"] == 1
    assert result["erased"] == 0
    fake_data_table.update_item.assert_not_called()


def test_handler_handles_conditional_check_failure(monkeypatch) -> None:
    """ConditionalCheckFailedException (tenant reconnected) should be skipped, not failed."""
    from botocore.exceptions import ClientError

    now_ms = int(time.time() * 1000)
    tenant = _make_tenant("t1", "FREE", now_ms - 1000)

    fake_data_table = MagicMock()
    fake_data_table.scan.return_value = {"Items": [tenant]}
    fake_data_table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}}, "UpdateItem")
    monkeypatch.setattr(main, "tenant_data_table", fake_data_table)

    fake_stmt_table = MagicMock()
    fake_stmt_table.query.return_value = {"Items": []}
    monkeypatch.setattr(main, "tenant_statements_table", fake_stmt_table)

    fake_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    fake_s3.get_paginator.return_value = paginator
    monkeypatch.setattr(main, "s3_client", fake_s3)

    result = main.lambda_handler({}, None)

    assert result["skipped"] == 1
    assert result["failed"] == 0


def test_handler_continues_after_single_failure(monkeypatch) -> None:
    """One tenant failing should not block erasure of the next."""
    now_ms = int(time.time() * 1000)
    tenants = [_make_tenant("t-fail", "FREE", now_ms - 1000), _make_tenant("t-ok", "FREE", now_ms - 1000)]

    fake_data_table = MagicMock()
    fake_data_table.scan.return_value = {"Items": tenants}

    call_count = {"n": 0}

    def mock_update_item(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulated failure")

    fake_data_table.update_item = mock_update_item
    monkeypatch.setattr(main, "tenant_data_table", fake_data_table)

    fake_stmt_table = MagicMock()
    fake_stmt_table.query.return_value = {"Items": []}
    monkeypatch.setattr(main, "tenant_statements_table", fake_stmt_table)

    fake_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    fake_s3.get_paginator.return_value = paginator
    monkeypatch.setattr(main, "s3_client", fake_s3)

    result = main.lambda_handler({}, None)

    assert result["failed"] == 1
    assert result["erased"] == 1


def test_scan_handles_pagination(monkeypatch) -> None:
    """Scanner should follow LastEvaluatedKey through all pages."""
    now_ms = int(time.time() * 1000)

    page1 = {"Items": [_make_tenant("t1", erasure_time=now_ms - 1)], "LastEvaluatedKey": {"TenantID": "t1"}}
    page2 = {"Items": [_make_tenant("t2", erasure_time=now_ms - 1)]}

    fake_table = MagicMock()
    fake_table.scan.side_effect = [page1, page2]
    monkeypatch.setattr(main, "tenant_data_table", fake_table)

    result = main._scan_for_erasable_tenants(now_ms)
    assert len(result) == 2
    assert fake_table.scan.call_count == 2


def test_handler_skips_syncing_tenant(monkeypatch) -> None:
    """Tenant with SYNCING status should be skipped (active operation)."""
    now_ms = int(time.time() * 1000)
    tenant = _make_tenant("t1", "SYNCING", now_ms - 1000)

    fake_data_table = MagicMock()
    fake_data_table.scan.return_value = {"Items": [tenant]}
    monkeypatch.setattr(main, "tenant_data_table", fake_data_table)
    monkeypatch.setattr(main, "tenant_statements_table", MagicMock())
    monkeypatch.setattr(main, "s3_client", MagicMock())

    result = main.lambda_handler({}, None)

    assert result["skipped"] == 1
    assert result["erased"] == 0
    fake_data_table.update_item.assert_not_called()


def test_mark_as_erased_uses_conditional_write(monkeypatch) -> None:
    """_mark_as_erased should SET ERASED, REMOVE erasure time + last sync, with condition."""
    fake_table = MagicMock()
    monkeypatch.setattr(main, "tenant_data_table", fake_table)

    main._mark_as_erased("tenant-abc")

    fake_table.update_item.assert_called_once()
    kwargs = fake_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"TenantID": "tenant-abc"}
    assert "SET TenantStatus = :erased" in kwargs["UpdateExpression"]
    assert "REMOVE EraseTenantDataTime" in kwargs["UpdateExpression"]
    assert "REMOVE" in kwargs["UpdateExpression"] and "LastSyncTime" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":erased"] == "ERASED"
    assert kwargs["ConditionExpression"] == "attribute_exists(EraseTenantDataTime)"


def test_delete_statement_rows_batch_deletes_with_correct_keys(monkeypatch) -> None:
    """_delete_statement_rows should query by TenantID and batch-delete all rows."""
    fake_table = MagicMock()
    # Simulate two items returned, no pagination.
    fake_table.query.return_value = {"Items": [{"TenantID": "t1", "StatementID": "stmt-001"}, {"TenantID": "t1", "StatementID": "stmt-001#item-1"}]}
    # batch_writer returns a context manager.
    fake_batch = MagicMock()
    fake_table.batch_writer.return_value.__enter__ = MagicMock(return_value=fake_batch)
    fake_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(main, "tenant_statements_table", fake_table)

    deleted = main._delete_statement_rows("t1")

    assert deleted == 2
    # Verify correct composite keys used for deletion.
    fake_batch.delete_item.assert_any_call(Key={"TenantID": "t1", "StatementID": "stmt-001"})
    fake_batch.delete_item.assert_any_call(Key={"TenantID": "t1", "StatementID": "stmt-001#item-1"})


def test_delete_statement_rows_handles_pagination(monkeypatch) -> None:
    """_delete_statement_rows should follow LastEvaluatedKey through all pages."""
    fake_table = MagicMock()
    page1 = {"Items": [{"TenantID": "t1", "StatementID": "stmt-001"}], "LastEvaluatedKey": {"TenantID": "t1", "StatementID": "stmt-001"}}
    page2 = {"Items": [{"TenantID": "t1", "StatementID": "stmt-002"}]}
    fake_table.query.side_effect = [page1, page2]

    fake_batch = MagicMock()
    fake_table.batch_writer.return_value.__enter__ = MagicMock(return_value=fake_batch)
    fake_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr(main, "tenant_statements_table", fake_table)

    deleted = main._delete_statement_rows("t1")

    assert deleted == 2
    assert fake_table.query.call_count == 2


def test_delete_s3_objects_calls_delete_with_correct_keys(monkeypatch) -> None:
    """_delete_s3_objects should list under tenant prefix and batch-delete."""
    fake_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": "t1/statements/stmt-001.pdf"}, {"Key": "t1/statements/stmt-001.json"}, {"Key": "t1/data/contacts.json"}]}]
    fake_s3.get_paginator.return_value = paginator
    monkeypatch.setattr(main, "s3_client", fake_s3)
    monkeypatch.setattr(main, "S3_BUCKET_NAME", "test-bucket")

    deleted = main._delete_s3_objects("t1")

    assert deleted == 3
    paginator.paginate.assert_called_once_with(Bucket="test-bucket", Prefix="t1/")
    fake_s3.delete_objects.assert_called_once()
    delete_call = fake_s3.delete_objects.call_args
    objects = delete_call.kwargs["Delete"]["Objects"]
    keys = {obj["Key"] for obj in objects}
    assert keys == {"t1/statements/stmt-001.pdf", "t1/statements/stmt-001.json", "t1/data/contacts.json"}


def test_delete_s3_objects_returns_zero_for_empty_prefix(monkeypatch) -> None:
    """No objects under prefix should return 0 and not call delete_objects."""
    fake_s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    fake_s3.get_paginator.return_value = paginator
    monkeypatch.setattr(main, "s3_client", fake_s3)
    monkeypatch.setattr(main, "S3_BUCKET_NAME", "test-bucket")

    deleted = main._delete_s3_objects("t1")

    assert deleted == 0
    fake_s3.delete_objects.assert_not_called()
