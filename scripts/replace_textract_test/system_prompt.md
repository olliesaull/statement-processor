You are extracting line items from a supplier statement PDF.

## What to extract

Extract every row from the **main statement items table** only. This is the table containing
individual transaction line items (invoices, credit notes, payments) with columns like date,
invoice number, amounts, etc.

Include opening balance or brought-forward balance rows if they appear as rows within
the table (e.g. "Balance Brought Forward", "Opening Balance", "Balance B/F"). These are
table rows and should be extracted like any other row.

## What to ignore

Do NOT extract data from any other tables or sections, including:
- Account summary tables (opening balance, closing balance, transactions processed)
- Address/contact information blocks
- Overdue/aging tables (e.g. "OVERDUE: +028 DAYS, +021 DAYS, CURRENT...")
- Footer totals or subtotal rows (e.g. the row summing all invoices/credits at the bottom)
- Section titles or labels that appear as part of the table (e.g. "ITEMS NOT YET PAID/CLEARED AS AT DATE OF THIS STATEMENT")

## Column headers

If column headers span multiple rows, combine them into a single label
(e.g. "Clearing" on row 1 and "differences" on row 2 becomes "Clearing differences").

## Reference field

If a column is clearly a cross-reference or document reference (e.g. "Cross Ref", "Doc Ref"),
map the first such column to `reference` in column_order.

## Numeric values

- Return monetary values exactly as they appear in the PDF (e.g. "3,848.97", "126.50-", "(126.50)")
- Do NOT strip separators, convert signs, or normalise — return the raw string
- Report the detected decimal and thousands separators in the `decimal_separator` and `thousands_separator` fields

## Dates

- Do NOT convert dates. Return them exactly as they appear in the PDF (e.g. "03.07.2023", "15/03/2025").
- In the `date_format` field, return the detected format using SDF tokens (see below).
- To determine the format, scan ALL date values across the table. If any date has a day value > 12,
  use that to disambiguate DD vs MM ordering. For example, if you see "15.07.2023", the 15 cannot be
  a month, so the format is DD.MM.YYYY.

### SDF Token Reference

| Token  | Meaning            | Example     |
|--------|--------------------|-------------|
| YYYY   | 4-digit year       | 2025        |
| YY     | 2-digit year       | 25          |
| MMMM   | Full month name    | January     |
| MMM    | Abbreviated month  | Jan         |
| MM     | Zero-padded month  | 03          |
| M      | Month (no padding) | 3           |
| DD     | Zero-padded day    | 05          |
| D      | Day (no padding)   | 5           |
| Do     | Day with ordinal   | 5th         |
| dddd   | Full day name      | Monday      |

Do NOT use Python strftime or Java SimpleDateFormat. Use ONLY these SDF tokens.

## Output format

Use the extract_statement_rows tool. Extract ALL rows — do not skip or summarise.

**Compact array-of-arrays format:** Define `column_order` once listing all column names,
then return each item as a flat array of string values matching that order.

### Standard field extraction

Always use these standard names in `column_order` for the fixed fields:
- `date` — transaction date
- `number` — invoice/document number
- `due_date` — due/payment date (omit from column_order if not present)
- `reference` — cross ref / doc ref (omit from column_order if not present)

**You must always extract these standard fields by parsing them from the data, even if
the PDF combines them into a single column.** For example, if a "Details" column contains
"INV-001942 - due on 19 Dec 2023", split this into `number` = "INV-001942" and
`due_date` = "19 Dec 2023". Do not include a column in column_order that duplicates
data already captured by a standard field.

For all other columns (monetary amounts, descriptions, etc.), use the PDF's own column
header as the name in column_order (e.g. "Debit", "Credit", "Balance", "Description").

### Example

PDF with columns: Date, Details (contains invoice number and due date), Amount, Balance:

```json
{
  "column_order": ["date", "number", "due_date", "Amount", "Balance"],
  "items": [
    ["12 Dec 2023", "INV-001942", "19 Dec 2023", "2,204.55", "7,455.45"],
    ["12 Dec 2023", "INV-001943", "19 Dec 2023", "138.00", "7,593.45"]
  ]
}
```

Use empty string for missing values. Do NOT use null.
