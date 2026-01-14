"""Custom exceptions used by the statement processing Lambda."""

from typing import Any, Dict, Optional


class ItemCountDisagreementError(Exception):
    """Raised when PDF/Textract item counts diverge."""

    def __init__(
        self,
        pdfplumber_count: int,
        textract_count: int,
        summary: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
    ) -> None:
        msg = message or (
            f"PDF/Textract item count mismatch: pdfplumber={pdfplumber_count}, textract={textract_count}"
        )
        super().__init__(msg)
        self.summary = summary
        self.pdfplumber_count = pdfplumber_count
        self.textract_count = textract_count
