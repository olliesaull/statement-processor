from typing import Any, Dict, List, Optional
import json

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

    print(json.dumps(summary, indent=2))

    assert summary["total"] == len(items)
    assert summary["flagged"] == 0
    # ensure none of the items were annotated with the flag
    assert all("_flags" not in item for item in statement["statement_items"])


def test_apply_outlier_flags_detects_extreme_total():
    """A single extreme total should be surfaced as an anomaly."""
    items = [
        _make_statement_item(total=120 + i * 3, number=f"INV-{i:03d}", date=f"{(i % 26) + 1:02d}/06/2024")
        for i in range(1, 26)
    ]
    outlier = _make_statement_item(
        total=5000.0,
        number="INV-999",
        date="15/06/2024",
    )
    items.append(outlier)

    statement, summary = apply_outlier_flags(_build_statement(items))

    assert summary["total"] == len(items)
    assert summary["flagged"] == 1
    flagged_indices = {item["index"] for item in summary["flagged_items"]}
    assert len(items) - 1 in flagged_indices  # outlier appended last
    flagged_item = statement["statement_items"][len(items) - 1]
    assert flagged_item.get("_flags") == ["ml-outlier"]
    assert summary["flagged_items"][0]["reasons"] == ["ml-outlier"]
