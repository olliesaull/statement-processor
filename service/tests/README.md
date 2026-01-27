# Unit tests (non-Playwright)

This directory contains unit tests that are not UI/end-to-end Playwright tests. The focus is on fast, deterministic checks of pure functions and small helpers.

## Tests

### Statement item classification

- Amount-based hints
  - Debit-only totals classify as `invoice`.
  - Credit-only totals default to `payment` when there is no text.
  - Credit-only totals with "credit note" text classify as `credit_note`.
  - Debit + credit present lets text drive the result (e.g. "invoice").
  - Credit-only totals ignore "invoice" text and stay in credit-type candidates.
  - Debit + credit present with no text defaults to `invoice`.

- Label/format variations
  - Totals provided as dicts with `Debit`/`Credit` labels.
  - Totals provided as list entries (`{label, value}`).
  - Debit/credit inferred from raw row keys.
  - Custom debit/credit labels from contact config.
  - Parenthetical negatives (e.g. `(10.00)`) still count as amounts.
  - Comma-separated numbers (e.g. `1,234.50`) still count as amounts.

- Confidence thresholds
  - Short tokens (e.g. "CR") should still pass credit-note thresholds.
  - Low-similarity tokens should fall back to the default type.
  - Payment wins when "payment" and "credit note" are both present.
  - Compound text like "CreditNote123" should still match credit-note synonyms.

### Formatting utilities

- Date formatting (contact config)
  - Configured templates are returned and used for display formatting.
  - ISO inputs are reformatted to the configured template.
  - Unparseable inputs are preserved.
  - Ordinal days (e.g. `1st Jan 2024`) are supported when configured.
  - Full month names (e.g. `March 5 2024`) are supported when configured.
  - Two-digit year templates preserve the `YY` output format.
  - Missing date formats fall back to ISO output.

- Number formatting (contact config)
  - EU separators (e.g. `1.234,50`) are parsed and formatted correctly.
  - Space thousands separators (e.g. `1 234.5`) are parsed and formatted correctly.
  - Invalid config separators fall back to defaults.
  - Mismatched statement separators remain unchanged (documented current behavior).
  - Empty and None values render as blank strings.
  - Non-numeric placeholders (e.g. `N/A`) are preserved.

## Structure

```
tests/
  conftest.py
  __init__.py
  test_item_classification.py
  test_statement_view_formatting.py
  README.md
```

## Test setup

- `tests/conftest.py` stubs the `config` module logger so importing helpers does not hit AWS SSM.
  This keeps unit tests fast and offline-friendly while still allowing structured log kwargs.

## Running tests

From `/home/ollie/statement-processor/service`:

- All tests: `python3.13 -m pytest -vv -s --tb=long tests/test_item_classification.py --headed`
- Specific test: `python3.13 -m pytest -vv -s --tb=long tests/test_item_classification.py::test_amount_hint_debit_only_returns_invoice --headed`

If you change any Python files, run the standard checks:

```
make dev
```
