"""Additional coverage tests for main.py.

Covers uncovered paths: validation errors, _release_reserved_tokens,
_consume_reserved_tokens, token-release failure message suffix,
and the non-dict/missing statement_items branch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.billing import BillingSettlementError
from core.statement_processor import ExtractionOutput
from main import _consume_reserved_tokens, _release_reserved_tokens, lambda_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_event(**overrides: object) -> dict[str, object]:
    """Return a valid extraction event with optional field overrides."""
    base = {"statementId": "stmt-1", "tenantId": "t1", "contactId": "c1", "pdfKey": "t1/statements/stmt-1.pdf", "jsonKey": "t1/statements/stmt-1.json", "pageCount": 1}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------
class TestLambdaHandlerValidation:
    """Validation error path (missing required fields)."""

    def test_missing_fields_returns_error(self) -> None:
        result = lambda_handler({}, None)
        assert result["status"] == "error"
        assert "Invalid event payload" in result["message"]

    def test_partial_event_returns_validation_errors(self) -> None:
        result = lambda_handler({"statementId": "s1"}, None)
        assert result["status"] == "error"
        assert isinstance(result["errors"], list)
        assert len(result["errors"]) > 0


# ---------------------------------------------------------------------------
# _release_reserved_tokens
# ---------------------------------------------------------------------------
class TestReleaseReservedTokens:
    """_release_reserved_tokens: wrapper around BillingSettlementService."""

    def test_returns_true_on_success(self, monkeypatch) -> None:
        monkeypatch.setattr("main.BillingSettlementService.release_statement_reservation", lambda *a, **kw: True)
        assert _release_reserved_tokens("t1", "s1", source="test") is True

    def test_returns_false_when_billing_error(self, monkeypatch) -> None:
        def _raise(*a, **kw):
            raise BillingSettlementError("boom")

        monkeypatch.setattr("main.BillingSettlementService.release_statement_reservation", _raise)
        assert _release_reserved_tokens("t1", "s1", source="test") is False


# ---------------------------------------------------------------------------
# _consume_reserved_tokens
# ---------------------------------------------------------------------------
class TestConsumeReservedTokens:
    """_consume_reserved_tokens: wrapper around BillingSettlementService."""

    def test_returns_true_on_success(self, monkeypatch) -> None:
        monkeypatch.setattr("main.BillingSettlementService.consume_statement_reservation", lambda *a, **kw: True)
        assert _consume_reserved_tokens("t1", "s1") is True

    def test_returns_false_when_billing_error(self, monkeypatch) -> None:
        def _raise(*a, **kw):
            raise BillingSettlementError("boom")

        monkeypatch.setattr("main.BillingSettlementService.consume_statement_reservation", _raise)
        assert _consume_reserved_tokens("t1", "s1") is False


# ---------------------------------------------------------------------------
# lambda_handler — token release failure appends message suffix
# ---------------------------------------------------------------------------
class TestTokenReleaseFailureMessage:
    """When extraction fails AND token release fails, message has operator suffix."""

    def test_release_failure_appends_operator_notice(self, monkeypatch) -> None:
        monkeypatch.setattr("main.run_extraction", lambda **kw: (_ for _ in ()).throw(RuntimeError("extraction died")))
        monkeypatch.setattr("main.update_processing_stage", lambda *a, **kw: None)
        monkeypatch.setattr("main._release_reserved_tokens", lambda *a, **kw: False)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "error"
        assert "operator attention" in result["message"]

    def test_release_success_no_operator_notice(self, monkeypatch) -> None:
        monkeypatch.setattr("main.run_extraction", lambda **kw: (_ for _ in ()).throw(RuntimeError("extraction died")))
        monkeypatch.setattr("main.update_processing_stage", lambda *a, **kw: None)
        monkeypatch.setattr("main._release_reserved_tokens", lambda *a, **kw: True)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "error"
        assert "operator attention" not in result["message"]


# ---------------------------------------------------------------------------
# lambda_handler — consume failure path
# ---------------------------------------------------------------------------
class TestConsumeFailurePath:
    """When extraction succeeds but billing consume fails."""

    def test_consume_failure_returns_error(self, monkeypatch) -> None:
        monkeypatch.setattr("main.run_extraction", lambda **kw: ExtractionOutput(filename="stmt.json", statement={"statement_items": [{"n": 1}]}))
        monkeypatch.setattr("main._consume_reserved_tokens", lambda *a, **kw: False)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "error"
        assert "billing settlement failed" in result["message"]


# ---------------------------------------------------------------------------
# lambda_handler — empty/partial statement dict
# ---------------------------------------------------------------------------
class TestEdgeCaseResults:
    """Cover empty statement payload and missing statement_items."""

    def test_empty_statement_payload(self, monkeypatch) -> None:
        """When statement dict is empty, item_count defaults to 0."""
        monkeypatch.setattr("main.run_extraction", lambda **kw: ExtractionOutput(filename="stmt.json", statement={}))
        monkeypatch.setattr("main._consume_reserved_tokens", lambda *a, **kw: True)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "ok"
        assert result["itemCount"] == 0
        assert result["earliestItemDate"] is None
        assert result["latestItemDate"] is None

    def test_none_statement_items(self, monkeypatch) -> None:
        """When statement_items key is absent, item_count defaults to 0."""
        monkeypatch.setattr("main.run_extraction", lambda **kw: ExtractionOutput(filename="stmt.json", statement={}))
        monkeypatch.setattr("main._consume_reserved_tokens", lambda *a, **kw: True)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "ok"
        assert result["itemCount"] == 0

    def test_pdf_bucket_override(self, monkeypatch) -> None:
        """When pdfBucket is provided, it is used instead of S3_BUCKET_NAME."""
        captured = {}

        def _capture_extraction(**kw):
            captured.update(kw)
            return ExtractionOutput(filename="stmt.json", statement={"statement_items": []})

        monkeypatch.setattr("main.run_extraction", _capture_extraction)
        monkeypatch.setattr("main._consume_reserved_tokens", lambda *a, **kw: True)

        lambda_handler(_valid_event(pdfBucket="custom-bucket"), None)
        assert captured["bucket"] == "custom-bucket"

    def test_successful_response_fields(self, monkeypatch) -> None:
        """Verify all fields in a successful response."""
        monkeypatch.setattr(
            "main.run_extraction",
            lambda **kw: ExtractionOutput(filename="stmt-1.json", statement={"statement_items": [{"n": 1}, {"n": 2}], "earliest_item_date": "2024-01-01", "latest_item_date": "2024-06-30"}),
        )
        monkeypatch.setattr("main._consume_reserved_tokens", lambda *a, **kw: True)

        result = lambda_handler(_valid_event(), None)
        assert result["status"] == "ok"
        assert result["statementId"] == "stmt-1"
        assert result["tenantId"] == "t1"
        assert result["contactId"] == "c1"
        assert result["jsonKey"] == "t1/statements/stmt-1.json"
        assert result["filename"] == "stmt-1.json"
        assert result["itemCount"] == 2
        assert result["earliestItemDate"] == "2024-01-01"
        assert result["latestItemDate"] == "2024-06-30"
