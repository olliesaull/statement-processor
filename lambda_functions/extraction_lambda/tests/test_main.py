"""Unit tests for extraction Lambda billing settlement flow."""

from __future__ import annotations

from main import lambda_handler


def test_lambda_handler_consumes_tokens_after_success(monkeypatch) -> None:
    """Successful processing should consume the earlier reservation."""

    monkeypatch.setattr(
        "main.run_extraction",
        lambda **kwargs: {"filename": "statement.json", "statement": {"statement_items": [{"number": "INV-1"}], "earliest_item_date": "2025-01-01", "latest_item_date": "2025-01-31"}},
    )
    monkeypatch.setattr("main._consume_reserved_tokens", lambda tenant_id, statement_id: True)

    result = lambda_handler(
        {
            "statementId": "statement-1",
            "tenantId": "tenant-1",
            "contactId": "contact-1",
            "pdfKey": "tenant-1/statements/statement-1.pdf",
            "jsonKey": "tenant-1/statements/statement-1.json",
            "pageCount": 1,
        },
        None,
    )

    assert result["status"] == "ok"
    assert result["itemCount"] == 1


def test_lambda_handler_releases_tokens_when_processing_raises(monkeypatch) -> None:
    """Processing errors should release reserved tokens before returning an error."""

    def _boom(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("processing blew up")

    monkeypatch.setattr("main.run_extraction", _boom)
    monkeypatch.setattr("main._release_reserved_tokens", lambda tenant_id, statement_id, source: True)

    result = lambda_handler(
        {
            "statementId": "statement-1",
            "tenantId": "tenant-1",
            "contactId": "contact-1",
            "pdfKey": "tenant-1/statements/statement-1.pdf",
            "jsonKey": "tenant-1/statements/statement-1.json",
            "pageCount": 1,
        },
        None,
    )

    assert result["status"] == "error"
    assert "processing blew up" in result["message"]
