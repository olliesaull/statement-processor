"""Unit tests for server-side PDF page counting."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage

from utils.pdf_page_count import PDFPageCountError, count_pdf_pages


def _build_test_pdf(page_count: int) -> bytes:
    """Build a valid in-memory PDF with the requested number of blank pages."""
    buffer = BytesIO()
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    return buffer.getvalue()


def test_count_pdf_pages_counts_pages_from_a_real_pdf() -> None:
    """A valid parsed PDF should return its exact page count."""
    uploaded_file = FileStorage(stream=BytesIO(_build_test_pdf(3)), filename="statement.pdf", content_type="application/pdf")

    assert count_pdf_pages(uploaded_file) == 3


def test_count_pdf_pages_rewinds_the_upload_stream() -> None:
    """Counting should not consume the file stream needed by later upload steps."""
    pdf_bytes = _build_test_pdf(1)
    uploaded_file = FileStorage(stream=BytesIO(pdf_bytes), filename="statement.pdf", content_type="application/pdf")

    assert count_pdf_pages(uploaded_file) == 1
    assert uploaded_file.stream.read() == pdf_bytes


def test_count_pdf_pages_rejects_invalid_pdf_payloads() -> None:
    """Invalid PDFs should fail fast instead of being heuristically counted."""
    with pytest.raises(PDFPageCountError):
        count_pdf_pages(FileStorage(stream=BytesIO(b"not a pdf"), filename="statement.pdf", content_type="application/pdf"))
