# Playwright end-to-end tests

These tests exercise the full workflow: login via Xero OAuth, configure a contact mapping, upload a statement PDF, validate the statement table, and validate Excel exports.

## Structure
- `playwright_tests/helpers/`: reusable actions (login, config, table parsing, Excel parsing).
- `playwright_tests/tests/e2e/test_statement_flow.py`: end-to-end flows.
- `playwright_tests/fixtures/test_runs.json`: per-run config.
- `playwright_tests/fixtures/expected/`: baseline Excel exports.

## Tests
- `test_config_upload_ui_validation`: Validates the statement table UI against the expected Excel baseline for each configured run.
- `test_ui_actions_excel_export_validation`: Exercises UI actions (complete an item, show payments) and asserts the Excel export reflects the changes.
- `test_tenant_switching_updates_contacts`: Switches tenants and verifies the active badge and contact list update accordingly.

## Setup
1) Update `playwright_tests/fixtures/test_runs.json` with one or more runs.
2) Put statement PDFs in `./statements`.
3) Drop baseline Excel exports in `playwright_tests/fixtures/expected/` and reference them via `expected_excel_filename`.
4) Set `XERO_EMAIL` and be ready to type it when prompted.
5) Start the Flask app locally.
6) Install Playwright browsers:
   - `python3.13 -m playwright install`

## Demo Company (UK) regression fixture: Test Statements Ltd
This run is designed to lock in the refactor baseline for the multi-scenario statement PDF. Demo Company data can be reset by Xero, so the PDF + Xero docs should be re-seeded when that happens.

### One-time (or after Demo Company reset)
1) Generate the test PDF:
   - `python3.13 statement-processor/scripts/generate_example_pdf/create_test_pdf.py`
2) Copy the PDF into the Playwright statements folder:
   - `test_pdf.pdf` → `statement-processor/service/playwright_tests/statements/test_statements_ltd.pdf`
3) Upload the PDF via the UI:
   - Log in and switch to tenant **Demo Company (UK)**.
   - Upload `test_statements_ltd.pdf` for contact **Test Statements Ltd**.
4) Ensure the contact config matches the PDF headers:
   - Number column: `reference`
   - Date column: `date`
   - Total columns: `debit`, `credit`
   - Date format: `YYYY-MM-DD`
5) Populate Xero from the extracted JSON:
   - Run `python3.13 statement-processor/scripts/populate_xero/populate_xero.py`.
   - The script defaults to the Demo Company (UK) tenant and the Test Statements Ltd statement/contact IDs; override via env vars if needed (`TENANT_ID`, `STATEMENT_ID`, `CONTACT_ID`).
6) Capture the baseline Excel export:
   - Open the statement detail view and click “Download Excel”.
   - Save it as `service/playwright_tests/fixtures/expected/test_statements_ltd.xlsx`.

### Notes
- The populate script intentionally skips “no match”, “balance forward”, and invalid date rows to preserve mismatch scenarios in the UI.
- If Demo Company resets, repeat the steps above (PDF generation is deterministic, so re-running it is safe).

## Optional environment variables
- `XERO_EMAIL`

## Running tests

- All tests: `python3.13 -m pytest -vv -s --tb=long playwright_tests/tests/e2e/test_statement_flow.py --headed`
- Specific test: `python3.13 -m pytest -vv -s --tb=long playwright_tests/tests/e2e/test_statement_flow.py::test_tenant_switching_updates_contacts --headed`


## Notes
- The tests upload a file by path (no OS file picker), so they work from WSL without opening files on the host.
- The statements view uses cached datasets; ensure the contact exists in cached contacts for the chosen tenant.
- Tests delete any existing statement for the contact to reset state between runs.
- Statement uploads are cached per pytest session; repeat tests for the same run reuse the first upload to avoid extra Textract costs.
- The login flow is real Xero OAuth; run in headed mode and complete any MFA prompts when asked.
- Each entry in `test_runs.json` maps to the `StatementFlowRun` fields (for example: `tenant_id`, `contact_name`, `statement_filename`, `expected_excel_filename`).
- Required run fields: `tenant_id`, `tenant_name`, `contact_name`, `number_column`, `date_column`, `total_column` (array), `date_format`, `statement_filename`.
