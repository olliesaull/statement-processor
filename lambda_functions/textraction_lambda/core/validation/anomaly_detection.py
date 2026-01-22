"""
Lightweight "anomaly" flagging for extracted statement items.

This module runs after `table_to_json` has produced a statement JSON dict. It is
not a machine-learning model; it is a simple keyword-based detector that tries
to catch common non-transaction rows that often appear in statements, such as:
- brought/carried forward lines
- opening/closing balances
- statement totals / amount due summaries

`apply_outlier_flags` can either:
- annotate suspicious items in-place by adding an `_flags` entry (default), or
- remove them entirely (`remove=True`)

It returns the possibly-modified statement dict plus a summary that can be logged
or attached to the output for inspection.
"""

import re
from collections import Counter
from typing import Any

from config import logger

FLAG_LABEL = "ml-outlier"

# Keyword token rules used by `_keyword_hit` to mark suspicious lines.
# Each entry is: (required_tokens, human_label)
SUSPECT_TOKEN_RULES: list[tuple[set[str], str]] = [
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
    """
    Normalize arbitrary values into a token-friendly lowercase string.

    This keeps letters/digits/spaces, removes punctuation, collapses whitespace,
    and turns separators like "/" into spaces so they tokenize consistently.
    """
    if value is None:
        return ""
    text = str(value)
    text = text.replace("/", " ")
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", text.lower())
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _tokenize(text: str) -> list[str]:
    """Split normalized text into tokens, dropping empty tokens."""
    return [tok for tok in text.split() if tok]


def _keyword_hit(value: Any) -> str | None:
    """
    Return a keyword label if the value looks like a non-item "summary/balance" line.

    We treat fields like `number` and `reference` as suspicious if they contain
    tokens that match balance/summary phrases. This is a heuristic: it aims to
    catch common statement formatting issues without being overly aggressive.
    """
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
    """Return True if `value` has a non-empty string representation."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return str(value).strip() != ""


def apply_outlier_flags(statement: dict[str, Any], *, remove: bool = False, one_based_index: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:  # pylint: disable=too-many-locals,too-many-branches
    """
    Flag suspicious statement items (and optionally remove them).

    Current behavior is keyword-based (see `SUSPECT_TOKEN_RULES`).

    Returns:
    - `statement`: the input dict, possibly modified in-place
    - `summary`: counts + per-item detail about what was flagged and why
    """
    items = statement.get("statement_items", []) or []
    if not items:
        return statement, {"total": 0, "flagged": 0, "flagged_items": [], "rules": {}, "field_stats": {}}

    logger.debug("Outlier flagging start", total_items=len(items), remove=remove)

    flagged_items: list[dict[str, Any]] = []
    flagged_indices: list[int] = []
    rule_counter: Counter[str] = Counter()

    for idx, item in enumerate(items):
        # We track both high-level "issues" (for counting) and structured "details"
        # so consumers can understand exactly what was detected.
        issues: list[str] = []
        details: list[dict[str, Any]] = []

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
                details.append({"field": "reference", "issue": "keyword", "keyword": keyword, "value": str(reference_val)})

        if issues:
            flagged_indices.append(idx)
            for issue in issues:
                rule_counter[issue] += 1
            flagged_items.append({"index": (idx + 1) if one_based_index else idx, "reasons": [FLAG_LABEL], "issues": issues, "details": details})

    flagged_index_set = set(flagged_indices)
    if remove:
        # Removing flagged items is an optional mode; default is to annotate items in-place.
        statement["statement_items"] = [it for i, it in enumerate(items) if i not in flagged_index_set]
    else:
        # Add a simple marker to the item itself so downstream consumers can show warnings.
        for idx in flagged_index_set:
            flags = items[idx].setdefault("_flags", [])
            if FLAG_LABEL not in flags:
                flags.append(FLAG_LABEL)

    summary = {"total": len(items), "flagged": len(flagged_items), "flagged_items": flagged_items, "rules": dict(rule_counter), "field_stats": {}}
    logger.debug("Outlier flagging complete", flagged=summary["flagged"], rules=summary["rules"])
    return statement, summary
