"""
Unit tests for statement item classification heuristics.
Grouped by heuristic category to keep future additions organized.
"""

from dataclasses import dataclass
from typing import Any

import pytest

from core.item_classification import guess_statement_item_type


@dataclass(frozen=True)
class ClassificationCase:
    """
    Represents a statement item classification test case.

    This test-only model groups classifier inputs to keep the matrix readable.

    Attributes:
        name: Human-friendly case id for pytest output.
        raw_row: Raw statement row values provided to the classifier.
        total_entries: Totals payload used to infer debit/credit hints.
        contact_config: Optional contact config override for label hints.
        expected: Expected classification label.
    """

    name: str
    raw_row: dict[str, Any]
    total_entries: Any
    contact_config: dict[str, Any] | None
    expected: str


CONTACT_CONFIG_CUSTOM_LABELS: dict[str, Any] = {"statement_items": {"total": {"debit": ["Charges"], "credit": ["Payments"]}}}


# region Amount-based hints
_AMOUNT_HINT_CASES = [
    # Debit-only totals should default to invoice to keep row labeling stable.
    ClassificationCase(name="debit-only invoice", raw_row={"Description": "Widgets"}, total_entries={"Debit": "100"}, contact_config=None, expected="invoice"),
    # Credit-only totals with no text should default to payment for determinism.
    ClassificationCase(name="credit-only empty text defaults to payment", raw_row={}, total_entries={"Credit": "50"}, contact_config=None, expected="payment"),
    # Credit-only totals should flip to credit_note when the text says so.
    ClassificationCase(name="credit-only text matches credit note", raw_row={"Description": "Credit note 123"}, total_entries={"Credit": "50"}, contact_config=None, expected="credit_note"),
    # When both sides exist, invoice text should drive the type decision.
    ClassificationCase(name="debit+credit invoice text", raw_row={"Reference": "Invoice INV-42"}, total_entries={"Debit": "100", "Credit": "20"}, contact_config=None, expected="invoice"),
    # Payment wording should win over the credit default when present.
    ClassificationCase(name="credit-only text matches payment", raw_row={"Description": "Payment received"}, total_entries={"Credit": "25"}, contact_config=None, expected="payment"),
    # Invoice text should not override the credit-only candidate set.
    ClassificationCase(name="credit-only ignores invoice text", raw_row={"Description": "Invoice 123"}, total_entries={"Credit": "25"}, contact_config=None, expected="payment"),
    # If both sides are present and text is empty, stay with invoice default.
    ClassificationCase(name="debit+credit empty text defaults to invoice", raw_row={}, total_entries={"Debit": "100", "Credit": "20"}, contact_config=None, expected="invoice"),
]


@pytest.mark.parametrize("case", _AMOUNT_HINT_CASES, ids=[case.name for case in _AMOUNT_HINT_CASES])
def test_amount_hints(case: ClassificationCase) -> None:
    result = guess_statement_item_type(raw_row=case.raw_row, total_entries=case.total_entries, contact_config=case.contact_config)
    assert result == case.expected


# endregion


# region Label/format variations
_LABEL_VARIATION_CASES = [
    # List-style totals should still be parsed for credit defaults.
    ClassificationCase(name="list totals credit label", raw_row={}, total_entries=[{"label": "CR", "value": "50"}], contact_config=None, expected="payment"),
    # Parenthetical credit values should count as amounts.
    ClassificationCase(name="parenthetical credit value", raw_row={}, total_entries={"Credit": "(10.00)"}, contact_config=None, expected="payment"),
    # Comma-separated debit values should still parse as amounts.
    ClassificationCase(name="debit value with commas", raw_row={}, total_entries={"Debit": "1,234.50"}, contact_config=None, expected="invoice"),
    # Debit keys in raw rows should provide a fallback hint.
    ClassificationCase(name="raw row debit key", raw_row={"Debit": "100"}, total_entries={}, contact_config=None, expected="invoice"),
    # Contact-configured labels should map to debit/credit hints.
    ClassificationCase(name="contact config custom debit label", raw_row={"Description": "Monthly"}, total_entries={"Charges": "75"}, contact_config=CONTACT_CONFIG_CUSTOM_LABELS, expected="invoice"),
]


@pytest.mark.parametrize("case", _LABEL_VARIATION_CASES, ids=[case.name for case in _LABEL_VARIATION_CASES])
def test_label_variations(case: ClassificationCase) -> None:
    result = guess_statement_item_type(raw_row=case.raw_row, total_entries=case.total_entries, contact_config=case.contact_config)
    assert result == case.expected


# endregion


# region Confidence thresholds
_CONFIDENCE_THRESHOLD_CASES = [
    # When payment and credit-note signals tie, prefer payment deterministically.
    ClassificationCase(name="payment beats credit note tie", raw_row={"Description": "Payment credit note"}, total_entries={"Credit": "10"}, contact_config=None, expected="payment"),
    # Joined text like CreditNote123 should still match credit notes.
    ClassificationCase(name="joined credit note text", raw_row={"Description": "CreditNote123"}, total_entries={"Credit": "10"}, contact_config=None, expected="credit_note"),
    # Short tokens such as CR should clear the credit-note threshold.
    ClassificationCase(name="short credit token", raw_row={"Reference": "CR 123"}, total_entries={"Credit": "10"}, contact_config=None, expected="credit_note"),
    # Low-similarity tokens should fall back to the amount-based default.
    ClassificationCase(name="low score defaults", raw_row={"Reference": "XYZ"}, total_entries={"Credit": "10"}, contact_config=None, expected="payment"),
]


@pytest.mark.parametrize("case", _CONFIDENCE_THRESHOLD_CASES, ids=[case.name for case in _CONFIDENCE_THRESHOLD_CASES])
def test_confidence_thresholds(case: ClassificationCase) -> None:
    result = guess_statement_item_type(raw_row=case.raw_row, total_entries=case.total_entries, contact_config=case.contact_config)
    assert result == case.expected


# endregion
