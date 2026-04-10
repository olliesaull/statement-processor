"""Coverage tests for utils/statement_upload_validation.py.

Covers branches not exercised by the existing test_statement_upload_validation.py:
- UploadPageCountResult.to_response_payload (with and without error)
- StatementUploadPreflightResult.to_response_payload
- count_uploaded_pdf_pages (non-PDF rejection, PDFPageCountError)
- prepare_statement_uploads (contact_id not found in lookup)
"""

from io import BytesIO
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

import utils.statement_upload_validation as validation_mod
from utils.pdf_page_count import PDFPageCountError
from utils.statement_upload_validation import StatementUploadPreflightResult, UploadPageCountResult, count_uploaded_pdf_pages, prepare_statement_uploads


def _make_upload(filename: str = "statement.pdf", content_type: str = "application/pdf", content: bytes = b"placeholder") -> FileStorage:
    """Build a minimal uploaded-file test double."""
    return FileStorage(stream=BytesIO(content), filename=filename, content_type=content_type)


# ---------------------------------------------------------------------------
# UploadPageCountResult.to_response_payload
# ---------------------------------------------------------------------------


class TestUploadPageCountResultPayload:
    """Tests for UploadPageCountResult.to_response_payload serialization."""

    def test_success_payload_omits_error(self) -> None:
        """Successful result should include filename and page_count but no error key."""
        result = UploadPageCountResult(filename="test.pdf", page_count=3)
        payload = result.to_response_payload()
        assert payload == {"filename": "test.pdf", "page_count": 3}
        assert "error" not in payload

    def test_error_payload_includes_error(self) -> None:
        """Failed result should include error key in the payload."""
        result = UploadPageCountResult(filename="bad.pdf", error="Only PDF statements are supported.")
        payload = result.to_response_payload()
        assert payload["filename"] == "bad.pdf"
        assert payload["page_count"] is None
        assert payload["error"] == "Only PDF statements are supported."


# ---------------------------------------------------------------------------
# StatementUploadPreflightResult.to_response_payload
# ---------------------------------------------------------------------------


class TestStatementUploadPreflightResultPayload:
    """Tests for StatementUploadPreflightResult.to_response_payload serialization."""

    def test_full_serialization(self) -> None:
        """All fields should appear in the serialized payload."""
        files = [UploadPageCountResult(filename="a.pdf", page_count=2), UploadPageCountResult(filename="b.pdf", error="Bad file")]
        preflight = StatementUploadPreflightResult(files=files, total_pages=2, available_tokens=10, is_sufficient=True, can_submit=False, shortfall=0)
        payload = preflight.to_response_payload()
        assert payload["total_pages"] == 2
        assert payload["available_tokens"] == 10
        assert payload["is_sufficient"] is True
        assert payload["can_submit"] is False
        assert payload["shortfall"] == 0
        assert payload["has_errors"] is True
        assert len(payload["files"]) == 2
        # First file has no error, second does
        assert "error" not in payload["files"][0]
        assert payload["files"][1]["error"] == "Bad file"

    def test_no_errors_flag(self) -> None:
        """has_errors should be False when all files succeed."""
        files = [UploadPageCountResult(filename="ok.pdf", page_count=5)]
        preflight = StatementUploadPreflightResult(files=files, total_pages=5, available_tokens=10, is_sufficient=True, can_submit=True, shortfall=0)
        payload = preflight.to_response_payload()
        assert payload["has_errors"] is False


# ---------------------------------------------------------------------------
# count_uploaded_pdf_pages
# ---------------------------------------------------------------------------


class TestCountUploadedPdfPages:
    """Tests for count_uploaded_pdf_pages — non-PDF rejection and PDFPageCountError."""

    def test_rejects_non_pdf_file(self, monkeypatch) -> None:
        """Non-PDF uploads should return an error result immediately."""
        monkeypatch.setattr(validation_mod, "is_allowed_pdf", lambda filename, mimetype, stream=None: False)
        upload = _make_upload(filename="notes.txt", content_type="text/plain")
        result = count_uploaded_pdf_pages("tenant-1", upload)
        assert result.error == "Only PDF statements are supported."
        assert result.page_count is None

    def test_returns_page_count_on_success(self, monkeypatch) -> None:
        """Valid PDF should return page_count without an error."""
        monkeypatch.setattr(validation_mod, "is_allowed_pdf", lambda filename, mimetype, stream=None: True)
        monkeypatch.setattr(validation_mod, "count_pdf_pages", lambda f: 7)
        upload = _make_upload()
        result = count_uploaded_pdf_pages("tenant-1", upload)
        assert result.page_count == 7
        assert result.error is None

    def test_handles_pdf_page_count_error(self, monkeypatch) -> None:
        """PDFPageCountError should be caught and returned as a user-facing error."""
        monkeypatch.setattr(validation_mod, "is_allowed_pdf", lambda filename, mimetype, stream=None: True)

        def _raise_error(f):
            raise PDFPageCountError("corrupt PDF")

        monkeypatch.setattr(validation_mod, "count_pdf_pages", _raise_error)
        upload = _make_upload()
        result = count_uploaded_pdf_pages("tenant-1", upload)
        assert result.error == "Unable to determine page count for this PDF."
        assert result.page_count is None

    def test_unnamed_file_uses_default_filename(self, monkeypatch) -> None:
        """Upload with no filename should default to 'Unnamed PDF'."""
        monkeypatch.setattr(validation_mod, "is_allowed_pdf", lambda filename, mimetype, stream=None: True)
        monkeypatch.setattr(validation_mod, "count_pdf_pages", lambda f: 1)
        upload = FileStorage(stream=BytesIO(b"data"), filename=None, content_type="application/pdf")
        result = count_uploaded_pdf_pages("tenant-1", upload)
        assert result.filename == "Unnamed PDF"
        assert result.page_count == 1


# ---------------------------------------------------------------------------
# prepare_statement_uploads — contact_id not found
# ---------------------------------------------------------------------------


class TestPrepareStatementUploadsContactNotFound:
    """Tests for prepare_statement_uploads when contact_id is missing from lookup."""

    def test_contact_not_in_lookup_produces_error(self, monkeypatch) -> None:
        """Contact name not in lookup should add a user-facing error message."""
        monkeypatch.setattr(validation_mod, "count_uploaded_pdf_pages", lambda tid, f: UploadPageCountResult(filename=f.filename or "test.pdf", page_count=2))
        error_messages: list[str] = []
        uploads = prepare_statement_uploads(
            "tenant-1",
            [_make_upload("good.pdf")],
            ["Unknown Co"],
            {},  # empty lookup — contact will not be found
            error_messages,
        )
        assert len(uploads) == 0
        assert len(error_messages) == 1
        assert "Unknown Co" in error_messages[0]
        assert "not recognised" in error_messages[0]

    def test_mix_of_found_and_not_found_contacts(self, monkeypatch) -> None:
        """Only rows with recognised contacts should proceed."""
        monkeypatch.setattr(validation_mod, "count_uploaded_pdf_pages", lambda tid, f: UploadPageCountResult(filename=f.filename or "test.pdf", page_count=1))
        error_messages: list[str] = []
        uploads = prepare_statement_uploads("tenant-1", [_make_upload("a.pdf"), _make_upload("b.pdf")], ["Known Co", "Mystery Inc"], {"Known Co": "contact-1"}, error_messages)
        assert len(uploads) == 1
        assert uploads[0].contact_id == "contact-1"
        assert len(error_messages) == 1
        assert "Mystery Inc" in error_messages[0]
