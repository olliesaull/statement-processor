"""
Unit tests for statement item classification heuristics.
Grouped by heuristic category to keep future additions organized.
"""

from core.item_classification import guess_statement_item_type

# Grouped by heuristic category to keep future additions organized.


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
