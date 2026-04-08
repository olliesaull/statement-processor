"""Tests for processing progress DynamoDB updates."""

import sys
import types

# Stub config before importing the module under test.
fake_config = types.ModuleType("config")
fake_config.tenant_statements_table = None
sys.modules.setdefault("config", fake_config)

from unittest.mock import MagicMock, patch

import pytest

from core.processing_progress import update_processing_stage


@pytest.fixture()
def mock_table():
    table = MagicMock()
    with patch("core.processing_progress.tenant_statements_table", table):
        yield table


class TestUpdateProcessingStage:
    """Verify DynamoDB update_item calls for each stage transition."""

    def test_sets_stage_only(self, mock_table):
        """Stage-only update (e.g. chunking) sets ProcessingStage, removes progress fields."""
        update_processing_stage("tenant-1", "stmt-1", "chunking")

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"] == {"TenantID": "tenant-1", "StatementID": "stmt-1"}
        assert ":stage" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":stage"] == "chunking"
        # progress and total_sections should be removed
        assert "REMOVE" in call_kwargs["UpdateExpression"]

    def test_sets_stage_with_progress_and_total(self, mock_table):
        """Extracting stage sets all three fields."""
        update_processing_stage("tenant-1", "stmt-1", "extracting", progress="0/5", total_sections=5)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":stage"] == "extracting"
        assert call_kwargs["ExpressionAttributeValues"][":progress"] == "0/5"
        assert call_kwargs["ExpressionAttributeValues"][":total_sections"] == 5
        assert "REMOVE" not in call_kwargs["UpdateExpression"]

    def test_sets_stage_with_progress_only(self, mock_table):
        """Progress update without total_sections removes total_sections."""
        update_processing_stage("tenant-1", "stmt-1", "extracting", progress="3/5")

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":progress"] == "3/5"
        assert "ProcessingTotalSections" in call_kwargs["UpdateExpression"]

    def test_post_processing_removes_progress_fields(self, mock_table):
        """Post-processing stage removes both progress and total_sections."""
        update_processing_stage("tenant-1", "stmt-1", "post_processing")

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":stage"] == "post_processing"
        assert "REMOVE" in call_kwargs["UpdateExpression"]
        assert "ProcessingProgress" in call_kwargs["UpdateExpression"]
        assert "ProcessingTotalSections" in call_kwargs["UpdateExpression"]

    def test_complete_removes_progress_fields(self, mock_table):
        """Complete stage removes both progress and total_sections."""
        update_processing_stage("tenant-1", "stmt-1", "complete")

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":stage"] == "complete"
        assert "REMOVE" in call_kwargs["UpdateExpression"]

    def test_failed_removes_progress_fields(self, mock_table):
        """Failed stage removes both progress and total_sections."""
        update_processing_stage("tenant-1", "stmt-1", "failed")

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":stage"] == "failed"
        assert "REMOVE" in call_kwargs["UpdateExpression"]

    def test_dynamo_error_is_swallowed(self, mock_table):
        """DynamoDB errors are logged but do not raise."""
        mock_table.update_item.side_effect = Exception("DynamoDB unavailable")

        # Should not raise
        update_processing_stage("tenant-1", "stmt-1", "extracting", progress="2/5")

    def test_none_table_is_handled(self):
        """If tenant_statements_table is None (e.g. test config), no-op."""
        with patch("core.processing_progress.tenant_statements_table", None):
            # Should not raise
            update_processing_stage("tenant-1", "stmt-1", "chunking")
