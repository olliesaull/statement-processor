# flag_outliers.py
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

# ---------------- basic helpers ----------------
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

def _presence_vector_any_amount(item: Dict[str, Any]) -> Tuple[bool, ...]:
    """
    Presence signature but with a single 'has_amount' flag = (debit or credit).
    This avoids penalizing credit-only rows when debit is the global majority.
    """
    td = item.get("transaction_date", {}) or {}
    has_debit = _has_value(item.get("debit"))
    has_credit = _has_value(item.get("credit"))
    has_amount = has_debit or has_credit
    return (
        _has_value(td.get("value")),
        _has_value(item.get("document_type")),
        _has_value(item.get("supplier_reference")),
        _has_value(item.get("customer_reference")),
        _has_value(item.get("description_details")),
        has_amount,                           # << collapsed amount presence
        _has_value(item.get("invoice_balance")),
        _has_value(item.get("balance")),
    )

def _hamming(a: Tuple[bool, ...], b: Tuple[bool, ...]) -> int:
    return sum(x != y for x, y in zip(a, b))

# ---------------- doc-type normalization ----------------
def _norm_doc_type(s: str) -> str:
    """
    Very light normalization to group obvious synonyms.
    Extend/adjust as you see more vendors.
    """
    t = (s or "").strip().lower()
    if not t:
        return ""
    # common variants
    if any(k in t for k in ("inv", "invoice")):            # IN, INV, Invoice
        return "invoice"
    if any(k in t for k in ("pymt", "pmt", "paymnt", "pay", "receipt", "py")):
        return "payment"
    if any(k in t for k in ("credit note", "crn", "cn")):
        return "credit_note"
    if any(k in t for k in ("debit note", "dbn", "dn")):
        return "debit_note"
    if "adj" in t or "adjust" in t:
        return "adjustment"
    if "fee" in t or "charge" in t:
        return "charge"
    return t  # fallback: original lowercased text

def _amount_orientation(item: Dict[str, Any]) -> str:
    has_debit = _has_value(item.get("debit"))
    has_credit = _has_value(item.get("credit"))
    if has_debit and has_credit:
        return "both"
    if has_debit:
        return "debit_only"
    if has_credit:
        return "credit_only"
    return "none"

# ---------------- profiling & flagging ----------------
def _build_profiles(items: List[Dict[str, Any]]):
    """
    Build per-doc_type profiles:
      - majority presence vector (with 'any amount' collapsed)
      - expected amount orientation (debit_only / credit_only / either)
    """
    by_type = defaultdict(list)
    for it in items:
        dt = _norm_doc_type(it.get("document_type", ""))
        by_type[dt].append(it)

    profiles = {}
    for dt, rows in by_type.items():
        # presence majority
        vecs = [_presence_vector_any_amount(it) for it in rows]
        maj_vec, _ = Counter(vecs).most_common(1)[0]

        # amount orientation distribution within this doc type
        orients = [_amount_orientation(it) for it in rows]
        orient_counts = Counter(orients)
        total = sum(orient_counts.values())

        # choose expectation with support threshold; otherwise "either"
        # e.g., if 60% of payments are credit_only -> expect credit_only
        support_threshold = 0.6
        expected_orient = "either"
        for choice in ("debit_only", "credit_only"):
            if orient_counts[choice] / max(total, 1) >= support_threshold:
                expected_orient = choice
                break

        profiles[dt] = {
            "majority_vec": maj_vec,
            "expected_orient": expected_orient,
            "orient_counts": dict(orient_counts),
            "count": total,
        }

    # global fallback (in case a row has unknown/empty doc_type)
    all_vecs = [_presence_vector_any_amount(it) for it in items]
    global_majority_vec, _ = Counter(all_vecs).most_common(1)[0]
    profiles["_global"] = {"majority_vec": global_majority_vec, "expected_orient": "either", "orient_counts": {}, "count": len(items)}
    return profiles

def flag_outliers(statement: Dict[str, Any], *, hamming_threshold: int = 3) -> List[Dict[str, Any]]:
    """
    Returns diagnostics parallel to statement_items:
      [{ "flags": [...], "presence": tuple, "hamming_to_majority": n, "doc_type_group": <str> }, ...]
    """
    items = statement.get("statement_items", []) or []
    if not items:
        return []

    profiles = _build_profiles(items)

    results: List[Dict[str, Any]] = []
    for it in items:
        dt_group = _norm_doc_type(it.get("document_type", "")) or "_global"
        prof = profiles.get(dt_group, profiles["_global"])

        vec = _presence_vector_any_amount(it)
        ham = _hamming(vec, prof["majority_vec"])

        flags: List[str] = []
        # a) presence outlier vs same-type majority
        if ham >= hamming_threshold:
            flags.append(f"presence-outlier:{ham}-fields-differ")

        # b) amount orientation checks per doc type
        orient = _amount_orientation(it)
        exp = prof["expected_orient"]
        if orient == "both":
            flags.append("both-debit-and-credit")
        elif exp in ("debit_only", "credit_only"):
            if orient == "none":
                flags.append("no-amounts")
            elif orient != exp:
                flags.append(f"unexpected-amount-column:expected-{exp}")

        # c) missing date if the doc-type majority has a date
        maj_has_date = prof["majority_vec"][0]  # first bit is date presence
        td_val = (it.get("transaction_date") or {}).get("value", "")
        if maj_has_date and not _has_value(td_val):
            flags.append("missing-date")

        # d) balance-only rows (common for totals), unchanged
        has_doc_like = _has_value(it.get("document_type")) or _has_value(it.get("description_details"))
        has_invbal = _has_value(it.get("invoice_balance"))
        has_bal = _has_value(it.get("balance"))
        has_debit = _has_value(it.get("debit"))
        has_credit = _has_value(it.get("credit"))
        if (has_invbal or has_bal) and (not has_debit) and (not has_credit) and (not has_doc_like):
            flags.append("balance-only-row")

        results.append({
            "flags": flags,
            "presence": vec,
            "hamming_to_majority": ham,
            "doc_type_group": dt_group,
        })

    return results

def apply_outlier_flags(statement: Dict[str, Any], *, remove: bool = False, one_based_index: bool = False):
    """
    Annotate each row with _flags (or drop flagged rows if remove=True).
    Summary includes which rows were flagged and by what reasons.
    """
    items = statement.get("statement_items", []) or []
    flags_info = flag_outliers(statement)

    if not items:
        return statement, {"total": 0, "flagged": 0, "reasons": {}, "flagged_items": [], "doc_type_profiles": {}}

    # Build doc-type profile summary for transparency/debug
    # (how many rows per doc type and amount orientation frequencies)
    doc_type_profiles = defaultdict(lambda: {"count": 0, "orient_counts": Counter()})
    for it in items:
        dt = _norm_doc_type(it.get("document_type", "")) or "_global"
        doc_type_profiles[dt]["count"] += 1
        doc_type_profiles[dt]["orient_counts"][_amount_orientation(it)] += 1
    doc_type_profiles = {
        dt: {"count": v["count"], "orient_counts": dict(v["orient_counts"])}
        for dt, v in doc_type_profiles.items()
    }

    flagged_items = []
    for idx, (it, fi) in enumerate(zip(items, flags_info)):
        if not fi["flags"]:
            continue
        flagged_items.append({
            "index": (idx + 1) if one_based_index else idx,
            "doc_type_group": fi["doc_type_group"],
            "reasons": fi["flags"],
            "date": (it.get("transaction_date") or {}).get("value", ""),
            "document_type": it.get("document_type", ""),
            "supplier_reference": it.get("supplier_reference", ""),
            "customer_reference": it.get("customer_reference", ""),
            "debit": it.get("debit", ""),
            "credit": it.get("credit", ""),
            "balance": it.get("balance", ""),
        })

    # mutate or not
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
        "flagged_items": flagged_items,
        "doc_type_profiles": doc_type_profiles,  # helpful to see why things were/weren't flagged
    }
    return statement, summary
