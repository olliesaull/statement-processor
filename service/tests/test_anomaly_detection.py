from typing import Any, Dict, List, Optional

from core.validation.anomaly_detection import apply_outlier_flags


def _make_statement_item(
    *,
    total: float,
    amount_paid: float = 0.0,
    date: str = "01/07/2024",
    due_date: str = "31/07/2024",
    number: str,
    reference: str = "Invoice",
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "total": f"{total:.2f}",
        "amount_due": {"current": f"{max(total - amount_paid, 0):.2f}"},
        "amount_paid": f"{amount_paid:.2f}",
        "date": date,
        "due_date": due_date,
        "number": number,
        "reference": reference,
        "date_format": "DD/MM/YYYY",
        "raw": raw or {"description": f"{reference} {number}"},
    }


def _build_statement(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"statement_items": items}


def test_apply_outlier_flags_no_anomalies():
    """Uniform invoices should not trigger false positives."""
    items = [
        _make_statement_item(total=100 + i * 5, number=f"INV-{i:03d}", date=f"{(i % 27) + 1:02d}/07/2024")
        for i in range(1, 21)
    ]

    statement, summary = apply_outlier_flags(_build_statement(items))

    assert summary["total"] == len(items)
    assert summary["flagged"] == 0
    assert summary["rules"] == {}
    # ensure none of the items were annotated with the flag
    assert all("_flags" not in item for item in statement["statement_items"])


def test_apply_outlier_flags_flags_missing_number():
    """Rows without an invoice number should be flagged."""
    items = [
        _make_statement_item(total=120 + i * 3, number=f"INV-{i:03d}", date=f"{(i % 26) + 1:02d}/06/2024")
        for i in range(1, 26)
    ]
    items.append(
        _make_statement_item(
            total=220.0,
            number="",
            reference="Balance b/f",
        )
    )

    statement, summary = apply_outlier_flags(_build_statement(items))

    assert summary["total"] == len(items)
    assert summary["flagged"] == 1
    flagged = summary["flagged_items"][0]
    assert flagged["index"] == len(items) - 1
    assert "missing-number" in flagged["issues"]
    assert summary["rules"]["missing-number"] == 1
    detail = next(det for det in flagged["details"] if det["field"] == "number")
    assert detail["issue"] == "missing"
    assert statement["statement_items"][flagged["index"]]["_flags"] == ["ml-outlier"]


def test_apply_outlier_flags_flags_keyword_reference():
    """Reference lines containing balance terminology should be highlighted."""
    items = [
        _make_statement_item(total=150.0, number=f"INV-{i:03d}", reference="Invoice")
        for i in range(1, 10)
    ]
    odd = _make_statement_item(
        total=150.0,
        number="INV-999",
        reference="Balance b/f",
    )
    items.append(odd)

    statement, summary = apply_outlier_flags(_build_statement(items))

    assert summary["flagged"] == 1
    flagged = summary["flagged_items"][0]
    assert flagged["index"] == len(items) - 1
    assert "keyword-reference" in flagged["issues"]
    assert summary["rules"]["keyword-reference"] == 1
    detail = next(det for det in flagged["details"] if det["field"] == "reference")
    assert detail["keyword"] == "balance b/f"
