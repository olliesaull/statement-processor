import re
from collections import Counter
from typing import Any, Dict, List, Tuple

FLAG_LABEL = "ml-outlier"

SUSPECT_TOKEN_RULES: List[Tuple[set[str], str]] = [
    ({"brought", "forward"}, "brought forward"),
    ({"carried", "forward"}, "carried forward"),
    ({"balance", "forward"}, "balance forward"),
    ({"forward", "balance"}, "forward balance"),
    ({"balance", "b", "f"}, "balance b/f"),
    ({"balance", "c", "f"}, "balance c/f"),
    ({"balance", "bf"}, "balance bf"),
    ({"balance", "cf"}, "balance cf"),
    ({"closing", "balance"}, "closing balance"),
    ({"opening", "balance"}, "opening balance"),
    ({"previous", "balance"}, "previous balance"),
    ({"statement", "balance"}, "statement balance"),
    ({"statement", "total"}, "statement total"),
    ({"outstanding", "balance"}, "outstanding balance"),
    ({"ending", "balance"}, "ending balance"),
    ({"final", "balance"}, "final balance"),
    ({"amount", "due"}, "amount due"),
    ({"balance", "brought"}, "balance brought"),
    ({"balance", "carried"}, "balance carried"),
    ({"summary"}, "summary"),
    ({"balance"}, "balance"),
]


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> List[str]:
    return [tok for tok in text.split() if tok]


def _keyword_hit(value: Any) -> str | None:
    text = _normalize_text(value)
    if not text:
        return None
    tokens = _tokenize(text)
    if not tokens:
        return None
    token_set = set(tokens)

    for required_tokens, label in SUSPECT_TOKEN_RULES:
        if required_tokens.issubset(token_set):
            if required_tokens == {"balance"}:
                if len(tokens) <= 3 and not any(tok.isdigit() for tok in tokens):
                    return label
            elif required_tokens == {"summary"}:
                if len(tokens) <= 3:
                    return label
            else:
                return label
    return None


def _has_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return str(value).strip() != ""


def apply_outlier_flags(
    statement: Dict[str, Any],
    *,
    remove: bool = False,
    one_based_index: bool = False,
    threshold_method: str = "iqr",
    percentile: float = 0.98,
    iqr_k: float = 1.5,
    zscore_z: float = 3.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _ = (threshold_method, percentile, iqr_k, zscore_z)

    items = statement.get("statement_items", []) or []
    if not items:
        return statement, {"total": 0, "flagged": 0, "flagged_items": [], "rules": {}, "field_stats": {}}

    flagged_items: List[Dict[str, Any]] = []
    flagged_indices: List[int] = []
    rule_counter: Counter[str] = Counter()

    for idx, item in enumerate(items):
        issues: List[str] = []
        details: List[Dict[str, Any]] = []

        number_val = item.get("number")
        reference_val = item.get("reference")

        if not _has_text(number_val):
            issues.append("missing-number")
            details.append({"field": "number", "issue": "missing"})
        else:
            keyword = _keyword_hit(number_val)
            if keyword:
                issues.append("keyword-number")
                details.append({"field": "number", "issue": "keyword", "keyword": keyword, "value": str(number_val)})

        if _has_text(reference_val):
            keyword = _keyword_hit(reference_val)
            if keyword:
                issues.append("keyword-reference")
                details.append(
                    {
                        "field": "reference",
                        "issue": "keyword",
                        "keyword": keyword,
                        "value": str(reference_val),
                    }
                )

        if issues:
            flagged_indices.append(idx)
            for issue in issues:
                rule_counter[issue] += 1
            flagged_items.append(
                {
                    "index": (idx + 1) if one_based_index else idx,
                    "reasons": [FLAG_LABEL],
                    "issues": issues,
                    "details": details,
                }
            )

    flagged_index_set = set(flagged_indices)
    if remove:
        statement["statement_items"] = [it for i, it in enumerate(items) if i not in flagged_index_set]
    else:
        for idx in flagged_index_set:
            flags = items[idx].setdefault("_flags", [])
            if FLAG_LABEL not in flags:
                flags.append(FLAG_LABEL)

    summary = {
        "total": len(items),
        "flagged": len(flagged_items),
        "flagged_items": flagged_items,
        "rules": dict(rule_counter),
        "field_stats": {},
    }
    return statement, summary
