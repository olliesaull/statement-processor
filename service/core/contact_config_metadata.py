from typing import Dict, List

FIELD_DESCRIPTIONS: Dict[str, str] = {
    "amount_due": (
        "One or more statement columns that represent the amount due for an item (total - amount paid). "
        "If there are separate debit and credit columns, add both."
    ),
    "amount_paid": (
        "Column showing payments or credits applied against the item."
    ),
    "date": (
        "Transaction date as it appears on the statement."
    ),
    "due_date": (
        "Payment due date from the statement line."
    ),
    "number": (
        "Document number on the statement (e.g. invoice number). This is the primary key used when matching to Xero. "
        "If the column contains items that are not a direct match to the number in Xero, that's okay as seen in the example."
    ),
    "reference": (
        "Any descriptive text that helps identify the transaction (project, memo, etc.)."
    ),
    "date_format": (
        "Date pattern (e.g., 'D MMMM YYYY', 'MM/DD/YY'). See the guide below for full token descriptions and examples."
    ),
    "total": (
        "The total due for an item (does not include any payments made)."
    ),
}

EXAMPLE_CONFIG: Dict[str, str | List[str]] = {
    "amount_due": ["debit", "credit"],
    "amount_paid": "",
    "date": "date",
    "due_date": "",
    "number": "reference",
    "reference": "description",
    "date_format": "YYYY-MM-DD",
    "total": "",
}
