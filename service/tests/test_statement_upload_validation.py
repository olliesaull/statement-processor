"""Unit tests for statement upload validation helpers."""

from io import BytesIO

from werkzeug.datastructures import FileStorage

import utils.statement_upload_validation as statement_upload_validation
from tenant_data_repository import TenantDataRepository
from utils.statement_upload_validation import (
    PreparedStatementUpload,
    UploadPageCountResult,
    build_statement_upload_preflight,
    build_upload_token_sufficiency,
    prepare_statement_uploads,
    validate_upload_payload,
)


def _make_upload(filename: str = "statement.pdf", content_type: str = "application/pdf") -> FileStorage:
    """Build a minimal uploaded-file test double."""
    return FileStorage(stream=BytesIO(b"placeholder"), filename=filename, content_type=content_type)


def test_validate_upload_payload_rejects_missing_or_mismatched_rows() -> None:
    """Uploads need at least one file and the same number of contacts."""
    assert not validate_upload_payload([], [])
    assert not validate_upload_payload([_make_upload()], [])
    assert validate_upload_payload([_make_upload()], ["Acme Ltd"])


def test_build_statement_upload_preflight_uses_server_counts_and_token_balance(monkeypatch) -> None:
    """Preflight results should reflect authoritative server counts and balance."""

    def _fake_count(tenant_id: str | None, uploaded_file: FileStorage) -> UploadPageCountResult:
        page_count = 3 if uploaded_file.filename == "one.pdf" else 4
        return UploadPageCountResult(filename=uploaded_file.filename or "statement.pdf", page_count=page_count)

    monkeypatch.setattr(statement_upload_validation, "count_uploaded_pdf_pages", _fake_count)
    monkeypatch.setattr(TenantDataRepository, "get_tenant_token_balance", classmethod(lambda cls, tenant_id: 5))

    result = build_statement_upload_preflight("tenant-1", [_make_upload("one.pdf"), _make_upload("two.pdf")])

    assert result.total_pages == 7
    assert result.available_tokens == 5
    assert not result.is_sufficient
    assert not result.can_submit
    assert result.shortfall == 2


def test_prepare_statement_uploads_returns_valid_rows_and_collects_errors(monkeypatch) -> None:
    """Preparation should keep valid rows and surface user-facing validation errors."""

    def _fake_count(tenant_id: str | None, uploaded_file: FileStorage) -> UploadPageCountResult:
        if uploaded_file.filename == "bad.pdf":
            return UploadPageCountResult(filename="bad.pdf", error="Unable to determine page count for this PDF.")
        return UploadPageCountResult(filename=uploaded_file.filename or "statement.pdf", page_count=2)

    monkeypatch.setattr(statement_upload_validation, "count_uploaded_pdf_pages", _fake_count)
    monkeypatch.setattr(statement_upload_validation, "get_contact_config", lambda tenant_id, contact_id: {"ContactID": contact_id})

    error_messages: list[str] = []
    prepared_uploads = prepare_statement_uploads(
        "tenant-1", [_make_upload("good.pdf"), _make_upload("bad.pdf"), _make_upload("missing-contact.pdf")], ["Acme Ltd", "Acme Ltd", ""], {"Acme Ltd": "contact-1"}, error_messages
    )

    assert len(prepared_uploads) == 1
    assert prepared_uploads[0].contact_id == "contact-1"
    assert prepared_uploads[0].contact_name == "Acme Ltd"
    assert prepared_uploads[0].page_count == 2
    assert error_messages == ["bad.pdf: Unable to determine page count for this PDF.", "Please select a contact for 'missing-contact.pdf'."]


def test_build_upload_token_sufficiency_sums_prepared_uploads(monkeypatch) -> None:
    """Token sufficiency should total all prepared upload page counts."""
    monkeypatch.setattr(TenantDataRepository, "get_tenant_token_balance", classmethod(lambda cls, tenant_id: 6))

    prepared_uploads = [
        PreparedStatementUpload(uploaded_file=_make_upload("one.pdf"), contact_id="contact-1", contact_name="Acme Ltd", page_count=2),
        PreparedStatementUpload(uploaded_file=_make_upload("two.pdf"), contact_id="contact-2", contact_name="Beta Ltd", page_count=5),
    ]

    result = build_upload_token_sufficiency("tenant-1", prepared_uploads)

    assert result.total_pages == 7
    assert result.available_tokens == 6
    assert result.shortfall == 1
    assert not result.is_sufficient
