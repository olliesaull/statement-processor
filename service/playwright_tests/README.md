Playwright end-to-end tests
===========================

These tests exercise the full workflow: seed a test login session, configure a contact mapping, upload a statement PDF, validate the statement table, and validate Excel exports.

Structure
---------
- `playwright_tests/helpers/`: reusable actions (login, config, table parsing, Excel parsing).
- `playwright_tests/tests/e2e/test_statement_flow.py`: end-to-end flows.
- `playwright_tests/fixtures/test_runs.json`: per-run config.
- `playwright_tests/fixtures/expected/`: baseline Excel exports.

Setup
-----
1) Update `playwright_tests/fixtures/test_runs.json` with one or more runs.
2) Put statement PDFs in `/statements` (or set `PLAYWRIGHT_STATEMENTS_DIR`).
3) Drop baseline Excel exports in `playwright_tests/fixtures/expected/` and reference them via `expected_excel_filename`.
4) Set `TEST_LOGIN_SECRET` (must match the header used by `/test-login`).
5) Start the Flask app locally (default base URL is `http://localhost:8080`).
6) Install Playwright browsers:
   - `python3.13 -m playwright install`

Optional environment variables
------------------------------
- `PLAYWRIGHT_BASE_URL` (default `http://localhost:8080`)
- `PLAYWRIGHT_STATEMENTS_DIR` (default `/statements`)
- `PLAYWRIGHT_TEST_LOGIN_HEADER` (default `X-Test-Auth`)
- `PLAYWRIGHT_STATEMENT_WAIT_SECONDS`, `PLAYWRIGHT_STATEMENT_MAX_REFRESHES`

Run
---
`python3.13 -m pytest playwright_tests/tests/e2e/test_statement_flow.py`

Notes
-----
- The tests upload a file by path (no OS file picker), so they work from WSL without opening files on the host.
- The statements view uses cached datasets; ensure the contact exists in cached contacts for the chosen tenant.
- Tests delete any existing statement for the contact to reset state between runs.
- Each entry in `test_runs.json` maps to the `StatementFlowRun` fields (for example: `tenant_id`, `contact_name`, `statement_filename`, `expected_excel_filename`).
- Required run fields: `tenant_id`, `tenant_name`, `contact_name`, `number_column`, `date_column`, `total_column` (array), `date_format`, `statement_filename`.
