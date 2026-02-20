"""
Heuristics for classifying statement rows.

The classifier uses:
- Configured column labels (from contact mapping config)
- Presence of debit/credit values in a row
- Text matching against synonym lists (invoice/credit/payment)

It is intentionally best-effort and falls back to a default type when evidence is weak.
"""

import difflib
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from core.models import ContactConfig
from logger import logger

_DEBIT_HINTS: tuple[str, ...] = ("debit", "dr")
_CREDIT_HINTS: tuple[str, ...] = ("credit", "cr")
_NUMERIC_CHARS_RE = re.compile(r"[^0-9\-\.,()]")


def _safe_decimal(value: str) -> Decimal | None:
    """Return a Decimal for the string value or None if parsing fails."""
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _normalize_label(label: Any) -> str:
    """Lowercase and drop non-alphanumeric characters for label matching."""
    if label is None:
        return ""
    return "".join(ch.lower() for ch in str(label) if ch.isalnum())


def _flatten_labels(value: Any) -> list[str]:
    """Extract a list of non-empty strings from a value that may be a list or scalar."""
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        labels: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                labels.append(item.strip())
        return labels
    return []


def _is_debit_norm(norm: str) -> bool:
    """Return True when the normalized label looks like a debit column."""
    return any(norm.startswith(hint) or norm.endswith(hint) for hint in _DEBIT_HINTS)


def _is_credit_norm(norm: str) -> bool:
    """Return True when the normalized label looks like a credit column."""
    return any(norm.startswith(hint) or norm.endswith(hint) for hint in _CREDIT_HINTS)


def _coerce_decimal(value: Any) -> Decimal | None:
    """Parse a value into a Decimal, handling commas/parentheses for negatives."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return _safe_decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    cleaned = _NUMERIC_CHARS_RE.sub("", text)
    if "(" in text and ")" in text and "-" not in cleaned:
        cleaned = "-" + cleaned
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = cleaned.replace(",", "").replace(" ", "")
    cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip()
    if cleaned in {"", "-", ".", "-.", ".-"}:
        return None
    return _safe_decimal(cleaned)


def _has_amount(value: Any) -> bool:
    """Return True when a value parses to a non-zero Decimal."""
    dec = _coerce_decimal(value)
    return dec is not None and dec != 0


def _collect_config_amount_labels(contact_config: ContactConfig | None) -> tuple[set[str], set[str]]:
    """Collect normalized debit/credit labels from contact config."""
    total_cfg = contact_config.total if contact_config else []
    debit_norms: set[str] = set()
    credit_norms: set[str] = set()

    def _record(labels: Iterable[str], bucket: str | None) -> None:
        for label in labels:
            norm = _normalize_label(label)
            if not norm:
                continue
            if bucket == "debit" or _is_debit_norm(norm):
                debit_norms.add(norm)
            if bucket == "credit" or _is_credit_norm(norm):
                credit_norms.add(norm)

    labels = _flatten_labels(total_cfg)
    _record(labels, None)

    return debit_norms, credit_norms


def _iter_total_entries(total_entries: dict[str, Any] | None) -> Iterable[tuple[str, Any]]:
    """Yield (label, value) pairs from dict-style total data."""
    if not isinstance(total_entries, dict):
        return
    for label, value in total_entries.items():
        if isinstance(label, str):
            yield label, value


def _extend_amount_norms(raw_row: dict[str, Any], debit_norms: set[str], credit_norms: set[str]) -> None:
    """Add debit/credit norms derived from raw row labels."""
    for key in raw_row or {}:
        norm = _normalize_label(key)
        if _is_debit_norm(norm):
            debit_norms.add(norm)
        elif _is_credit_norm(norm):
            credit_norms.add(norm)


def _classify_amount_label(label: str, debit_norms: set[str], credit_norms: set[str]) -> str | None:
    """Classify label as debit/credit and update norms if inferred."""
    norm = _normalize_label(label)
    if norm in debit_norms:
        return "debit"
    if norm in credit_norms:
        return "credit"
    if _is_debit_norm(norm):
        debit_norms.add(norm)
        return "debit"
    if _is_credit_norm(norm):
        credit_norms.add(norm)
        return "credit"
    return None


def _scan_total_entries(total_entries: dict[str, Any] | None, debit_norms: set[str], credit_norms: set[str]) -> tuple[bool, bool, list[str], list[str]]:
    """Inspect total entries and return amount evidence."""
    debit_has_value = False
    credit_has_value = False
    debit_labels_used: list[str] = []
    credit_labels_used: list[str] = []

    for label, value in _iter_total_entries(total_entries):
        category = _classify_amount_label(label, debit_norms, credit_norms)
        if category == "debit":
            if _has_amount(value):
                debit_has_value = True
                debit_labels_used.append(label)
        elif category == "credit" and _has_amount(value):
            credit_has_value = True
            credit_labels_used.append(label)

    return debit_has_value, credit_has_value, debit_labels_used, credit_labels_used


def _inverse_row(raw_row: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    """Build a normalized-label index for raw row values."""
    return {_normalize_label(key): (key, val) for key, val in (raw_row or {}).items() if isinstance(key, str)}


def _scan_inverse_amounts(inverse_raw: dict[str, tuple[str, Any]], norms: set[str]) -> tuple[bool, list[str]]:
    """Check inverse row for any amount values matching known norms."""
    for norm in norms:
        match = inverse_raw.get(norm)
        if match and _has_amount(match[1]):
            return True, [match[0]]
    return False, []


def _evaluate_amount_hint(raw_row: dict[str, Any], total_entries: dict[str, Any] | None, contact_config: ContactConfig | None) -> tuple[str | None, bool, bool, list[str], list[str]]:
    """
    Infer debit/credit signals and return a type hint plus evidence details.

    Returns:
        (amount_hint, debit_has_value, credit_has_value, debit_labels_used, credit_labels_used)
    """
    debit_norms, credit_norms = _collect_config_amount_labels(contact_config)

    _extend_amount_norms(raw_row, debit_norms, credit_norms)
    debit_has_value, credit_has_value, debit_labels_used, credit_labels_used = _scan_total_entries(total_entries, debit_norms, credit_norms)

    if not debit_has_value or not credit_has_value:
        inverse_raw = _inverse_row(raw_row)
        if not debit_has_value:
            found, labels = _scan_inverse_amounts(inverse_raw, debit_norms)
            if found:
                debit_has_value = True
                debit_labels_used.extend(labels)
        if not credit_has_value:
            found, labels = _scan_inverse_amounts(inverse_raw, credit_norms)
            if found:
                credit_has_value = True
                credit_labels_used.extend(labels)

    amount_hint = None
    if debit_has_value and not credit_has_value:
        amount_hint = "invoice"
    elif credit_has_value and not debit_has_value:
        amount_hint = "credit"

    return amount_hint, debit_has_value, credit_has_value, debit_labels_used, credit_labels_used


def _default_type(candidate_types: set[str]) -> str:
    """Pick the first preferred type from the candidate set."""
    for option in ("invoice", "payment", "credit_note"):
        if option in candidate_types:
            return option
    return "invoice"


def _compact_text(value: Any) -> str:
    """Uppercase and strip non-alphanumeric characters."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _extract_tokens(text: str) -> list[str]:
    """Extract compact tokens from text for synonym matching."""
    tokens = [_compact_text(tok) for tok in re.findall(r"[A-Za-z0-9]+", text.upper())]
    return [token for token in tokens if token]


def _candidate_types_from_hint(amount_hint: str | None) -> set[str]:
    """Return candidate document types given an amount hint."""
    if amount_hint == "invoice":
        return {"invoice"}
    if amount_hint == "credit":
        return {"credit_note", "payment"}
    return {"invoice", "credit_note", "payment"}


def _score_token_similarity(token: str, syn_norm: str) -> float:
    """Score a token against a synonym string."""
    if token == syn_norm:
        score = 1.0
    elif token.startswith(syn_norm) or syn_norm.startswith(token):
        score = 0.9
    else:
        score = difflib.SequenceMatcher(None, token, syn_norm).ratio()
    if len(syn_norm) <= 2 and score > 0.8:
        score = 0.8
    return score


def _best_match_for_synonyms(synonyms: Sequence[str], tokens: Sequence[str], joined_compact: str) -> tuple[float, dict[str, Any] | None]:
    """Return the best score/detail for a synonym list."""
    type_best = 0.0
    best_detail: dict[str, Any] | None = None
    for syn in synonyms:
        syn_norm = _compact_text(syn)
        if not syn_norm:
            continue

        if syn_norm in joined_compact:
            if type_best <= 1.0:
                type_best = 1.0
                best_detail = {"synonym": syn_norm, "token": None, "score": 1.0, "source": "joined_text"}
            continue

        for token in tokens:
            score = _score_token_similarity(token, syn_norm)
            if score > type_best:
                type_best = score
                best_detail = {"synonym": syn_norm, "token": token, "score": score, "source": "token"}

    return type_best, best_detail


def _choose_best_type(candidate_types: set[str], joined_text: str, tokens: list[str], default_type: str) -> tuple[str, float, dict[str, dict[str, Any]]]:
    """Pick the best type and metadata from matched tokens."""
    type_synonyms: dict[str, list[str]] = {
        "payment": ["payment", "paid", "receipt", "remittance", "banktransfer", "directdebit", "ddpayment", "cashreceipt"],
        "credit_note": ["creditnote", "credit", "creditmemo", "crn", "cr", "cn"],
        "invoice": ["invoice", "inv", "taxinvoice", "bill"],
    }

    joined_compact = _compact_text(joined_text)

    best_type = default_type
    best_score = 0.0
    type_details: dict[str, dict[str, Any]] = {}

    for doc_type, synonyms in type_synonyms.items():
        if doc_type not in candidate_types:
            continue
        type_best, best_detail = _best_match_for_synonyms(synonyms, tokens, joined_compact)
        if best_detail:
            type_details[doc_type] = best_detail
        if type_best > best_score:
            best_score = type_best
            best_type = doc_type

    return best_type, best_score, type_details


def guess_statement_item_type(raw_row: dict[str, Any], total_entries: dict[str, Any] | None = None, contact_config: ContactConfig | None = None) -> str:
    """Heuristically classify a row as ``invoice``, ``credit_note``, or ``payment``."""
    amount_hint, debit_has_value, credit_has_value, debit_labels, credit_labels = _evaluate_amount_hint(raw_row or {}, total_entries, contact_config)

    candidate_types = _candidate_types_from_hint(amount_hint)

    default_type = _default_type(candidate_types)

    values = (raw_row or {}).values()
    joined_text = " ".join(str(v) for v in values if v)
    if not joined_text.strip():
        logger.debug(
            "Statement item classification",
            best_type=default_type,
            reason="no_text",
            amount_hint=amount_hint,
            debit=debit_has_value,
            credit=credit_has_value,
            debit_labels=debit_labels,
            credit_labels=credit_labels,
        )
        return default_type

    tokens = _extract_tokens(joined_text)
    if not tokens:
        logger.debug(
            "Statement item classification",
            best_type=default_type,
            reason="no_tokens",
            amount_hint=amount_hint,
            debit=debit_has_value,
            credit=credit_has_value,
            debit_labels=debit_labels,
            credit_labels=credit_labels,
        )
        return default_type

    best_type, best_score, type_details = _choose_best_type(candidate_types, joined_text, tokens, default_type)

    min_confidence = {"payment": 0.6, "credit_note": 0.65, "invoice": 0.0}

    best_detail = type_details.get(best_type, {})
    logger.debug(
        "Statement item classification",
        best_type=best_type,
        best_score=round(best_score, 4),
        min_confidence=min_confidence.get(best_type, 0.0),
        amount_hint=amount_hint,
        debit=debit_has_value,
        credit=credit_has_value,
        matched_synonym=best_detail.get("synonym"),
        matched_token=best_detail.get("token"),
        match_source=best_detail.get("source"),
        raw_keys=list((raw_row or {}).keys()),
    )

    if best_score < min_confidence.get(best_type, 0.0):
        return default_type

    return best_type
