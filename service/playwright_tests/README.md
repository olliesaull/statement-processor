Playwright end-to-end tests
===========================

These tests exercise the full workflow: login via Xero OAuth, configure a contact mapping, upload a statement PDF, validate the statement table, and validate Excel exports.

Structure
---------
- `playwright_tests/helpers/`: reusable actions (login, config, table parsing, Excel parsing).
- `playwright_tests/tests/e2e/test_statement_flow.py`: end-to-end flows.
- `playwright_tests/fixtures/test_runs.json`: per-run config.
- `playwright_tests/fixtures/expected/`: baseline Excel exports.

Setup
-----
1) Update `playwright_tests/fixtures/test_runs.json` with one or more runs.
2) Put statement PDFs in `./statements`.
3) Drop baseline Excel exports in `playwright_tests/fixtures/expected/` and reference them via `expected_excel_filename`.
4) Set `XERO_EMAIL` and be ready to type it when prompted.
5) Start the Flask app locally.
6) Install Playwright browsers:
   - `python3.13 -m playwright install`

Optional environment variables
------------------------------
- `XERO_EMAIL`

Run
---
`python3.13 -m pytest -vv -s --tb=long playwright_tests/tests/e2e/test_statement_flow.py --headed`


Notes
-----
- The tests upload a file by path (no OS file picker), so they work from WSL without opening files on the host.
- The statements view uses cached datasets; ensure the contact exists in cached contacts for the chosen tenant.
- Tests delete any existing statement for the contact to reset state between runs.
- The login flow is real Xero OAuth; run in headed mode and complete any MFA prompts when asked.
- Each entry in `test_runs.json` maps to the `StatementFlowRun` fields (for example: `tenant_id`, `contact_name`, `statement_filename`, `expected_excel_filename`).
- Required run fields: `tenant_id`, `tenant_name`, `contact_name`, `number_column`, `date_column`, `total_column` (array), `date_format`, `statement_filename`.
