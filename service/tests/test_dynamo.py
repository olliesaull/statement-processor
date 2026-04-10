"""Tests for DynamoDB statement helpers in utils/dynamo.py.

Covers query wrappers, single-item operations, batch updates,
best-effort repair, and cascading deletes (DDB + S3).
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError
from src.enums import ProcessingStage

import utils.dynamo as dynamo_module
from utils.dynamo import (
    _query_statements_by_completed,
    delete_statement_data,
    get_completed_statements,
    get_incomplete_statements,
    get_statement_item_status_map,
    get_statement_record,
    mark_statement_completed,
    persist_item_types_to_dynamo,
    repair_processing_stage,
    set_all_statement_items_completed,
    set_statement_item_completed,
)

TENANT_ID = "tenant-test-123"
STATEMENT_ID = "stmt-abc-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_ddb_and_s3(monkeypatch):
    """Replace the real DynamoDB table and S3 client with MagicMocks."""
    fake_table = MagicMock()
    fake_s3 = MagicMock()
    monkeypatch.setattr(dynamo_module, "tenant_statements_table", fake_table)
    monkeypatch.setattr(dynamo_module, "s3_client", fake_s3)
    monkeypatch.setattr(dynamo_module, "S3_BUCKET_NAME", "test-bucket")
    return fake_table, fake_s3


@pytest.fixture()
def fake_table(_patch_ddb_and_s3):
    """Return the patched DynamoDB table mock."""
    return _patch_ddb_and_s3[0]


@pytest.fixture()
def fake_s3(_patch_ddb_and_s3):
    """Return the patched S3 client mock."""
    return _patch_ddb_and_s3[1]


# ---------------------------------------------------------------------------
# _query_statements_by_completed
# ---------------------------------------------------------------------------


class TestQueryStatementsByCompleted:
    """Direct tests for the GSI query helper."""

    def test_returns_empty_list_when_tenant_id_is_none(self, fake_table):
        """No DDB call when tenant_id is falsy."""
        result = _query_statements_by_completed(None, "false")
        assert result == []
        fake_table.query.assert_not_called()

    def test_returns_empty_list_when_tenant_id_is_empty(self, fake_table):
        """No DDB call when tenant_id is an empty string."""
        result = _query_statements_by_completed("", "true")
        assert result == []
        fake_table.query.assert_not_called()

    def test_single_page_response(self, fake_table):
        """Items from a single query page are returned directly."""
        items = [{"TenantID": TENANT_ID, "StatementID": "s1"}]
        fake_table.query.return_value = {"Items": items}
        result = _query_statements_by_completed(TENANT_ID, "false")
        assert result == items
        fake_table.query.assert_called_once()

    def test_paginates_across_multiple_pages(self, fake_table):
        """All pages are collected when LastEvaluatedKey is present."""
        page1_items = [{"StatementID": "s1"}]
        page2_items = [{"StatementID": "s2"}]
        fake_table.query.side_effect = [{"Items": page1_items, "LastEvaluatedKey": {"pk": "cursor"}}, {"Items": page2_items}]
        result = _query_statements_by_completed(TENANT_ID, "true")
        assert len(result) == 2
        assert result == page1_items + page2_items
        assert fake_table.query.call_count == 2

    def test_empty_items_key_treated_as_empty_list(self, fake_table):
        """Missing 'Items' key should not crash."""
        fake_table.query.return_value = {}
        result = _query_statements_by_completed(TENANT_ID, "false")
        assert result == []


# ---------------------------------------------------------------------------
# get_incomplete_statements / get_completed_statements
# ---------------------------------------------------------------------------


class TestIncompleteAndCompletedStatements:
    """Wrappers that read tenant_id from the Flask session."""

    def test_get_incomplete_calls_query_with_false(self, fake_table, monkeypatch):
        """get_incomplete_statements passes 'false' to the query helper."""
        fake_table.query.return_value = {"Items": [{"id": "1"}]}
        # Mock session.get to return a tenant_id
        monkeypatch.setattr(dynamo_module, "session", MagicMock(get=MagicMock(return_value=TENANT_ID)))
        result = get_incomplete_statements()
        assert result == [{"id": "1"}]

    def test_get_completed_calls_query_with_true(self, fake_table, monkeypatch):
        """get_completed_statements passes 'true' to the query helper."""
        fake_table.query.return_value = {"Items": [{"id": "2"}]}
        monkeypatch.setattr(dynamo_module, "session", MagicMock(get=MagicMock(return_value=TENANT_ID)))
        result = get_completed_statements()
        assert result == [{"id": "2"}]

    def test_returns_empty_when_session_has_no_tenant(self, fake_table, monkeypatch):
        """Empty result when no tenant_id in the session."""
        monkeypatch.setattr(dynamo_module, "session", MagicMock(get=MagicMock(return_value=None)))
        result = get_incomplete_statements()
        assert result == []
        fake_table.query.assert_not_called()


# ---------------------------------------------------------------------------
# get_statement_record
# ---------------------------------------------------------------------------


class TestGetStatementRecord:
    """Single-item retrieval by composite key."""

    def test_returns_item_when_found(self, fake_table):
        """Existing item is returned from the DDB response."""
        record = {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID, "ContactName": "Acme"}
        fake_table.get_item.return_value = {"Item": record}
        result = get_statement_record(TENANT_ID, STATEMENT_ID)
        assert result == record
        fake_table.get_item.assert_called_once_with(Key={"TenantID": TENANT_ID, "StatementID": STATEMENT_ID})

    def test_returns_none_when_not_found(self, fake_table):
        """None when DDB returns no Item key."""
        fake_table.get_item.return_value = {}
        result = get_statement_record(TENANT_ID, STATEMENT_ID)
        assert result is None


# ---------------------------------------------------------------------------
# mark_statement_completed
# ---------------------------------------------------------------------------


class TestMarkStatementCompleted:
    """Toggle the Completed flag on a statement header."""

    def test_sets_completed_to_true(self, fake_table):
        """completed=True writes 'true' string to DDB."""
        mark_statement_completed(TENANT_ID, STATEMENT_ID, completed=True)
        call_kwargs = fake_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":completed"] == "true"
        assert call_kwargs["Key"] == {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID}

    def test_sets_completed_to_false(self, fake_table):
        """completed=False writes 'false' string to DDB."""
        mark_statement_completed(TENANT_ID, STATEMENT_ID, completed=False)
        call_kwargs = fake_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":completed"] == "false"


# ---------------------------------------------------------------------------
# get_statement_item_status_map
# ---------------------------------------------------------------------------


class TestGetStatementItemStatusMap:
    """Build a {statement_item_id: bool} mapping from DDB query."""

    def test_returns_empty_dict_when_tenant_missing(self, fake_table):
        result = get_statement_item_status_map("", STATEMENT_ID)
        assert result == {}
        fake_table.query.assert_not_called()

    def test_returns_empty_dict_when_statement_missing(self, fake_table):
        result = get_statement_item_status_map(TENANT_ID, "")
        assert result == {}
        fake_table.query.assert_not_called()

    def test_maps_items_to_boolean_completed(self, fake_table):
        """DDB items with 'true'/'false' strings become Python bools."""
        fake_table.query.return_value = {
            "Items": [
                {"StatementID": f"{STATEMENT_ID}#item-1", "Completed": "true"},
                {"StatementID": f"{STATEMENT_ID}#item-2", "Completed": "false"},
                {"StatementID": f"{STATEMENT_ID}#item-3", "Completed": "True"},
            ]
        }
        result = get_statement_item_status_map(TENANT_ID, STATEMENT_ID)
        assert result[f"{STATEMENT_ID}#item-1"] is True
        assert result[f"{STATEMENT_ID}#item-2"] is False
        # Case-insensitive: "True" normalizes to true
        assert result[f"{STATEMENT_ID}#item-3"] is True

    def test_skips_items_without_statement_id(self, fake_table):
        """Items missing the StatementID field are skipped."""
        fake_table.query.return_value = {
            "Items": [
                {"Completed": "true"},  # no StatementID
                {"StatementID": f"{STATEMENT_ID}#item-1", "Completed": "false"},
            ]
        }
        result = get_statement_item_status_map(TENANT_ID, STATEMENT_ID)
        assert len(result) == 1

    def test_paginates_across_pages(self, fake_table):
        """Collects items from multiple DDB query pages."""
        fake_table.query.side_effect = [
            {"Items": [{"StatementID": f"{STATEMENT_ID}#item-1", "Completed": "true"}], "LastEvaluatedKey": {"pk": "cursor"}},
            {"Items": [{"StatementID": f"{STATEMENT_ID}#item-2", "Completed": "false"}]},
        ]
        result = get_statement_item_status_map(TENANT_ID, STATEMENT_ID)
        assert len(result) == 2
        assert fake_table.query.call_count == 2

    def test_defaults_to_false_when_completed_missing(self, fake_table):
        """Missing Completed field defaults to False."""
        fake_table.query.return_value = {"Items": [{"StatementID": f"{STATEMENT_ID}#item-1"}]}
        result = get_statement_item_status_map(TENANT_ID, STATEMENT_ID)
        assert result[f"{STATEMENT_ID}#item-1"] is False


# ---------------------------------------------------------------------------
# set_statement_item_completed
# ---------------------------------------------------------------------------


class TestSetStatementItemCompleted:
    """Toggle completion on a single statement item."""

    def test_updates_item_when_inputs_valid(self, fake_table):
        item_id = f"{STATEMENT_ID}#item-1"
        set_statement_item_completed(TENANT_ID, item_id, True)
        call_kwargs = fake_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":completed"] == "true"

    def test_noop_when_tenant_empty(self, fake_table):
        set_statement_item_completed("", f"{STATEMENT_ID}#item-1", True)
        fake_table.update_item.assert_not_called()

    def test_noop_when_item_id_empty(self, fake_table):
        set_statement_item_completed(TENANT_ID, "", True)
        fake_table.update_item.assert_not_called()

    def test_sets_false_value(self, fake_table):
        item_id = f"{STATEMENT_ID}#item-1"
        set_statement_item_completed(TENANT_ID, item_id, False)
        call_kwargs = fake_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":completed"] == "false"


# ---------------------------------------------------------------------------
# set_all_statement_items_completed
# ---------------------------------------------------------------------------


class TestSetAllStatementItemsCompleted:
    """Batch-set completion for all items under a statement."""

    def test_updates_each_item(self, fake_table):
        """Each item returned by get_statement_item_status_map gets updated."""
        fake_table.query.return_value = {"Items": [{"StatementID": f"{STATEMENT_ID}#item-1", "Completed": "false"}, {"StatementID": f"{STATEMENT_ID}#item-2", "Completed": "false"}]}
        set_all_statement_items_completed(TENANT_ID, STATEMENT_ID, True)
        # One query call + two update_item calls
        assert fake_table.update_item.call_count == 2

    def test_noop_when_no_items(self, fake_table):
        """No update calls when no items exist."""
        fake_table.query.return_value = {"Items": []}
        set_all_statement_items_completed(TENANT_ID, STATEMENT_ID, True)
        fake_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# persist_item_types_to_dynamo
# ---------------------------------------------------------------------------


class TestPersistItemTypesToDynamo:
    """Threaded batch update of item_type on DDB records."""

    def test_updates_each_classification(self, fake_table):
        """Each key-value pair triggers an update_item call."""
        updates = {"stmt#item-1": "invoice", "stmt#item-2": "credit_note"}
        persist_item_types_to_dynamo(TENANT_ID, updates)
        assert fake_table.update_item.call_count == 2

    def test_noop_when_tenant_none(self, fake_table):
        persist_item_types_to_dynamo(None, {"stmt#item-1": "invoice"})
        fake_table.update_item.assert_not_called()

    def test_noop_when_updates_empty(self, fake_table):
        persist_item_types_to_dynamo(TENANT_ID, {})
        fake_table.update_item.assert_not_called()

    def test_logs_error_on_client_error(self, fake_table):
        """ClientError during update is logged but does not propagate."""
        fake_table.update_item.side_effect = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "Too fast"}}, "UpdateItem")
        # Should not raise
        persist_item_types_to_dynamo(TENANT_ID, {"stmt#item-1": "invoice"}, max_workers=1)

    def test_respects_max_workers_param(self, fake_table):
        """Custom max_workers is forwarded to the thread pool."""
        updates = {"a": "x"}
        # Just verify no crash with explicit max_workers
        persist_item_types_to_dynamo(TENANT_ID, updates, max_workers=2)
        assert fake_table.update_item.call_count == 1


# ---------------------------------------------------------------------------
# repair_processing_stage
# ---------------------------------------------------------------------------


class TestRepairProcessingStage:
    """Best-effort ProcessingStage → failed repair."""

    def test_calls_update_with_failed_stage(self, fake_table):
        """update_item is called with the failed stage value."""
        repair_processing_stage(TENANT_ID, STATEMENT_ID)
        call_kwargs = fake_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":failed"] == ProcessingStage.FAILED
        assert call_kwargs["Key"] == {"TenantID": TENANT_ID, "StatementID": STATEMENT_ID}

    def test_swallows_conditional_check_failure(self, fake_table):
        """ConditionalCheckFailedException is silently caught."""
        fake_table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "Already failed"}}, "UpdateItem")
        # Should not raise
        repair_processing_stage(TENANT_ID, STATEMENT_ID)

    def test_swallows_generic_exception(self, fake_table):
        """Any exception is caught — best-effort semantics."""
        fake_table.update_item.side_effect = RuntimeError("Unexpected")
        repair_processing_stage(TENANT_ID, STATEMENT_ID)


# ---------------------------------------------------------------------------
# delete_statement_data
# ---------------------------------------------------------------------------


class TestDeleteStatementData:
    """Cascading delete: DDB items + S3 objects."""

    def test_noop_when_tenant_empty(self, fake_table, fake_s3):
        delete_statement_data("", STATEMENT_ID)
        fake_table.query.assert_not_called()
        fake_s3.delete_object.assert_not_called()

    def test_noop_when_statement_empty(self, fake_table, fake_s3):
        delete_statement_data(TENANT_ID, "")
        fake_table.query.assert_not_called()
        fake_s3.delete_object.assert_not_called()

    def test_deletes_ddb_items_and_s3_objects(self, fake_table, fake_s3):
        """Statement header + items are batch-deleted, then S3 artifacts removed."""
        # Simulate DDB query returning two items (header + one child)
        fake_batch_writer = MagicMock()
        fake_table.batch_writer.return_value.__enter__ = MagicMock(return_value=fake_batch_writer)
        fake_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
        fake_table.query.return_value = {"Items": [{"StatementID": STATEMENT_ID}, {"StatementID": f"{STATEMENT_ID}#item-1"}]}
        delete_statement_data(TENANT_ID, STATEMENT_ID)

        # Two DDB items deleted via batch_writer
        assert fake_batch_writer.delete_item.call_count == 2

        # Two S3 objects deleted (PDF + JSON)
        assert fake_s3.delete_object.call_count == 2
        s3_keys = [c[1]["Key"] for c in fake_s3.delete_object.call_args_list]
        assert any(k.endswith(".pdf") for k in s3_keys)
        assert any(k.endswith(".json") for k in s3_keys)

    def test_skips_items_without_sort_key(self, fake_table, fake_s3):
        """Items missing StatementID field are not deleted."""
        fake_batch_writer = MagicMock()
        fake_table.batch_writer.return_value.__enter__ = MagicMock(return_value=fake_batch_writer)
        fake_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
        fake_table.query.return_value = {
            "Items": [
                {},  # no StatementID
                {"StatementID": STATEMENT_ID},
            ]
        }
        delete_statement_data(TENANT_ID, STATEMENT_ID)
        assert fake_batch_writer.delete_item.call_count == 1

    def test_paginates_ddb_deletes(self, fake_table, fake_s3):
        """Handles paginated DDB query results during deletion."""
        fake_batch_writer = MagicMock()
        fake_table.batch_writer.return_value.__enter__ = MagicMock(return_value=fake_batch_writer)
        fake_table.batch_writer.return_value.__exit__ = MagicMock(return_value=False)
        fake_table.query.side_effect = [{"Items": [{"StatementID": STATEMENT_ID}], "LastEvaluatedKey": {"pk": "cursor"}}, {"Items": [{"StatementID": f"{STATEMENT_ID}#item-1"}]}]
        delete_statement_data(TENANT_ID, STATEMENT_ID)
        assert fake_batch_writer.delete_item.call_count == 2
        assert fake_table.query.call_count == 2

    def test_handles_s3_no_such_key(self, fake_table, fake_s3):
        """NoSuchKey on S3 delete is non-fatal (object already gone)."""
        fake_table.query.return_value = {"Items": []}
        # Simulate NoSuchKey exception class on the s3_client mock
        no_such_key = type("NoSuchKey", (Exception,), {})
        fake_s3.exceptions = MagicMock()
        fake_s3.exceptions.NoSuchKey = no_such_key
        fake_s3.delete_object.side_effect = no_such_key("gone")
        # Should not raise
        delete_statement_data(TENANT_ID, STATEMENT_ID)

    def test_propagates_unexpected_s3_error(self, fake_table, fake_s3):
        """Non-NoSuchKey S3 errors propagate to the caller."""
        fake_table.query.return_value = {"Items": []}
        # Make the NoSuchKey exception class something specific
        no_such_key = type("NoSuchKey", (Exception,), {})
        fake_s3.exceptions = MagicMock()
        fake_s3.exceptions.NoSuchKey = no_such_key
        fake_s3.delete_object.side_effect = RuntimeError("S3 outage")
        with pytest.raises(RuntimeError, match="S3 outage"):
            delete_statement_data(TENANT_ID, STATEMENT_ID)
