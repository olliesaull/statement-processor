"""Tests for the reconcile-ready backfill migration script.

Focus on the idempotent candidate-selection predicate and the write loop.
The ``main()`` CLI path is not exercised here because it loads ``service.env``
and boots the full service config (SSM + Valkey).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# Make the script importable as a plain module without running ``main()``.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import backfill_reconcile_ready as script  # noqa: E402  pylint: disable=wrong-import-position


# ---------------------------------------------------------------------------
# needs_backfill
# ---------------------------------------------------------------------------


class TestNeedsBackfill:
    """Pure predicate — rows must be FREE + LastSyncTime + no ReconcileReadyAt."""

    def test_free_with_last_sync_and_no_reconcile_ready_is_candidate(self):
        assert script.needs_backfill({"TenantStatus": "FREE", "LastSyncTime": 1700000000000}) is True

    def test_already_backfilled_row_is_not_candidate(self):
        row = {"TenantStatus": "FREE", "LastSyncTime": 1700000000000, "ReconcileReadyAt": 1700000000000}
        assert script.needs_backfill(row) is False

    def test_missing_last_sync_time_is_not_candidate(self):
        assert script.needs_backfill({"TenantStatus": "FREE"}) is False

    def test_non_free_status_is_not_candidate(self):
        for status in ("LOADING", "SYNCING", "LOAD_INCOMPLETE", "ERASED"):
            assert script.needs_backfill({"TenantStatus": status, "LastSyncTime": 1}) is False, status

    def test_status_is_case_insensitive(self):
        assert script.needs_backfill({"TenantStatus": "free", "LastSyncTime": 1}) is True


# ---------------------------------------------------------------------------
# build_update_kwargs
# ---------------------------------------------------------------------------


class TestBuildUpdateKwargs:
    """UpdateItem payload must be idempotent and use if_not_exists for progress maps."""

    def test_writes_reconcile_ready_and_progress_with_if_not_exists(self):
        kwargs = script.build_update_kwargs("tenant-1", 1700000000000)

        assert kwargs["Key"] == {"TenantID": "tenant-1"}
        expr = kwargs["UpdateExpression"]
        assert "ReconcileReadyAt = :reconcile_ready_at" in expr
        assert "LastFullLoadCompletedAt = if_not_exists(LastFullLoadCompletedAt" in expr
        # Progress maps must use if_not_exists so we never clobber live progress.
        for key in ("ContactsProgress", "CreditNotesProgress", "InvoicesProgress", "PaymentsProgress", "PerContactIndexProgress"):
            assert f"if_not_exists(#{key}" in expr

    def test_condition_expression_guards_against_overwrite(self):
        kwargs = script.build_update_kwargs("tenant-1", 1700000000000)
        assert kwargs["ConditionExpression"] == "attribute_not_exists(ReconcileReadyAt)"

    def test_progress_payload_uses_last_sync_time(self):
        kwargs = script.build_update_kwargs("tenant-1", 1700000000000)
        values = kwargs["ExpressionAttributeValues"]
        payload = values[":ContactsProgress"]
        assert payload["status"] == "complete"
        assert payload["updated_at"] == 1700000000000
        assert payload["records_fetched"] is None
        assert payload["record_total"] is None
        assert values[":per_contact_index"] == {"status": "complete", "updated_at": 1700000000000}


# ---------------------------------------------------------------------------
# iter_tenant_items
# ---------------------------------------------------------------------------


class _FakeTable:
    """Minimal DynamoDB-style table stub.

    Emulates pagination via ``LastEvaluatedKey`` across a list of pages. Each
    ``update_item`` call appends its kwargs to ``self.writes`` so tests can
    assert the full payload.
    """

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.writes: list[dict] = []
        self.update_raises: dict[str, Exception] = {}

    def scan(self, **_kwargs):
        # ExclusiveStartKey contains the page index we should return next.
        start_key = _kwargs.get("ExclusiveStartKey")
        if start_key is None:
            page_idx = 0
        else:
            page_idx = start_key["__page__"]

        if page_idx >= len(self.pages):
            return {"Items": []}

        response = {"Items": self.pages[page_idx]}
        if page_idx + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"__page__": page_idx + 1}
        return response

    def update_item(self, **kwargs):
        tenant_id = kwargs["Key"]["TenantID"]
        if tenant_id in self.update_raises:
            raise self.update_raises[tenant_id]
        self.writes.append(kwargs)


class TestIterTenantItems:
    """Pagination: yields every item across multiple scan pages."""

    def test_iterates_multiple_pages(self):
        pages = [[{"TenantID": "a"}, {"TenantID": "b"}], [{"TenantID": "c"}]]
        table = _FakeTable(pages)

        yielded = [item["TenantID"] for item in script.iter_tenant_items(table)]

        assert yielded == ["a", "b", "c"]

    def test_empty_scan_yields_nothing(self):
        table = _FakeTable([])
        assert list(script.iter_tenant_items(table)) == []


# ---------------------------------------------------------------------------
# backfill_table (dry_run + write)
# ---------------------------------------------------------------------------


class TestBackfillTable:
    """Integration of predicate + iterator + write loop."""

    @staticmethod
    def _silent_logger():
        captured: list[str] = []

        def _log(msg):
            captured.append(str(msg))

        return _log, captured

    def test_dry_run_returns_candidates_without_writes(self):
        pages = [
            [
                {"TenantID": "candidate", "TenantStatus": "FREE", "LastSyncTime": 100},
                {"TenantID": "already-ready", "TenantStatus": "FREE", "LastSyncTime": 200, "ReconcileReadyAt": 200},
                {"TenantID": "loading", "TenantStatus": "LOADING", "LastSyncTime": 300},
            ]
        ]
        table = _FakeTable(pages)
        log, _ = self._silent_logger()

        candidate_count, written = script.backfill_table(table, dry_run=True, logger=log)

        assert candidate_count == 1
        assert written == 0
        assert table.writes == []

    def test_writes_once_per_candidate_on_live_run(self):
        pages = [
            [
                {"TenantID": "t1", "TenantStatus": "FREE", "LastSyncTime": 100},
                {"TenantID": "t2", "TenantStatus": "FREE", "LastSyncTime": 200},
                {"TenantID": "t3", "TenantStatus": "FREE", "LastSyncTime": 300, "ReconcileReadyAt": 300},
            ]
        ]
        table = _FakeTable(pages)
        log, _ = self._silent_logger()

        candidate_count, written = script.backfill_table(table, dry_run=False, logger=log)

        assert candidate_count == 2
        assert written == 2
        assert {w["Key"]["TenantID"] for w in table.writes} == {"t1", "t2"}

    def test_swallows_conditional_check_failure(self):
        """A concurrent writer may set ReconcileReadyAt between scan and update — treat as a no-op."""
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        pages = [[{"TenantID": "t1", "TenantStatus": "FREE", "LastSyncTime": 100}]]
        table = _FakeTable(pages)
        table.update_raises["t1"] = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        log, captured = self._silent_logger()

        candidate_count, written = script.backfill_table(table, dry_run=False, logger=log)

        assert candidate_count == 1
        assert written == 0
        assert any("raced" in line for line in captured)

    def test_logs_and_continues_on_other_errors(self):
        """An unrelated ClientError must not abort the whole run."""
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

        pages = [
            [
                {"TenantID": "t1", "TenantStatus": "FREE", "LastSyncTime": 100},
                {"TenantID": "t2", "TenantStatus": "FREE", "LastSyncTime": 200},
            ]
        ]
        table = _FakeTable(pages)
        table.update_raises["t1"] = ClientError({"Error": {"Code": "ProvisionedThroughputExceeded"}}, "UpdateItem")
        log, _ = self._silent_logger()

        candidate_count, written = script.backfill_table(table, dry_run=False, logger=log)

        assert candidate_count == 2
        # t2 must still succeed.
        assert written == 1
        assert {w["Key"]["TenantID"] for w in table.writes} == {"t2"}
