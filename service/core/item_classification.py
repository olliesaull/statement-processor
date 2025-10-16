from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List


def guess_statement_item_type(raw_row: Dict[str, Any]) -> str:
    """Heuristically classify a row as ``invoice``, ``credit_note``, or ``payment``."""
    values = (raw_row or {}).values()
    joined_text = " ".join(str(v) for v in values if v)
    if not joined_text.strip():
        return "invoice"

    def _compact(s: str) -> str:
        return "".join(ch for ch in str(s or "").upper() if ch.isalnum())

    tokens = [_compact(tok) for tok in re.findall(r"[A-Za-z0-9]+", joined_text.upper())]
    tokens = [t for t in tokens if t]
    if not tokens:
        return "invoice"

    type_synonyms: Dict[str, List[str]] = {
        "payment": [
            "payment",
            "receipt",
            "paid",
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

    best_type = "invoice"
    best_score = 0.0

    for doc_type, synonyms in type_synonyms.items():
        type_best = 0.0
        for syn in synonyms:
            syn_norm = _compact(syn)
            if not syn_norm:
                continue

            if syn_norm in joined_compact:
                type_best = max(type_best, 1.0)
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

        if type_best > best_score:
            best_score = type_best
            best_type = doc_type

    min_confidence = {
        "payment": 0.6,
        "credit_note": 0.65,
        "invoice": 0.0,
    }

    if best_score < min_confidence.get(best_type, 0.0):
        return "invoice"

    return best_type

