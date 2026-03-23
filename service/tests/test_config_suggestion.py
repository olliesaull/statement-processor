"""Tests for config suggestion orchestrator."""

import json
from io import BytesIO

from pypdf import PdfWriter

import core.config_suggestion as config_suggestion_module
from core.config_suggestion import suggest_config_for_statement


def _make_single_page_pdf_bytes() -> bytes:
    """Create a minimal single-page PDF for test mocks."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_textract_response(headers: list[str], rows: list[list[str]]) -> dict:
    """Build a minimal Textract AnalyzeDocument response with one table."""
    # Build cells: row 1 = headers, subsequent rows = data
    cells = []
    for col_idx, header in enumerate(headers, 1):
        cells.append({"BlockType": "CELL", "RowIndex": 1, "ColumnIndex": col_idx, "Text": header})
    for row_idx, row in enumerate(rows, 2):
        for col_idx, value in enumerate(row, 1):
            cells.append({"BlockType": "CELL", "RowIndex": row_idx, "ColumnIndex": col_idx, "Text": value})
    return {"Blocks": [{"BlockType": "TABLE"}] + cells}


def test_suggest_config_happy_path(monkeypatch) -> None:
    """Full flow: Textract -> Bedrock -> S3 -> DynamoDB status update."""
    headers = ["Date", "Invoice No", "Amount"]
    rows = [["15/03/2025", "INV-001", "1,234.56"]]
    textract_response = _make_textract_response(headers, rows)

    # Mock Textract
    class FakeTextract:
        def analyze_document(self, **kwargs):
            assert kwargs["FeatureTypes"] == ["TABLES"]
            return textract_response

    # Mock Bedrock
    suggested = {"number": "Invoice No", "date": "Date", "due_date": "", "total": ["Amount"], "date_format": "DD/MM/YYYY", "decimal_separator": ".", "thousands_separator": ","}
    monkeypatch.setattr(config_suggestion_module, "suggest_column_mapping", lambda headers, rows: (suggested, "High confidence"))

    # Mock date disambiguation
    monkeypatch.setattr(config_suggestion_module, "disambiguate_date_format", lambda dates, fmt: fmt)

    # Mock S3 — get_object returns a minimal PDF so _extract_page_one can parse it.
    s3_puts = []
    fake_pdf = _make_single_page_pdf_bytes()

    class FakeS3:
        def get_object(self, **kwargs):
            return {"Body": BytesIO(fake_pdf)}

        def put_object(self, **kwargs):
            s3_puts.append(kwargs)

    # Mock DynamoDB
    ddb_updates = []

    class FakeTable:
        def update_item(self, **kwargs):
            ddb_updates.append(kwargs)

    monkeypatch.setattr(config_suggestion_module, "textract_client", FakeTextract())
    monkeypatch.setattr(config_suggestion_module, "s3_client", FakeS3())
    monkeypatch.setattr(config_suggestion_module, "S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(config_suggestion_module, "tenant_statements_table", FakeTable())

    suggest_config_for_statement(tenant_id="t1", contact_id="c1", contact_name="Acme Ltd", statement_id="s1", pdf_s3_key="t1/statements/s1.pdf", filename="invoice.pdf")

    # Verify S3 suggestion saved
    assert len(s3_puts) == 1
    assert "config-suggestions" in s3_puts[0]["Key"]
    body = json.loads(s3_puts[0]["Body"])
    assert body["suggested_config"]["number"] == "Invoice No"
    assert body["detected_headers"] == headers
    assert body["confidence_notes"] == "High confidence"

    # Verify DynamoDB status update
    assert len(ddb_updates) == 1
    assert "pending_config_review" in str(ddb_updates[0])


def test_suggest_config_textract_failure_sets_failed_status(monkeypatch) -> None:
    """When Textract fails after retries, status should be config_suggestion_failed."""
    fake_pdf = _make_single_page_pdf_bytes()

    class FakeS3:
        def get_object(self, **kwargs):
            return {"Body": BytesIO(fake_pdf)}

    class FakeTextract:
        def analyze_document(self, **kwargs):
            raise Exception("Textract error")

    ddb_updates = []

    class FakeTable:
        def update_item(self, **kwargs):
            ddb_updates.append(kwargs)

    monkeypatch.setattr(config_suggestion_module, "s3_client", FakeS3())
    monkeypatch.setattr(config_suggestion_module, "S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(config_suggestion_module, "textract_client", FakeTextract())
    monkeypatch.setattr(config_suggestion_module, "tenant_statements_table", FakeTable())

    suggest_config_for_statement(tenant_id="t1", contact_id="c1", contact_name="Acme Ltd", statement_id="s1", pdf_s3_key="t1/statements/s1.pdf", filename="invoice.pdf")

    assert len(ddb_updates) == 1
    assert "config_suggestion_failed" in str(ddb_updates[0])
