"""
Unit tests for anomaly detection flagging.
"""

from core.validation.anomaly_detection import apply_outlier_flags


# region Anomaly flagging
def test_anomaly_detection_flags_suspicious_items_and_reports_summary() -> None:
    """Flag suspicious items and emit summary counts.

    Args:
        None.

    Returns:
        None.
    """
    statement = {"statement_items": [{"number": "", "reference": "Balance brought forward"}, {"number": "INV-100", "reference": "Widgets"}]}

    output, summary = apply_outlier_flags(statement, remove=False, one_based_index=True)

    items = output["statement_items"]
    assert items[0]["_flags"] == ["ml-outlier"]
    assert "_flags" not in items[1]

    assert summary["total"] == 2
    assert summary["flagged"] == 1
    assert summary["rules"]["missing-number"] == 1
    assert summary["rules"]["keyword-reference"] == 1

    flagged = summary["flagged_items"][0]
    assert flagged["index"] == 1
    assert flagged["reasons"] == ["ml-outlier"]
    assert set(flagged["issues"]) == {"missing-number", "keyword-reference"}


def test_anomaly_detection_remove_mode_drops_flagged_items() -> None:
    """Remove flagged items when requested.

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


# endregion
