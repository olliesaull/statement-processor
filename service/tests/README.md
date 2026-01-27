# Unit tests (non-Playwright)

This directory contains unit tests that are not UI/end-to-end Playwright tests. The focus is on fast, deterministic checks of pure functions and small helpers.

## Tests

### Statement item classification

- Amount-based hints
  - Debit-only totals classify as `invoice`.
  - Credit-only totals default to `payment` when there is no text.
  - Credit-only totals with "credit note" text classify as `credit_note`.
  - Debit + credit present lets text drive the result (e.g. "invoice").

- Label/format variations
  - Totals provided as dicts with `Debit`/`Credit` labels.
  - Totals provided as list entries (`{label, value}`).
  - Debit/credit inferred from raw row keys.
  - Custom debit/credit labels from contact config.

- Confidence thresholds
  - Short tokens (e.g. "CR") should still pass credit-note thresholds.
  - Low-similarity tokens should fall back to the default type.

## Structure

```
tests/
  __init__.py
  test_item_classification.py
  README.md
```

## Running tests

From `/home/ollie/statement-processor/service`:

- All tests: `python3.13 -m pytest -vv -s --tb=long tests/test_item_classification.py --headed`
- Specific test: `python3.13 -m pytest -vv -s --tb=long tests/test_item_classification.py::test_amount_hint_debit_only_returns_invoice --headed`

If you change any Python files, run the standard checks:

```
make dev
```
