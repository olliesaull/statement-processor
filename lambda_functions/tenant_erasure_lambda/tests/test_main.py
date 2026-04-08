"""Unit tests for the tenant erasure Lambda handler."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

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
