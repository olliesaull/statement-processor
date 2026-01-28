"""
Unit tests for anomaly detection flagging.
"""

from core.validation.anomaly_detection import apply_outlier_flags


# region Anomaly flagging
def test_anomaly_detection_flags_suspicious_items_and_reports_summary() -> None:
    """Flag suspicious items and emit summary counts.

    This locks in both the UI-facing `_flags` and the persisted `FlagDetails` metadata.

    Args:
        None.

    Returns:
        None.
    """
    statement = {"statement_items": [{"number": "", "reference": "Balance brought forward"}, {"number": "INV-100", "reference": "Widgets"}]}

    output, summary = apply_outlier_flags(statement, remove=False)

    items = output["statement_items"]
    assert items[0]["_flags"] == ["ml-outlier"]
    assert "_flags" not in items[1]

    flag_details = items[0]["FlagDetails"]["ml-outlier"]
    assert flag_details["issues"] == ["missing-number", "keyword-reference"]
    assert flag_details["source"] == "anomaly_detection"
    assert flag_details["details"][0]["field"] == "number"
    assert flag_details["details"][1]["field"] == "reference"

    assert summary["total"] == 2
    assert summary["flagged"] == 1
    assert summary["rules"]["missing-number"] == 1
    assert summary["rules"]["keyword-reference"] == 1

    flagged = summary["flagged_items"][0]
    assert flagged["index"] == 0
    assert flagged["reasons"] == ["ml-outlier"]
    assert set(flagged["issues"]) == {"missing-number", "keyword-reference"}


def test_anomaly_detection_remove_mode_drops_flagged_items() -> None:
    """Remove flagged items when requested.

    This ensures `remove=True` trims suspect rows rather than just annotating them.

    Args:
        None.

    Returns:
        None.
    """
    statement = {"statement_items": [{"number": "", "reference": "Balance forward"}, {"number": "INV-200", "reference": "Parts"}]}

    output, summary = apply_outlier_flags(statement, remove=True)

    items = output["statement_items"]
    assert len(items) == 1
    assert items[0]["number"] == "INV-200"

    assert summary["total"] == 2
    assert summary["flagged"] == 1


def test_anomaly_detection_ignores_normal_items() -> None:
    """Do not flag well-formed items with no suspicious text.

    This guards against false positives in routine statement rows.

    Args:
        None.

    Returns:
        None.
    """
    statement = {"statement_items": [{"number": "INV-300", "reference": "Widgets"}]}

    output, summary = apply_outlier_flags(statement, remove=False)

    items = output["statement_items"]
    assert "_flags" not in items[0]
    assert "FlagDetails" not in items[0]
    assert summary["flagged"] == 0


def test_anomaly_detection_balance_keyword_requires_short_non_numeric_text() -> None:
    """Only flag plain "balance" rows when the text is short and non-numeric.

    This keeps summary rows flagged while avoiding normal references like "Balance 2024".

    Args:
        None.

    Returns:
        None.
    """
    statement = {"statement_items": [{"number": "INV-1", "reference": "Balance"}, {"number": "INV-2", "reference": "Balance 2024"}]}

    output, summary = apply_outlier_flags(statement, remove=False)

    items = output["statement_items"]
    assert items[0]["_flags"] == ["ml-outlier"]
    assert "_flags" not in items[1]
    assert summary["flagged"] == 1


# endregion


# region FlagDetails persistence
def test_anomaly_flag_details_survive_dynamodb_sanitization() -> None:
    """Preserve FlagDetails structure through DynamoDB sanitization.

    The persistence layer should keep the nested issues/details intact so DDB
    rows still contain human-readable flag metadata.

    Args:
        None.

    Returns:
        None.
    """
    from core.textract_statement import _sanitize_for_dynamodb

    statement = {"statement_items": [{"number": "", "reference": "Balance brought forward"}]}
    output, _summary = apply_outlier_flags(statement, remove=False)

    sanitized = _sanitize_for_dynamodb(output["statement_items"][0])
    flag_details = sanitized["FlagDetails"]["ml-outlier"]

    assert flag_details["issues"] == ["missing-number", "keyword-reference"]
    assert flag_details["source"] == "anomaly_detection"
    assert flag_details["details"][0]["field"] == "number"
    assert flag_details["details"][1]["field"] == "reference"


# endregion
