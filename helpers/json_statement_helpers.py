"""Module for functions that help the LLM create structured JSON"""

import json
from pathlib import Path
from typing import Any, Dict

def build_statement_prompt(statement_text: str):
    system = (
        "You are an expert Accounts Receivable statement normalizer. "
        "You will be given plain text extracted from a supplier statement (OCR or PDF text). "
        "Return ONLY JSON that strictly matches the provided schema. "
        "Do NOT include markdown, code fences, comments, or any text outside the JSON. "
        "Rules:\n"
        "1) Rows usually start with a date (DD/MM/YYYY or YYYY-MM-DD). Parse to ISO (YYYY-MM-DD).\n"
        "2) Treat spaces or commas in numbers as thousand separators (e.g., '63 624.65' or '63,624.65'). Use '.' as decimal point.\n"
        "3) If a line has both an amount and a running balance, set 'amount' to the transaction amount and 'balance' to the new running balance.\n"
        "4) Infer doc_type using keywords and common patterns:\n"
        "   - 'INV' → 'invoice'\n"
        "   - 'CRN' or 'Credit Note' → 'credit_note'\n"
        "   - 'EFT', 'Payment', 'Bank', 'Transfer' → 'payment'\n"
        "   - 'Reverse', 'Reversal', 'Adj' → 'adjustment'\n"
        "   Otherwise → 'other'\n"
        "5) If a value is missing or ambiguous, use null. Do NOT invent data.\n"
        "6) Multi-line descriptions: join them into a single description for the row when obvious.\n"
        "7) Optional aging buckets may appear (e.g., '105 Days 84 Days ... Current Amount Due'). If present, parse to labels+values.\n"
        "8) No extra keys beyond the schema; additionalProperties=false must be satisfied."
    )

    user = (
        "STATEMENT_TEXT:\n"
        f"{statement_text}\n\n"
        "Return the JSON only."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

STATEMENT_JSON_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "supplier_name": {"type": ["string", "null"], "maxLength": 200},
        "statement_date": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "date": {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"},
                    "reference": {"type": ["string", "null"], "maxLength": 120},
                    "description": {"type": ["string", "null"]},
                    "doc_type": {
                        "type": ["string", "null"],
                        "enum": ["invoice", "credit_note", "payment", "adjustment", "other", None]
                    },
                    "amount": {"type": ["number", "null"]},
                    "balance": {"type": ["number", "null"]},
                    "debit": {"type": ["number", "null"]},
                    "credit": {"type": ["number", "null"]},
                    "currency": {"type": ["string", "null"], "maxLength": 10}
                },
                "required": [
                    "date",
                    "reference",
                    "description",
                    "doc_type",
                    "amount",
                    "balance",
                    "debit",
                    "credit",
                    "currency"
                ]
            }
        },
        "aging": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"}},
                "values": {"type": "array", "items": {"type": "number"}}
            },
            "required": ["labels", "values"]
        },
        "closing_balance": {"type": ["number", "null"]}
    },
    "required": ["supplier_name", "statement_date", "rows", "aging", "closing_balance"]
}
