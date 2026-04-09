"""Test helpers for the extraction Lambda."""

import io

from pypdf import PdfWriter


def make_test_pdf(pages: int = 1) -> bytes:
    """Create a minimal valid PDF with the given number of blank pages.

    Used by integration tests that need a real PdfReader-parseable PDF
    but don't care about the page content (Bedrock responses are mocked).
    """
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
