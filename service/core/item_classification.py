"""
Heuristics for classifying statement rows.

The classifier uses:
- Configured column labels (from contact mapping config)
- Presence of debit/credit values in a row
- Text matching against synonym lists (invoice/credit/payment)

It is intentionally best-effort and falls back to a default type when evidence is weak.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from config import logger

_DEBIT_HINTS: tuple[str, ...] = ("debit", "dr")
_CREDIT_HINTS: tuple[str, ...] = ("credit", "cr")
_NUMERIC_CHARS_RE = re.compile(r"[^0-9\-\.,()]")


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
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
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
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _has_amount(value: Any) -> bool:
    """Return True when a value parses to a non-zero Decimal."""
    dec = _coerce_decimal(value)
    return dec is not None and dec != 0


def _extract_total_template(contact_config: dict[str, Any] | None) -> Any:
    """Locate the `total` configuration template in the contact config payload."""
    if not isinstance(contact_config, dict):
        return None
    statement_items = contact_config.get("statement_items")
    if isinstance(statement_items, dict):
        source = statement_items
    elif isinstance(statement_items, list) and statement_items and isinstance(statement_items[0], dict):
        source = statement_items[0]
    else:
        source = contact_config
    if not isinstance(source, dict):
        return None
    return source.get("total")


def _collect_config_amount_labels(
    contact_config: dict[str, Any] | None,
) -> tuple[set[str], set[str]]:
    """Collect normalized debit/credit labels from contact config."""
    total_cfg = _extract_total_template(contact_config)
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

    if isinstance(total_cfg, dict):
        for key, value in total_cfg.items():
            key_norm = _normalize_label(key)
            if key_norm.startswith("debit") or key_norm in {"dr"}:
                bucket = "debit"
            elif key_norm.startswith("credit") or key_norm in {"cr"}:
                bucket = "credit"
            else:
                bucket = None
            labels = _flatten_labels(value)
            if not labels:
                labels = [key]
            _record(labels, bucket)
    else:
        labels = _flatten_labels(total_cfg)
        _record(labels, None)

    return debit_norms, credit_norms


def _iter_total_entries(total_entries: Any) -> Iterable[tuple[str, Any]]:
    """Yield (label, value) pairs from dict or list-style total data."""
    if isinstance(total_entries, dict):
        for label, value in total_entries.items():
            if isinstance(label, str):
                yield label, value
    elif isinstance(total_entries, list):
        for item in total_entries:
            if isinstance(item, dict):
                label = item.get("label") or item.get("header") or item.get("name")
                if isinstance(label, str):
                    yield label, item.get("value")


def _evaluate_amount_hint(
    raw_row: dict[str, Any],
    total_entries: Any,
    contact_config: dict[str, Any] | None,
) -> tuple[str | None, bool, bool, list[str], list[str]]:
    """
    Infer debit/credit signals and return a type hint plus evidence details.

    Returns:
        (amount_hint, debit_has_value, credit_has_value, debit_labels_used, credit_labels_used)
    """
    debit_norms, credit_norms = _collect_config_amount_labels(contact_config)

    for key in raw_row or {}:
        norm = _normalize_label(key)
        if _is_debit_norm(norm):
            debit_norms.add(norm)
        elif _is_credit_norm(norm):
            credit_norms.add(norm)

    debit_has_value = False
    credit_has_value = False
    debit_labels_used: list[str] = []
    credit_labels_used: list[str] = []

    for label, value in _iter_total_entries(total_entries):
        norm = _normalize_label(label)
        category: str | None = None
        if norm in debit_norms:
            category = "debit"
        elif norm in credit_norms:
            category = "credit"
        else:
            if _is_debit_norm(norm):
                debit_norms.add(norm)
                category = "debit"
            elif _is_credit_norm(norm):
                credit_norms.add(norm)
                category = "credit"

        if category == "debit":
            if _has_amount(value):
                debit_has_value = True
                debit_labels_used.append(label)
        elif category == "credit" and _has_amount(value):
            credit_has_value = True
            credit_labels_used.append(label)

    if not debit_has_value:
        inverse_raw = {_normalize_label(key): (key, val) for key, val in (raw_row or {}).items() if isinstance(key, str)}
        for norm in debit_norms:
            match = inverse_raw.get(norm)
            if match and _has_amount(match[1]):
                debit_has_value = True
                debit_labels_used.append(match[0])
                break

    if not credit_has_value:
        inverse_raw = {_normalize_label(key): (key, val) for key, val in (raw_row or {}).items() if isinstance(key, str)}
        for norm in credit_norms:
            match = inverse_raw.get(norm)
            if match and _has_amount(match[1]):
                credit_has_value = True
                credit_labels_used.append(match[0])
                break

    if debit_has_value and not credit_has_value:
        return "invoice", True, False, debit_labels_used, credit_labels_used
    if credit_has_value and not debit_has_value:
        return "credit", False, True, debit_labels_used, credit_labels_used

    return (
        None,
        debit_has_value,
        credit_has_value,
        debit_labels_used,
        credit_labels_used,
    )


def _default_type(candidate_types: set[str]) -> str:
    """Pick the first preferred type from the candidate set."""
    for option in ("invoice", "payment", "credit_note"):
        if option in candidate_types:
            return option
    return "invoice"


def guess_statement_item_type(
    raw_row: dict[str, Any],
    total_entries: dict[str, Any] | None = None,
    contact_config: dict[str, Any] | None = None,
) -> str:
    """Heuristically classify a row as ``invoice``, ``credit_note``, or ``payment``."""
    amount_hint, debit_has_value, credit_has_value, debit_labels, credit_labels = _evaluate_amount_hint(raw_row or {}, total_entries, contact_config)

    candidate_types: set[str] = {"invoice", "credit_note", "payment"}
    if amount_hint == "invoice":
        candidate_types = {"invoice"}
    elif amount_hint == "credit":
        candidate_types = {"credit_note", "payment"}

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

    def _compact(s: str) -> str:
        return "".join(ch for ch in str(s or "").upper() if ch.isalnum())

    tokens = [_compact(tok) for tok in re.findall(r"[A-Za-z0-9]+", joined_text.upper())]
    tokens = [t for t in tokens if t]
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

    type_synonyms: dict[str, list[str]] = {
        "payment": [
            "payment",
            "paid",
            "receipt",
            "remittance",
            "banktransfer",
            "directdebit",
            "ddpayment",
            "cashreceipt",
        ],
        "credit_note": ["creditnote", "credit", "creditmemo", "crn", "cr", "cn"],
        "invoice": ["invoice", "inv", "taxinvoice", "bill"],
    }

    joined_compact = _compact(joined_text)

    best_type = default_type
    best_score = 0.0
    type_details: dict[str, dict[str, Any]] = {}

    for doc_type, synonyms in type_synonyms.items():
        if doc_type not in candidate_types:
            continue
        type_best = 0.0
        for syn in synonyms:
            syn_norm = _compact(syn)
            if not syn_norm:
                continue

            if syn_norm in joined_compact:
                if type_best <= 1.0:
                    type_best = 1.0
                    type_details[doc_type] = {
                        "synonym": syn_norm,
                        "token": None,
                        "score": 1.0,
                        "source": "joined_text",
                    }
                continue

            for token in tokens:
                if token == syn_norm:
                    score = 1.0
                elif token.startswith(syn_norm) or syn_norm.startswith(token):
                    score = 0.9
                else:
                    score = difflib.SequenceMatcher(None, token, syn_norm).ratio()

                if len(syn_norm) <= 2 and score > 0.8:
                    score = 0.8

                if score > type_best:
                    type_best = score
                    type_details[doc_type] = {
                        "synonym": syn_norm,
                        "token": token,
                        "score": score,
                        "source": "token",
                    }

        if type_best > best_score:
            best_score = type_best
            best_type = doc_type

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
