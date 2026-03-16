"""PDF page-count helpers for upload validation.

The browser keeps its lightweight estimate for instant UX feedback, but the
backend now uses ``pypdf`` so token enforcement is based on a real PDF parse
rather than a raw-byte heuristic. The same helper is used by both the preflight
API and the final upload path so the server always applies one consistent rule.
"""

from pypdf import PdfReader
from werkzeug.datastructures import FileStorage


class PDFPageCountError(ValueError):
    """Raised when a PDF page count cannot be determined safely."""


def count_pdf_pages(uploaded_file: FileStorage) -> int:
    """Return a parsed page count for an uploaded PDF while preserving the stream.

    Args:
        uploaded_file: Uploaded PDF file wrapper from Flask/Werkzeug.

    Returns:
        Positive integer page count.

    Raises:
        PDFPageCountError: When the upload stream cannot be counted.
    """
    stream = uploaded_file.stream
    stream.seek(0)
    try:
        page_count = len(PdfReader(stream, strict=False).pages)
    except Exception as exc:  # pragma: no cover - pypdf exposes several parse-time exception types.
        raise PDFPageCountError("Unable to determine PDF page count") from exc
    finally:
        stream.seek(0)

    if page_count <= 0:
        raise PDFPageCountError("PDF contains no pages")

    return page_count
