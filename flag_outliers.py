from collections import Counter
from typing import Any, Dict, List, Tuple

def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return v.strip() != ""
    return True

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float))

def _row_presence_vector(item: Dict[str, Any]) -> Tuple[bool, ...]:
    """
    Boolean presence signature across key fields for 'shape' comparison.
    Adjust this list to your canonical schema.
    """
    td = item.get("transaction_date", {}) or {}
    vec = (
        _has_value(td.get("value")),
        _has_value(item.get("document_type")),
        _has_value(item.get("supplier_reference")),
        _has_value(item.get("customer_reference")),
        _has_value(item.get("description_details")),
        _has_value(item.get("debit")),
        _has_value(item.get("credit")),
        _has_value(item.get("invoice_balance")),
        _has_value(item.get("balance")),
    )
    return vec

def _hamming(a: Tuple[bool, ...], b: Tuple[bool, ...]) -> int:
    return sum(x != y for x, y in zip(a, b))

def flag_outliers(statement: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns a list parallel to statement_items with per-row diagnostics:
      [{ "flags": [...], "presence": tuple, "hamming_to_majority": n }, ...]
    Empty flags == row looks normal.
    """
    items = statement.get("statement_items", []) or []
    if not items:
        return []

    vectors = [_row_presence_vector(it) for it in items]
    counts = Counter(vectors)
    majority_vec, _ = counts.most_common(1)[0]
    per_field_majority = tuple(sum(v[i] for v in vectors) > len(vectors) / 2 for i in range(len(majority_vec)))

    results: List[Dict[str, Any]] = []
    for it, vec in zip(items, vectors):
        flags = []
        ham = _hamming(vec, majority_vec)

        # presence-pattern outlier (structurally different than most)
        if ham >= 3:
            flags.append(f"presence-outlier:{ham}-fields-differ")

        # amounts sanity
        debit, credit = it.get("debit"), it.get("credit")
        has_debit, has_credit = _has_value(debit), _has_value(credit)
        if has_debit and has_credit:
            flags.append("both-debit-and-credit")
        maj_has_amount = per_field_majority[5] or per_field_majority[6]
        if (not has_debit) and (not has_credit) and maj_has_amount:
            flags.append("no-amounts")

        # balance-only rows
        has_invbal = _has_value(it.get("invoice_balance"))
        has_bal = _has_value(it.get("balance"))
        has_doc_like = _has_value(it.get("document_type")) or _has_value(it.get("description_details"))
        if (has_invbal or has_bal) and (not has_debit) and (not has_credit) and (not has_doc_like):
            flags.append("balance-only-row")

        # missing date if majority has a date
        td_val = (it.get("transaction_date") or {}).get("value", "")
        if per_field_majority[0] and not _has_value(td_val):
            flags.append("missing-date")

        # sparse single-amount row
        num_cells = sum(1 for v in [debit, credit, it.get("invoice_balance"), it.get("balance")] if _is_number(v))
        non_empty_cells = sum(1 for k, v in it.items() if k != "raw" and _has_value(v))
        if non_empty_cells <= 3 and num_cells == 1:
            flags.append("sparse-single-amount")

        results.append({
            "flags": flags,
            "presence": vec,
            "hamming_to_majority": ham,
        })

    return results

def apply_outlier_flags(statement: Dict[str, Any], *, remove: bool = False, one_based_index: bool = False):
    """
    Annotate each row with _flags (or drop flagged rows if remove=True).
    Returns (statement, summary) where summary includes the list of flagged items.

    Note: if remove=True, indices in summary refer to the ORIGINAL positions.
    """
    items = statement.get("statement_items", []) or []
    flags_info = flag_outliers(statement)

    if not items:
        return statement, {"total": 0, "flagged": 0, "reasons": {}, "flagged_items": []}

    # Build flagged items list (using original indices)
    flagged_items = []
    for idx, (it, fi) in enumerate(zip(items, flags_info)):
        if not fi["flags"]:
            continue
        flagged_items.append({
            "index": (idx + 1) if one_based_index else idx,
            "reasons": fi["flags"],
            "date": (it.get("transaction_date") or {}).get("value", ""),
            "document_type": it.get("document_type", ""),
            "supplier_reference": it.get("supplier_reference", ""),
            "customer_reference": it.get("customer_reference", ""),
            "debit": it.get("debit", ""),
            "credit": it.get("credit", ""),
            "balance": it.get("balance", ""),
        })

    # Optionally attach flags to each item or remove flagged items
    if remove:
        statement["statement_items"] = [it for it, fi in zip(items, flags_info) if not fi["flags"]]
    else:
        for it, fi in zip(items, flags_info):
            it["_flags"] = fi["flags"]

    reason_counts = Counter([r for fi in flags_info for r in fi["flags"]])

    summary = {
        "total": len(items),
        "flagged": len(flagged_items),
        "reasons": dict(reason_counts),
        "flagged_items": flagged_items,   # ← the list you asked for
    }
    return statement, summary
