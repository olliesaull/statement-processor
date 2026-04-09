"""
Unit tests for reference validation against PDF text.
"""

import pytest

import core.validation.validate_item_count as validate_item_count
from exceptions import ItemCountDisagreementError


class _FakePage:
    """Minimal page stub that mimics pdfplumber's extract_text API."""

    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdf:
    """Context manager wrapper that exposes a pages list."""

    def __init__(self, page_texts: list[str]) -> None:
        self.pages = [_FakePage(text) for text in page_texts]

    def __enter__(self) -> "_FakePdf":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


def _patch_pdf_open(monkeypatch: pytest.MonkeyPatch, page_texts: list[str]) -> None:
    def _open(_arg) -> _FakePdf:
        return _FakePdf(page_texts)

    monkeypatch.setattr(validate_item_count.pdfplumber, "open", _open)


# region Item count validation
def test_item_count_validation_skips_image_only_pdfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip validation when pdfplumber cannot extract text.

    This prevents false alarms for scanned PDFs with no selectable text.

    Args:
        None.

    Returns:
        None.
    """
    _patch_pdf_open(monkeypatch, ["", ""])

    statement_items = [{"reference": "INV-1"}]
    result = validate_item_count.validate_references_roundtrip(b"%PDF-1.4", statement_items)

    assert result["checked"] == 0
    assert result["pdf_candidates"] == 0


def test_item_count_validation_passes_when_references_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return summary when JSON references are present in PDF text.

    We bypass pdfplumber internals and focus on the comparison logic.

    Args:
        None.

    Returns:
        None.
    """
    _patch_pdf_open(monkeypatch, ["some text"])
    monkeypatch.setattr(validate_item_count, "extract_normalized_pdf_text", lambda _pdf: "INV100INV200")
    monkeypatch.setattr(validate_item_count, "extract_pdf_candidates_with_pattern", lambda _pdf, _pattern: {"INV100", "INV200"})

    statement_items = [{"reference": "INV-100"}, {"reference": "INV-200"}]
    result = validate_item_count.validate_references_roundtrip(b"%PDF-1.4", statement_items)

    assert result["checked"] == 2
    assert result["pdf_candidates"] == 2


def test_item_count_validation_raises_when_references_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise when extracted references are missing from the PDF text.

    This documents the error path used to alert on missing items.

    Args:
        None.

    Returns:
        None.
    """
    _patch_pdf_open(monkeypatch, ["some text"])
    monkeypatch.setattr(validate_item_count, "extract_normalized_pdf_text", lambda _pdf: "INV100")
    monkeypatch.setattr(validate_item_count, "extract_pdf_candidates_with_pattern", lambda _pdf, _pattern: {"INV100"})

    statement_items = [{"reference": "INV-100"}, {"reference": "INV-200"}]

    try:
        validate_item_count.validate_references_roundtrip(b"%PDF-1.4", statement_items)
    except ItemCountDisagreementError as exc:
        assert exc.summary is not None
        assert exc.summary["json_refs_found"] == 1
        assert exc.summary["json_refs_missing"] == 1
        assert exc.summary["pdf_candidates"] == 1
        assert exc.pdfplumber_count == 1
        assert exc.textract_count == 2
    else:
        raise AssertionError("Expected ItemCountDisagreementError")


# endregion


# region Reference family regex
def test_reference_family_regex_matches_known_prefixes() -> None:
    """Learn a regex that matches the observed reference families.

    This guards against regressions in the prefix/digit-length heuristics.

    Args:
        None.

    Returns:
        None.
    """
    pattern = validate_item_count.make_family_regex_from_examples(["INV-100", "INV-101", "INV-102", "CN-5000"])

    assert pattern.fullmatch("INV100")
    assert pattern.fullmatch("INV101")
    assert pattern.fullmatch("CN5000")
    assert not pattern.fullmatch("BILL100")


# endregion
