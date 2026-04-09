"""Custom exceptions used by the statement processing Lambda."""

from typing import Any


class ItemCountDisagreementError(Exception):
    """Raised when PDF/extraction item counts diverge."""

    def __init__(self, pdfplumber_count: int, extraction_count: int, summary: dict[str, Any] | None = None, message: str | None = None) -> None:
        msg = message or (f"PDF/extraction item count mismatch: pdfplumber={pdfplumber_count}, extraction={extraction_count}")
        super().__init__(msg)
        self.summary = summary
        self.pdfplumber_count = pdfplumber_count
        self.extraction_count = extraction_count
