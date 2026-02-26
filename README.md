# Statement Processor

## Repository Structure
- Major domains (top‑level directories):
  - `cdk/`: Infrastructure‑as‑code for provisioning AWS resources (CDK app and stacks).
  - `lambda_functions/`: Lambda/container workloads; currently hosts the Textraction Lambda codebase.
  - `service/`: Flask service, web assets, and service‑level tests.
  - `scripts/`: One‑off utilities and operational scripts (e.g. data maintenance, sample artefacts).
- Shared libraries (internal to this repo):
  - `service/core` and `service/utils`: shared domain logic and helpers for the Flask service.
  - `lambda_functions/textraction_lambda/core`: shared extraction, transformation, and validation logic for the Textraction Lambda.
- Directory tree (excluding `.git`, `venv`, `__pycache__`, `node_modules`, and build artefacts):
```text
.
├── AGENTS.md
├── Makefile
├── README.md
├── update_dependencies.sh
├── cdk/
│   ├── app.py
│   ├── requirements.txt
│   └── stacks/
│       ├── __init__.py
│       └── statement_processor.py
├── lambda_functions/
│   └── textraction_lambda/
│       ├── Dockerfile
│       ├── config.py
│       ├── exceptions.py
│       ├── logger.py
│       ├── main.py
│       ├── pyproject.toml
│       ├── requirements.txt
│       ├── requirements-dev.txt
│       ├── run_static_checks.sh
│       ├── core/
│       │   ├── __init__.py
│       │   ├── date_utils.py
│       │   ├── extraction.py
│       │   ├── get_contact_config.py
│       │   ├── models.py
│       │   ├── textract_statement.py
│       │   ├── transform.py
│       │   └── validation/
│       │       ├── __init__.py
│       │       ├── anomaly_detection.py
│       │       └── validate_item_count.py
│       └── tests/
├── scripts/
│   ├── add_config_to_ddb/
│   │   ├── requirements.txt
│   │   └── temp_add_config_to_ddb.py
│   ├── check_duplicate_invoice_ids/
│   │   └── check_duplicate_invoice_ids.py
│   ├── clear_ddb_and_s3/
│   │   ├── clear_ddb_and_s3.py
│   │   └── requirements.txt
│   ├── generate_example_pdf/
│   │   ├── create_example_pdf.py
│   │   ├── create_test_pdf.py
│   │   ├── move_statement_img.sh
│   │   ├── requirements.txt
│   │   └── sample_statement.png
│   └── populate_xero/
│       ├── populate_xero.py
│       └── requirements.txt
└── service/
    ├── app.py
    ├── config.py
    ├── Dockerfile
    ├── logger.py
    ├── pyproject.toml
    ├── pytest.ini
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── run_as_container.sh
    ├── sync.py
    ├── tenant_data_repository.py
    ├── xero_repository.py
    ├── core/
    │   ├── __init__.py
    │   ├── contact_config_metadata.py
    │   ├── date_utils.py
    │   ├── get_contact_config.py
    │   ├── item_classification.py
    │   └── models.py
    ├── playwright_tests/
    │   ├── helpers/
    │   └── tests/
    ├── static/
    │   └── assets/
    ├── templates/
    ├── tests/
    └── utils/
        ├── __init__.py
        ├── auth.py
        ├── dynamo.py
        ├── formatting.py
        ├── statement_view.py
        ├── storage.py
        ├── tenant_status.py
        └── workflows.py
```

## Major constructs and resources (from `cdk/stacks/statement_processor.py`)
- **DynamoDB tables**
  - `TenantStatementsTable` (`tenant_statements_table`): statement‑level records; GSIs `TenantIDCompletedIndex` and `TenantIDStatementItemIDIndex` support filtering by completion status and per‑item lookups (see inline comments).
  - `TenantContactsConfigTable` (`tenant_contacts_config_table`): shared table wired into both App Runner and the Textraction Lambda via env vars and IAM grants, so it acts as shared per‑tenant configuration/state (details of contents TODO (needs verification)).
  - `TenantDataTable` (`tenant_data_table`): shared tenant data table wired into both App Runner and the Textraction Lambda via env vars and IAM grants (details of contents TODO (needs verification)).
- **S3 bucket**
  - `dexero-statement-processor-{stage}` (`s3_bucket`): shared object store referenced by both App Runner and the Textraction Lambda; includes an explicit bucket policy to allow Textract to read statement PDFs.
- **Lambda**
  - `TextractionLambda` (`textraction_lambda`): container‑image Lambda built from `lambda_functions/textraction_lambda` to perform statement extraction; invoked by the Step Functions state machine (`ProcessStatement` task).
  - `TextractionLambda` and `StatementProcessorWebLambda` are explicitly configured for `arm64`, and their Docker image assets are built as `linux/arm64` to avoid architecture mismatches and improve cold-start efficiency on Graviton.
  - `TextractionLambdaLogGroup` (`textraction_log_group`): explicit log group with stage‑dependent retention (3 months in prod, 1 week otherwise).
- **Step Functions**
  - `TextractionStateMachine` (`state_machine`): orchestrates `StartTextractDocumentAnalysis` → `WaitForTextract` → `GetTextractStatus` → `ProcessStatement`, with success/failure handling for Textract job status.
- **App Runner**
  - `Statement Processor Website` (`web`): App Runner service built from `service/` (`AppRunnerImage`) to run the Flask service; uses an instance role to access DynamoDB, S3, Textract, and Step Functions.
- **IAM roles and policies**
  - `Statement Processor App Runner Instance Role` (`statement_processor_instance_role`): grants App Runner access to CloudWatch metrics, Textract, and Step Functions; table and S3 permissions are added via grants.
  - Web Lambda runtime no longer requires `ssm:GetParameter`/`kms:Decrypt` for Xero/session secrets; `cdk/deploy_stack.sh` reads SSM secure parameters before deploy and passes them into CDK as deploy-time environment variables for Lambda. This removes per-cold-start SSM/KMS network calls from the Flask service startup path.
  - Textract permissions added to both Lambda and state machine roles to allow `StartDocumentAnalysis` and `GetDocumentAnalysis`.
- **CloudWatch + SNS**
  - `StatementProcessorAppRunnerErrorMetricFilter` + `StatementProcessorAppRunnerErrorAlarm`: parses App Runner application logs for `ERROR` and raises an alarm.
  - `StatementProcessorAppRunnerErrorTopic`: SNS topic with email subscriptions for alarm notifications.

## Orchestration (Step Functions & Textract)
**State machine definitions and entry points**
- `TextractionStateMachine` is defined in `cdk/stacks/statement_processor.py` as a single chainable state machine built from `StartTextractDocumentAnalysis` -> `WaitForTextract` -> `GetTextractStatus` -> `IsTextractFinished?` -> `ProcessStatement` or `TextractFailed`.
- Executions are started from the Flask service via `service/utils/workflows.py:start_textraction_state_machine`, invoked during upload in `service/app.py:_process_statement_upload`.

**Step-by-step flow (code-grounded)**
1. Upload handler registers statement metadata and starts the state machine (`service/app.py:_process_statement_upload` -> `service/utils/workflows.py:start_textraction_state_machine`).
2. Step Functions calls Textract `startDocumentAnalysis` with the S3 PDF location (`StartTextractDocumentAnalysis` in `cdk/stacks/statement_processor.py`).
3. Workflow waits 10 seconds (`WaitForTextract`).
4. Workflow calls `getDocumentAnalysis` to check `JobStatus` (`GetTextractStatus`).
5. If status is `SUCCEEDED` or `PARTIAL_SUCCESS`, invoke `TextractionLambda` with job id + S3 keys (`ProcessStatement`).
6. If status is `FAILED`, transition to `TextractFailed` (explicit failure).
7. Otherwise, loop back to wait and poll again until timeout.
8. Lambda retrieves paginated Textract results, builds statement JSON, persists items, and writes JSON to S3 (`lambda_functions/textraction_lambda/core/extraction.py` + `lambda_functions/textraction_lambda/core/textract_statement.py`).
9. `lambda_functions/textraction_lambda/main.py` returns a compact metadata payload (IDs, `jsonKey`, filename/date/item summary) instead of embedding the full statement JSON in state output; the full artifact is read from S3 to avoid Step Functions state-size limits.

## Flask Service

- **App structure**
  - Main application: `service/app.py` (Flask app factory, route handlers, template rendering, orchestration).
  - Templates and UI assets: `service/templates/` (Jinja2 views) and `service/static/` (static assets).
  - Configuration + AWS clients: `service/config.py` (environment-variable loading, boto3 clients/resources).
  - Logging: `service/logger.py` (structured logger used across modules).
  - Session/auth wiring: encrypted chunked cookie session config in `service/app.py` (custom Flask `SessionInterface`, no Flask-Session/Redis/ElastiCache dependency).
    - Tenant sync-status checks are read directly from DynamoDB via `service/utils/tenant_status.py` for consistent cross-instance behavior.

- **Main modules/packages**
  - `service/core/`: domain models and mapping logic (e.g. `contact_config_metadata.py`, `get_contact_config.py`, `item_classification.py`, `models.py`).
  - `service/utils/`: cross‑cutting utilities:
    - Auth/session helpers: `service/utils/auth.py`
    - DynamoDB access: `service/utils/dynamo.py`
    - S3 keying + uploads: `service/utils/storage.py`
    - Step Functions start: `service/utils/workflows.py`
    - Statement view/matching logic: `service/utils/statement_view.py`
    - Statement Excel export assembly: `service/utils/statement_excel_export.py`
      - Builds XLSX payload bytes, worksheet styling (match/mismatch/anomaly + completed variants), mismatch borders, legend sheet, and download filename metadata.
      - `service/app.py` now calls this module and only wraps the payload in a Flask response.
    - Shared statement row helpers: `service/utils/statement_rows.py`
      - Centralizes row item-type labeling (`invoice` -> `INV`, etc.) and Xero ID lookup from matched row payloads.
      - Reused by both HTML row-building and Excel export paths to keep link/label behavior aligned.
    - Formatting/helpers: `service/utils/formatting.py`, `service/utils/tenant_status.py`
  - Xero integration + caching: `service/xero_repository.py`
  - Background sync job: `service/sync.py`
  - Tenant metadata: `service/tenant_data_repository.py`
  - Tests: `service/tests/`, `service/playwright_tests/`

- **Stage-aware local cache path** (`service/config.py`)
  - `LOCAL_DATA_DIR` is the base directory for cached Xero datasets (`contacts.json`, `invoices.json`, `payments.json`, `credit_notes.json`) that are written by `service/sync.py` and read by `service/xero_repository.py`.
  - When `STAGE` is `dev` or `local`, the base path is `./tmp/data` (relative to the current working directory, typically `service/`), so tenant files land under `service/tmp/data/<tenant_id>/...`.
  - For any other stage (including prod), the base path is `/tmp/data` on the host filesystem.
  - If a local dataset is missing, `service/xero_repository.py:load_local_dataset` downloads from S3 and stores it under the same base path.

- **Key routes/endpoints and purpose** (all in `service/app.py`)
  - **Core UI**
    - `/` (GET): landing page (`index`).
    - `/cookies` (GET): cookie policy + consent page for essential cookies.
    - `/tenant_management` (GET): tenant picker/overview (requires Xero auth via `@xero_token_required`).
    - `/upload-statements` (GET/POST): upload PDFs and trigger textraction (requires tenant + Xero auth, blocks while loading).
    - `/statements` (GET): list and sort statements (requires tenant + Xero auth, blocks while loading).
    - `/statement/<statement_id>` (GET/POST): statement detail view, completion toggles, and XLSX export (requires tenant + Xero auth, blocks while loading).
    - `/statement/<statement_id>/delete` (POST): delete statement + artefacts (requires tenant + Xero auth, blocks while loading).
    - `/configs` (GET/POST): contact mapping configuration UI (requires tenant + Xero auth, blocks while loading).
    - `/instructions` (GET): instructions page.
    - `/about` (GET): non-technical overview page covering product purpose, use cases, outcomes, and practical limits.
    - **Shared statement row colour system (UI + Excel)**:
      - Canonical source: `service/core/statement_row_palette.py`.
      - Base states are `match`, `mismatch`, and `anomaly` with existing row colours.
      - Completed colours are generated (not hard-coded) by blending each base background toward white via `STATEMENT_ROW_COMPLETED_ALPHA` (default `0.65`), so tuning one value updates both UI and XLSX output.
      - Completed text colours intentionally stay the same as normal text colours so completed rows remain readable (no text fade).
      - Flask exposes palette-derived CSS custom properties globally via `service/app.py:_inject_statement_row_palette_css`, and `service/templates/base.html` writes them to `:root`.
      - Statement table CSS (`service/static/assets/css/main.css`) consumes those variables for both normal and completed rows.
      - Excel export (`service/utils/statement_excel_export.py`) builds fills from the same palette (`_build_excel_state_fills`) and applies normal vs completed variants per row based on statement item completion status.
  - **Tenant APIs**
    - `/api/tenant-statuses` (GET): returns tenant sync statuses for polling UI.
    - `/api/tenants/<tenant_id>/sync` (POST): triggers background Xero sync for a tenant.
    - **Auth behavior for API routes**: When `@xero_token_required` protects a `/api/...` endpoint and the session token is missing or expired, the decorator returns `401` JSON (`{"error": "auth_required"}`) instead of redirecting. The frontend polling/sync code (`service/static/assets/js/main.js`) treats either a 401 response or a redirected login response as a signal to navigate to `/login`, so passive actions still force a full re-login.
  - **Auth**
    - `/login` (GET): start Xero OAuth flow.
    - `/callback` (GET): OAuth callback (token validation + tenant load).
    - `/logout` (GET): clear session.
    - `/tenants/select` (POST): set active tenant in session.
    - `/tenants/disconnect` (POST): disconnect tenant from Xero.
    - **Cookie consent gate**: Protected routes and `/login` require the browser cookie `cookie_consent=true`. If consent is missing, UI routes redirect to `/cookies`; API routes return `401` JSON with `{"error": "cookie_consent_required", "redirect": "/cookies"}`.
    - **Session-state UI cookie**: Authenticated UI responses set `session_is_set=true` (short-lived helper cookie) so frontend JavaScript can toggle the final navbar item between `Login` and `Logout` without template-time session checks.
    - **Encrypted chunked auth-session cookies**:
      - Backend/session store is `service/utils/encrypted_chunked_session.py`, a custom Flask `SessionInterface` that keeps session state entirely in browser cookies (stateless server runtime).
      - Session payload is serialized with Flask `TaggedJSONSerializer`, encrypted/authenticated with Fernet, and split into sibling cookies when needed (`session`, `session.1`, `session.2`, ...).
      - Primary cookie format is `v1.<chunk_count>.<chunk0>`; sibling cookies carry overflow chunks.
      - `SESSION_TTL_SECONDS` (default `900`) is enforced in two places: cookie `Max-Age` and Fernet decrypt-time TTL validation. Expired payloads are rejected server-side even if the browser still sends a stale cookie.
      - Cookie controls are configured in `service/app.py`: `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE='Lax'`, and `SESSION_REFRESH_EACH_REQUEST=True` for rolling expiry behavior.
      - Required auth/session secrets (`XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`, `SESSION_FERNET_KEY`, `FLASK_SECRET_KEY`) are now read directly from environment variables in `service/config.py`.
      - In AWS, `cdk/deploy_stack.sh` resolves those values from SSM secure parameters at deployment and passes them into CDK for Lambda environment injection. This keeps deployment-time secret sourcing while reducing cold-start latency by removing runtime secret fetches.
      - Flask app secret key remains stable across cold starts because it is provided as a fixed environment value rather than generated at runtime.
      - Invalid/missing/tampered chunk sets and oversized payloads fail closed: session opens empty and cookie family is cleared on response, preventing partial/unsafe recovery.
  - **Misc**
    - `/.well-known/<path>` (GET): returns 204 for DevTools probes.

- **Upload processing flow** (from `service/app.py`)
  - `upload_statements` validates file/contact counts, enforces PDF MIME/extension rules (`service/utils/storage.py:is_allowed_pdf`), and verifies a contact config exists (`_ensure_contact_config`).
  - `_process_statement_upload`:
    - Uploads PDF to S3 (`upload_statement_to_s3` → `service/utils/storage.py`).
    - Writes statement metadata to DynamoDB (`add_statement_to_table` → `service/utils/dynamo.py`).
    - Computes JSON output key (`statement_json_s3_key`) and starts Step Functions (`start_textraction_state_machine` → `service/utils/workflows.py`).

## Data Model

**Overview**
- Primary stores are DynamoDB tables for statement data/config/status and S3 for statement artefacts and cached Xero datasets (tables/bucket created in `cdk/stacks/statement_processor.py`).
- The structured statement JSON schema is produced by the Textraction Lambda (`lambda_functions/textraction_lambda/core/textract_statement.py`, `lambda_functions/textraction_lambda/core/models.py`) and consumed by the Flask service (`service/app.py`, `service/utils/storage.py`).

### DynamoDB
**TenantStatementsTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
  - Sort key: `StatementID`
- **GSIs**
  - `TenantIDCompletedIndex` (PK: `TenantID`, SK: `Completed`) used by `service/utils/dynamo.py:get_incomplete_statements` and `get_completed_statements`.
  - `TenantIDStatementItemIDIndex` (PK: `TenantID`, SK: `StatementItemID`) defined in CDK but not referenced in code (TODO (needs verification)).
- **Concept**
  - Single-table pattern storing both statement headers and statement line items.
  - `RecordType` distinguishes row types: `"statement"` for headers (`service/utils/dynamo.py:add_statement_to_table`) and `"statement_item"` for line items (`lambda_functions/textraction_lambda/core/textract_statement.py:_persist_statement_items`).
- **Writers**
  - Statement headers: `service/utils/dynamo.py:add_statement_to_table` (initial record).
  - Item rows + header updates: `lambda_functions/textraction_lambda/core/textract_statement.py` (writes item rows; sets `EarliestItemDate`, `LatestItemDate`, `JobId` on header).
  - Status updates: `service/utils/dynamo.py` (completion flags and item type updates).
- **Readers**
  - `service/utils/dynamo.py` (list statements, read header + item status, delete statement data).
  - `service/app.py` (statement list/detail flows).
  - `lambda_functions/textraction_lambda/core/textract_statement.py` (reads header to preserve completion status during re‑processing).
- **Example header item** (created by `add_statement_to_table`, later updated by the Lambda):
```json
{
  "TenantID": "<tenant_id>",
  "StatementID": "<statement_id>",
  "RecordType": "statement",
  "OriginalStatementFilename": "<filename.pdf>",
  "ContactID": "<contact_id>",
  "ContactName": "<contact_name>",
  "UploadedAt": "2024-01-28T12:34:56+00:00",
  "Completed": "false",
  "JobId": "<textract_job_id>",
  "EarliestItemDate": "YYYY-MM-DD",
  "LatestItemDate": "YYYY-MM-DD"
}
```
- **Example item row** (built from statement JSON; numeric values are normalised to DynamoDB `Decimal` in `_sanitize_for_dynamodb`):
```json
{
  "TenantID": "<tenant_id>",
  "StatementID": "<statement_id>#item-0001",
  "StatementItemID": "<statement_id>#item-0001",
  "ParentStatementID": "<statement_id>",
  "RecordType": "statement_item",
  "Completed": "false",
  "ContactID": "<contact_id>",
  "statement_item_id": "<statement_id>#item-0001",
  "date": "YYYY-MM-DD",
  "due_date": "YYYY-MM-DD",
  "number": "INV-123",
  "reference": "INV-123",
  "item_type": "invoice",
  "total": {"debit": "100.00"},
  "raw": {"<header>": "<cell>"},
  "_flags": ["invalid-date", "ml-outlier"],
  "FlagDetails": {"ml-outlier": {"issues": ["keyword-number"], "details": [], "source": "anomaly_detection"}}
}
```
- `statement_item.total` is now treated as a dict-only `{label: value}` mapping in both service and textraction code paths; legacy list-style totals are no longer supported.

**TenantContactsConfigTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
  - Sort key: `ContactID`
- **Concept**
  - Stores per‑tenant, per‑contact mapping config under the `config` attribute.
  - Config shape is flattened at the root (for example `number`, `total`, `date_format`, `decimal_separator`, `thousands_separator`); nested legacy `statement_items` mappings are no longer read.
- **Writers**
  - Config UI save/load: `service/core/get_contact_config.py`.
  - Raw header persistence during extraction: `lambda_functions/textraction_lambda/core/transform.py:_persist_raw_headers` (via `core/get_contact_config.py:set_contact_config`).
- **Readers**
  - Config UI and upload validation: `service/core/get_contact_config.py`, `service/app.py`.
  - Textraction mapping: `lambda_functions/textraction_lambda/core/get_contact_config.py`.
- **Example item** (based on `service/core/contact_config_metadata.py:EXAMPLE_CONFIG`):
```json
{
  "TenantID": "<tenant_id>",
  "ContactID": "<contact_id>",
  "config": {
    "date": "date",
    "due_date": "",
    "number": "reference",
    "date_format": "YYYY-MM-DD",
    "total": ["debit", "credit"],
    "decimal_separator": ".",
    "thousands_separator": ","
  }
}
```

**TenantDataTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
- **Concept**
  - Tracks tenant‑level sync status and the last successful sync time.
- **Writers**
  - `service/sync.py:update_tenant_status` (sets `TenantStatus`, `LastSyncTime`).
  - `service/sync.py:check_load_required` (seeds a row with `TenantStatus=LOADING`).
- **Readers**
  - `service/tenant_data_repository.py` and `service/app.py` (tenant status APIs/UI gating).
- **Example item**:
```json
{
  "TenantID": "<tenant_id>",
  "TenantStatus": "LOADING",
  "LastSyncTime": 1706448896000
}
```

### S3 Layout
**Bucket**
- Name pattern: `dexero-statement-processor-{stage}` (`cdk/stacks/statement_processor.py`).

**Key structure for statements** (defined in `service/utils/storage.py`)
- PDFs: `{tenant_id}/statements/{statement_id}.pdf`
  - Written by: `service/app.py:_process_statement_upload` via `upload_statement_to_s3`.
  - Read by: Textract (via Step Functions) and the Textraction Lambda.
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- JSON outputs: `{tenant_id}/statements/{statement_id}.json`
  - Written by: `lambda_functions/textraction_lambda/core/textract_statement.py:run_textraction`.
  - Read by: `service/utils/storage.py:fetch_json_statement` (used in `service/app.py` statement detail view).
  - Updated by: `service/app.py:_persist_classification_updates` (re‑uploads JSON after item type changes).
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- Key sanitisation: `_statement_s3_key` rejects path separators in `tenant_id`/`statement_id` to avoid path traversal in keys (`service/utils/storage.py`).

**Key structure for cached Xero datasets**
- `{tenant_id}/data/{resource}.json` where `resource` is one of `contacts`, `invoices`, `credit_notes`, `payments` (`service/xero_repository.py`, `service/sync.py`).
  - Written by: `service/sync.py` after fetching from Xero.
  - Read by: `service/xero_repository.py` (download to local cache when missing).

## Anomaly Detection Logic

**Anomaly detection and validation** (Textraction Lambda; `lambda_functions/textraction_lambda`)

- **Rule: Flag invalid dates on extraction**
  - Logic: In `_map_row_to_item(...)`, if a `date` field contains text but parsing with the configured format returns `None`, the item gets an `invalid-date` flag (`lambda_functions/textraction_lambda/core/transform.py`).
  - Why it exists: highlights rows where configured date parsing fails, signalling potentially incorrect mappings or malformed input.
  - Example: Statement date “32/13/2024” with format `DD/MM/YYYY` yields `invalid-date`.

- **Rule: Keyword‑based outlier flagging (`ml-outlier`)**
  - Logic: `apply_outlier_flags(...)` flags items when:
    - `number` is missing (`missing-number` issue), or
    - `number` / `reference` contains balance/summary keywords from `SUSPECT_TOKEN_RULES` (e.g. “brought forward”, “closing balance”, “amount due”).  
    The single‑token `balance` rule only triggers when the text is short (≤3 tokens) and contains no digits; `summary` only triggers when short (≤3 tokens) (`lambda_functions/textraction_lambda/core/validation/anomaly_detection.py`).
  - Why it exists: it is intended to catch non‑transaction rows like balances and summary lines that often appear in statements.
  - Example: “Balance brought forward” or “Amount due” in a reference field is flagged; “Balance 2023” is not flagged by the single‑token “balance” rule because it includes digits.

- **Rule: Flags are additive and preserved**
  - Logic: Flagged items get `_flags` (list of strings) plus `FlagDetails[FLAG_LABEL]` with structured issues/details; `remove=False` keeps rows and only annotates them. `run_textraction` calls `apply_outlier_flags(..., remove=False)` so items are preserved (`lambda_functions/textraction_lambda/core/validation/anomaly_detection.py`, `lambda_functions/textraction_lambda/core/textract_statement.py`).
  - Why it exists: enables UI warnings without dropping data. `_flags` is kept as legacy to not break UI logic currently. Once UI is updated it will be completely replaced by `FlagDetails`
  - Example: A row with `ml-outlier` is still shown in the UI but highlighted as anomalous.

- **Rule: Best‑effort reference validation (non‑blocking)**
  - Logic: `validate_references_roundtrip(...)` compares extracted references against PDF text (pdfplumber) and raises `ItemCountDisagreementError` on mismatch, but the call is wrapped in `try/except` in `run_textraction`, so failures only log warnings and do not block output (`lambda_functions/textraction_lambda/core/validation/validate_item_count.py`, `lambda_functions/textraction_lambda/core/textract_statement.py`).
  - Why it exists: provides a safety check while preserving pipeline availability when PDFs are noisy or scanned.
  - Example: If the PDF is image‑only (no extractable text), validation is skipped to avoid false mismatches.

## Xero Matching Logic

- Payment-looking references are excluded from substring matching


## Tenant Snapshot Script (`scripts/tenant_snapshot/tenant_snapshot.py`)

This script backs up and restores **contact configs + statement PDFs** for a single tenant using environment variables only (no CLI args).

### What it does
- **Backup mode** (`TENANT_SNAPSHOT_MODE=backup`):
  - Reads all rows for `TENANT_ID` from `TenantContactsConfigTable`.
  - Reads statement header rows for `TENANT_ID` from `TenantStatementsTable` (`RecordType=statement` / missing).
  - Downloads each statement PDF from `s3://$S3_BUCKET_NAME/{tenant_id}/statements/{statement_id}.pdf`.
  - Writes snapshot files under `TENANT_SNAPSHOT_DIR/<TENANT_ID>/`:
    - `contact_configs.json`
    - `statements_manifest.json`
    - `pdfs/<statement_id>.pdf`
- **Restore mode** (`TENANT_SNAPSHOT_MODE=restore`):
  - Rewrites contact configs from `contact_configs.json` back to `TenantContactsConfigTable`.
  - Re-uploads PDFs from `pdfs/` to S3 using **new** statement IDs.
  - Recreates statement header rows in `TenantStatementsTable` (new IDs, fresh `UploadedAt`, `Completed=false`).
  - Optionally starts Textraction Step Functions for each restored statement to regenerate JSON/item rows.

### Environment variables
- Required:
  - `TENANT_ID`
  - `TENANT_SNAPSHOT_MODE` (`backup` or `restore`)
- Usually loaded from `service/.env` (or set manually):
  - `S3_BUCKET_NAME`
  - `TENANT_CONTACTS_CONFIG_TABLE_NAME`
  - `TENANT_STATEMENTS_TABLE_NAME`
- Optional:
  - `TENANT_SNAPSHOT_ENV_FILE` (default: `service/.env`)
  - `TENANT_SNAPSHOT_DIR` (default: `scripts/tenant_snapshot/snapshots`)
  - `TENANT_SNAPSHOT_YES=true` (skip confirmation prompt)
  - `TENANT_SNAPSHOT_START_WORKFLOWS=true|false` (restore only; default `true`)
  - `TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS` (restore only; default `1`, waits between workflow starts to reduce Textract throughput errors)
  - `AWS_PROFILE`, `AWS_REGION`

### Example usage
```bash
cd statement-processor

# Backup one tenant
export TENANT_ID=<tenant-id>
export TENANT_SNAPSHOT_MODE=backup
python3.13 scripts/tenant_snapshot/tenant_snapshot.py

# ...run scripts/clear_ddb_and_s3/clear_ddb_and_s3.py...

# Restore same tenant snapshot
export TENANT_SNAPSHOT_MODE=restore
python3.13 scripts/tenant_snapshot/tenant_snapshot.py
```

### Notes
- Restore intentionally creates **new statement IDs**. Statement JSON and item rows are regenerated by Textraction.
- If you disable workflow starts (`TENANT_SNAPSHOT_START_WORKFLOWS=false`), PDFs + statement headers are restored but JSON/item rows will not exist until processing is triggered later.
- This script is designed for operational reset/reseed workflows, not perfect forensic restoration of every historical field.

## Playwright Regression Fixture: Test Statements Ltd (Demo Company UK)

This fixture locks the end-to-end statement rendering logic against a known PDF + Xero dataset. Demo Company (UK) is periodically reset by Xero, so the dataset must be re-seeded when that happens.

### Setup (after resets or when bootstrapping)
1) Generate the PDF fixture:
   - `python3.13 scripts/generate_example_pdf/create_test_pdf.py`
2) Copy the generated PDF to the Playwright fixtures folder:
   - Source: `scripts/generate_example_pdf/test_pdf.pdf`
   - Destination: `service/playwright_tests/statements/test_statements_ltd.pdf`
3) Upload the PDF via the UI:
   - Log in to the app and switch to tenant **Demo Company (UK)**.
   - Upload `test_statements_ltd.pdf` for contact **Test Statements Ltd**.
4) Ensure the contact mapping matches the PDF headers:
   - Number column: `reference`
   - Date column: `date`
   - Total columns: `debit`, `credit`
   - Date format: `YYYY-MM-DD`
5) Populate Xero from the extracted statement JSON:
   - Run `python3.13 scripts/populate_xero/populate_xero.py`.
   - The script defaults to Demo Company (UK) and the Test Statements Ltd statement/contact IDs; override as needed with `TENANT_ID`, `STATEMENT_ID`, and `CONTACT_ID` env vars.
6) Capture the Excel baseline:
   - From the statement detail page, click “Download Excel”.
   - Save it as `service/playwright_tests/fixtures/expected/test_statements_ltd.xlsx`.

### Notes
- The population script intentionally skips “no match”, “balance forward”, and invalid date rows so the UI shows both matched and unmatched cases.
- If the Demo Company tenant resets, repeat the setup steps above to restore the fixture.
