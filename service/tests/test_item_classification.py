"""
Unit tests for statement item classification heuristics.
Grouped by heuristic category to keep future additions organized.
"""

import logging
import sys
import types

# The classifier imports the global config module for logging, which triggers
# AWS SSM lookups on import (does not work for agents). Stub it so unit tests stay fast and offline.
fake_config = types.ModuleType("config")
fake_config.logger = logging.getLogger("statement-processor.tests")
sys.modules["config"] = fake_config

from core.item_classification import guess_statement_item_type  # noqa: E402


# region Amount-based hints
def test_amount_hint_debit_only_returns_invoice() -> None:
    """Return invoice when only debit totals are present.

    We rely on debit-only totals to classify the most common statement rows
    even when the description text is unhelpful. This protects UX from rows
    that lack clear keywords (e.g. plain amounts).

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "Widgets"}, total_entries={"Debit": "100"}, contact_config=None)
    assert result == "invoice"


def test_amount_hint_credit_only_defaults_to_payment_when_text_empty() -> None:
    """Return payment when only credit totals exist and text is empty.

    When we only see credit values, the classifier narrows to credit-note vs
    payment. With no text evidence, it should default to payment so UI labels
    remain stable and deterministic.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={}, total_entries={"Credit": "50"}, contact_config=None)
    assert result == "payment"


def test_amount_hint_credit_only_prefers_credit_note_when_text_matches() -> None:
    """Return credit_note when credit totals and matching text are present.

    Credit notes often include explicit text. We validate that even with a
    credit-only amount hint, matching text flips the decision away from the
    default payment classification.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "Credit note 123"}, total_entries={"Credit": "50"}, contact_config=None)
    assert result == "credit_note"


def test_amount_hint_both_debit_and_credit_allows_text_to_drive_invoice() -> None:
    """Return invoice when both sides present and invoice text appears.

    Some statements include both debit and credit columns in the same row.
    In that case the heuristic should rely on textual evidence, not amounts
    alone, to choose the right type.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Reference": "Invoice INV-42"}, total_entries={"Debit": "100", "Credit": "20"}, contact_config=None)
    assert result == "invoice"


def test_amount_hint_credit_only_prefers_payment_when_text_matches() -> None:
    """Return payment when credit totals and payment text are present.

    Payments are the most frequent credit-side rows. This test ensures the
    payment synonym list wins when the row explicitly mentions a payment.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "Payment received"}, total_entries={"Credit": "25"}, contact_config=None)
    assert result == "payment"


def test_amount_hint_credit_only_ignores_invoice_text() -> None:
    """Ignore invoice text when the amount hint restricts candidates to credit types.

    The classifier narrows candidate types based on credit-only amounts, so even
    if a row mentions "invoice" the result must stay within credit-related types.
    This protects us from mismatched wording in payment narratives.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "Invoice 123"}, total_entries={"Credit": "25"}, contact_config=None)
    assert result == "payment"


def test_amount_hint_both_debit_and_credit_defaults_to_invoice_when_text_missing() -> None:
    """Default to invoice when both debit and credit are present but text is empty.

    Some statements include both debit and credit columns with sparse row text.
    With no textual evidence, the classifier should land on the stable default
    (invoice) to keep UI labeling consistent.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={}, total_entries={"Debit": "100", "Credit": "20"}, contact_config=None)
    assert result == "invoice"


# endregion


# region Label/format variations
def test_label_variation_list_totals_credit_label_defaults_to_payment() -> None:
    """Handle list-style totals with credit labels.

    The extraction layer may emit totals as a list of {label, value} entries.
    We verify those inputs still trigger the credit-only default behavior.

    Args:
        None.

    Returns:
        None.
    """
    total_entries = [{"label": "CR", "value": "50"}]
    result = guess_statement_item_type(raw_row={}, total_entries=total_entries, contact_config=None)
    assert result == "payment"


def test_label_variation_parenthetical_credit_value_counts_as_amount() -> None:
    """Treat parenthetical values as valid credit amounts.

    Statements often show negative credits in parentheses. We need to interpret
    them as non-zero amounts so credit-only hints still drive classification.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={}, total_entries={"Credit": "(10.00)"}, contact_config=None)
    assert result == "payment"


def test_label_variation_debit_value_with_commas_counts_as_amount() -> None:
    """Parse comma-separated debit values as non-zero amounts.

    OCR extracts totals with separators (e.g., "1,234.50"). We verify that the
    numeric parser treats these as valid amounts so invoice hints still apply.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={}, total_entries={"Debit": "1,234.50"}, contact_config=None)
    assert result == "invoice"


def test_label_variation_raw_row_debit_key_is_respected() -> None:
    """Infer debit totals from raw row keys when totals are missing.

    Some statement layouts omit explicit totals while still including debit/
    credit columns in the raw row. The classifier should fall back to those
    raw values so we still get an invoice hint.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Debit": "100"}, total_entries={}, contact_config=None)
    assert result == "invoice"


def test_label_variation_contact_config_maps_custom_debit_label() -> None:
    """Use contact-configured debit labels to drive classification.

    Contact configs allow custom debit/credit labels (e.g. "Charges").
    We ensure those mappings influence the amount hint so classification
    stays aligned with tenant-specific statement formats.

    Args:
        None.

    Returns:
        None.
    """
    contact_config = {"statement_items": {"total": {"debit": ["Charges"], "credit": ["Payments"]}}}
    result = guess_statement_item_type(raw_row={"Description": "Monthly"}, total_entries={"Charges": "75"}, contact_config=contact_config)
    assert result == "invoice"


# endregion


# region Confidence thresholds
def test_confidence_threshold_payment_wins_when_credit_note_also_present() -> None:
    """Prefer payment when payment and credit-note signals are equally strong.

    If both "payment" and "credit note" appear in the same row, the heuristic
    resolves ties by processing payment first. This ensures deterministic output
    when OCR captures multiple document cues in one description.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "Payment credit note"}, total_entries={"Credit": "10"}, contact_config=None)
    assert result == "payment"


def test_confidence_threshold_joined_text_matches_credit_note_compound() -> None:
    """Match compound credit-note text even when tokenization splits words.

    Some rows render as "CreditNote123" with no whitespace. The joined-text
    matching path should detect "creditnote" as a substring and prefer the
    credit-note classification over the payment default.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Description": "CreditNote123"}, total_entries={"Credit": "10"}, contact_config=None)
    assert result == "credit_note"


def test_confidence_threshold_short_credit_token_passes() -> None:
    """Accept short credit-note tokens that clear confidence thresholds.

    Many statements use short tokens like "CR" instead of full words. The
    classifier explicitly caps short-token scores; this test confirms the
    capped score still clears the credit-note threshold.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Reference": "CR 123"}, total_entries={"Credit": "10"}, contact_config=None)
    assert result == "credit_note"


def test_confidence_threshold_low_score_falls_back_to_default() -> None:
    """Fall back to the default type when similarity is too low.

    We guard against random tokens (e.g. OCR noise) causing incorrect type
    flips. This test checks that low similarity stays with the default type
    chosen from the amount hint.

    Args:
        None.

    Returns:
        None.
    """
    result = guess_statement_item_type(raw_row={"Reference": "XYZ"}, total_entries={"Credit": "10"}, contact_config=None)
    assert result == "payment"


# endregion
