Playwright flow test
====================

This test exercises the end-to-end flow: seed a test login session, create a contact config, upload a statement PDF, open the statement detail view, and verify the table renders.

Setup
-----
1) Create `playwright_tests/test_runs.json` with one or more runs.
2) Place the PDFs referenced by `statement_filename` alongside the JSON file.
3) Set `TEST_LOGIN_SECRET` (must match the header used by `/test-login`).
4) Start the Flask app locally (default base URL is `http://localhost:8080`).
5) Install Playwright browsers:
   - `python3.13 -m playwright install`

Optional environment variables
------------------------------
- `PLAYWRIGHT_BASE_URL` (default `http://localhost:8080`)
- `PLAYWRIGHT_STATEMENT_WAIT_SECONDS`, `PLAYWRIGHT_STATEMENT_MAX_REFRESHES`

Run
---
`python3.13 -m pytest playwright_tests/test_statement_flow.py`

Notes
-----
- The test uploads a file by path (no OS file picker), so it works from WSL without opening files on the host.
- The statements view uses cached datasets; ensure the contact exists in cached contacts for the chosen tenant.
- Each entry in `test_runs.json` maps to the `StatementFlowRun` fields (for example: `tenant_id`, `contact_name`, `statement_filename`, `expected_table_text`).
- Required run fields: `tenant_id`, `tenant_name`, `contact_name`, `number_column`, `date_column`, `total_column` (array), `date_format`, `statement_filename`.
