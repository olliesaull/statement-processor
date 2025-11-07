from typing import Any, Dict, Optional
from config import logger
import json


class ItemCountDisagreementError(Exception):
    """Raised when pdfplumber and Textract item counts disagree.

    On creation, logs a warning including both counts and, if provided,
    the full validation summary for deeper troubleshooting.
    """

    def __init__(self, pdfplumber_count: int, textract_count: int, summary: Optional[Dict[str, Any]] = None, message: Optional[str] = None) -> None:
        msg = message or (f"PDF/Textract item count mismatch: pdfplumber={pdfplumber_count}, textract={textract_count}")
        super().__init__(msg)
        self.pdfplumber_count = pdfplumber_count
        self.textract_count = textract_count
        self.summary = summary

        # Best-effort log; never raise from logger issues inside exception init
        try:
            if summary is not None:
                logger.warning("Item count disagreement", pdfplumber_count=pdfplumber_count, textract_count=textract_count, summary=json.dumps(summary, indent=2))
            else:
                logger.warning("Item count disagreement", pdfplumber_count=pdfplumber_count, textract_count=textract_count)
        except Exception:
            pass
