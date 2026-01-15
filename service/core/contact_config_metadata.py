FIELD_DESCRIPTIONS: dict[str, str] = {
    "date": ("Transaction date as it appears on the statement."),
    "due_date": ("Payment due date from the statement line. If you do not care about due date, leave it blank."),
    "number": (
        "Document number on the statement (e.g. invoice number). This is the primary key used when matching to Xero. "
        "If the column contains items that are not a direct match to the number in Xero, that's okay as seen in the example."
    ),
    "date_format": ("Date pattern (e.g., 'D MMMM YYYY', 'MM/DD/YY'). See the guide below for full token descriptions and examples."),
    "total": ("One or more statement columns that represent the total outstanding for an item. If there are separate debit and credit columns, add both."),
}

EXAMPLE_CONFIG: dict[str, str | list[str]] = {
    "date": "date",
    "due_date": "",
    "number": "reference",
    "date_format": "YYYY-MM-DD",
    "total": ["debit", "credit"],
    "decimal_separator": ".",
    "thousands_separator": ",",
}
