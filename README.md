# Statement Processor

## Repository Structure
- Major domains (top‑level directories):
  - `cdk/`: Infrastructure‑as‑code for provisioning AWS resources (CDK app and stacks).
  - `lambda_functions/`: Lambda/container workloads; hosts the Extraction Lambda and the Tenant Erasure Lambda.
  - `service/`: Flask service, web assets, and service‑level tests.
  - `scripts/`: One‑off utilities and operational scripts (e.g. data maintenance, sample artefacts).
- Shared libraries (internal to this repo):
  - `service/core` and `service/utils`: shared domain logic and helpers for the Flask service.
  - `lambda_functions/extraction_lambda/core`: shared extraction, transformation, and validation logic for the Extraction Lambda.
  - `lambda_functions/tenant_erasure_lambda/`: scheduled Lambda that erases data for disconnected tenants past their erasure deadline.
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
│   ├── tenant_erasure_lambda/
│   │   ├── Dockerfile
│   │   ├── config.py
│   │   ├── logger.py
│   │   ├── main.py
│   │   ├── pyproject.toml
│   │   ├── requirements.txt
│   │   ├── requirements-dev.txt
│   │   └── tests/
│   │       ├── __init__.py
│   │       ├── conftest.py
│   │       └── test_main.py
│   └── extraction_lambda/
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
│       │   ├── billing.py
│       │   ├── date_utils.py
│       │   ├── extraction.py
│       │   ├── extraction_prompt.md
│       │   ├── models.py
│       │   ├── statement_processor.py
│       │   └── validation/
│       │       ├── __init__.py
│       │       └── anomaly_detection.py
│       └── tests/
├── scripts/
│   ├── accuracy_test/
│   │   ├── generate_pdfs.py
│   │   ├── requirements.txt
│   │   └── run_accuracy_test.py
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
│   ├── populate_xero/
│   │   ├── populate_xero.py
│   │   └── requirements.txt
│   ├── replace_textract_test/
│   │   ├── run.py
│   │   ├── system_prompt.md
│   │   └── requirements.txt
│   └── update-vendor-assets.sh
└── service/
    ├── app.py
    ├── banner_service.py
    ├── billing_service.py
    ├── config.py
    ├── Dockerfile
    ├── logger.py
    ├── pyproject.toml
    ├── pytest.ini
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── run_as_container.sh
    ├── statement_view_cache.py
    ├── sync.py
    ├── tenant_data_repository.py
    ├── xero_repository.py
    ├── core/
    │   ├── __init__.py
    │   ├── date_disambiguation.py
    │   ├── date_utils.py
    │   ├── item_classification.py
    │   ├── models.py
    │   ├── number_disambiguation.py
    │   ├── statement_detail_types.py
    │   └── statement_row_palette.py
    ├── playwright_tests/
    │   ├── helpers/
    │   └── tests/
    ├── static/
    │   └── assets/
    │       └── vendor/
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
  - ~~`TenantContactsConfigTable`~~: **Removed.** Previously stored per-contact column mappings. Now redundant because Bedrock returns self-describing statement JSON with embedded metadata (`header_mapping`, `date_format`, etc.).
  - `TenantDataTable` (`tenant_data_table`): shared tenant state table wired into both App Runner and the Extraction Lambda via env vars and IAM grants; this now stays focused on sync/load metadata rather than mutable billing balance state.
  - `TenantBillingTable` (`tenant_billing_table`): dedicated tenant billing snapshot table keyed by `TenantID`; shared by App Runner and the Extraction Lambda because uploads reserve tokens in the web app while asynchronous consume/release settlement happens after the Step Functions workflow finishes. Keeping this snapshot separate from `TenantDataTable` lets balance writes stay atomic with the token ledger without colliding with sync/load metadata.
  - `TenantTokenLedgerTable` (`tenant_token_ledger_table`): append-only tenant billing ledger table keyed by `TenantID` + `LedgerEntryID`; shared by App Runner and the Extraction Lambda because both runtimes now participate in the token lifecycle (`RESERVE` on upload, `CONSUME` on success, `RELEASE` on failure).
  - `StripeEventStoreTable` (`stripe_event_store_table`): Stripe webhook idempotency table keyed by `StripeEventID`; exposed only to App Runner because webhook verification and deduplication terminate in the Flask service, not the Extraction Lambda.
- **S3 bucket**
  - `dexero-statement-processor-{stage}` (`s3_bucket`): shared object store referenced by both App Runner and the Extraction Lambda. The previous Textract bucket policy has been removed — the Lambda now downloads PDFs directly from S3 and sends them to Bedrock.
- **Lambda**
  - `ExtractionLambda` (`extraction_lambda`): container‑image Lambda built from `lambda_functions/extraction_lambda` to perform statement extraction; invoked by the Step Functions state machine (`ProcessStatement` task).
  - `TenantErasureLambda` (`tenant_erasure_lambda`): container‑image Lambda built from `lambda_functions/tenant_erasure_lambda`; triggered daily at 02:00 UTC by an EventBridge scheduler rule. Scans `TenantDataTable` for tenants whose `EraseTenantDataTime` has passed, deletes all S3 objects under the tenant prefix and all `TenantStatementsTable` rows, then sets `TenantStatus` to `ERASED` via a conditional write. The conditional write uses `attribute_exists(EraseTenantDataTime)` to prevent a race condition where the tenant reconnects between the scan and the status update — if `ConditionalCheckFailedException` is raised, the tenant is skipped rather than failed. Tenants with `LOADING` or `SYNCING` status at erasure time are also skipped to avoid interfering with an active reconnection sync. The Lambda continues to the next tenant on any per-tenant error, returning a summary of `erased`, `skipped`, and `failed` counts.
  - `ExtractionLambda` and `StatementProcessorWebLambda` are explicitly configured for `arm64`, and their Docker image assets are built as `linux/arm64` to avoid architecture mismatches and improve cold-start efficiency on Graviton.
  - `ExtractionLambdaLogGroup` (`extraction_log_group`): explicit log group with stage‑dependent retention (3 months in prod, 1 week otherwise).
- **Step Functions**
  - `ExtractionStateMachine` (`state_machine`): 2-state workflow — `ProcessStatement` → `DidStatementProcessingSucceed?`. The Lambda calls Bedrock synchronously, so the previous Textract polling states were removed.
- **App Runner**
  - `Statement Processor Website` (`web`): App Runner service built from `service/` (`AppRunnerImage`) to run the Flask service; uses an instance role to access DynamoDB, S3, and Step Functions. App Runner no longer needs Textract or Bedrock permissions — extraction is handled entirely by the Lambda.
  - App Runner now receives explicit `AWS_REGION` and `AWS_DEFAULT_REGION` runtime env vars from CDK instead of relying on platform-injected defaults. Rationale: `service/config.py` creates boto3 clients during module import, and the Flask worker boot path must not depend on whether App Runner happens to inject a default region variable.
  - App Runner health checks now use HTTP against `/healthz` instead of raw TCP. The Flask route returns `200` with an empty body. Rationale: TCP only proves the Gunicorn master socket is open; it does not prove a worker successfully booted or that Flask can return a response. `/healthz` is a tiny unauthenticated route with no template or business-logic work, so it is a better signal for whether Flask can actually handle requests.
  - Lambda does not set `AWS_REGION` manually in CDK. Rationale: Lambda reserves that environment variable name and already injects the runtime region automatically.
  - Production public-domain settings are configured in `cdk/app.py` (`PROD_DOMAIN_NAME`) and consumed by `cdk/stacks/statement_processor.py` to set CloudFront aliases and the OAuth callback host consistently.
- **IAM roles and policies**
  - `Statement Processor App Runner Instance Role` (`statement_processor_instance_role`): grants App Runner access to CloudWatch metrics and Step Functions; table and S3 permissions are added via grants. Textract and Bedrock permissions are no longer needed on App Runner.
  - Web Lambda runtime no longer requires `ssm:GetParameter`/`kms:Decrypt` for Xero/session secrets; `cdk/deploy_stack.sh` reads SSM secure parameters before deploy and passes them into CDK as deploy-time environment variables for Lambda. This removes per-cold-start SSM/KMS network calls from the Flask service startup path.
  - `cdk/deploy_stack.sh` now runs a bounded Docker multi-arch preflight before `cdk deploy`: it reuses any existing `buildx` builder that already advertises `linux/arm64` (preferring the active/default builder before creating the repo-specific `multiarch` builder), skips the privileged `tonistiigi/binfmt` refresh when an initial `linux/arm64` smoke test already succeeds, and wraps bootstrap/runtime checks in explicit progress messages plus timeouts. Rationale: first-run image pulls and stale custom builders were previously hidden behind `/dev/null`, which made deploys look stuck at the Docker multi-arch step even when Docker was still bootstrapping emulation.
  - Lambda gets `bedrock:InvokeModel` for Bedrock Converse API calls (replacing the previous `textract:GetDocumentAnalysis` permission). The state machine no longer needs direct Textract permissions since it only invokes the Lambda.
- **CloudWatch + SNS**
  - `StatementProcessorAppRunnerErrorMetricFilter` + `StatementProcessorAppRunnerErrorAlarm`: parses App Runner application logs for `ERROR` and raises an alarm.
  - `ExtractionLambdaErrorMetricFilter` + `ExtractionLambdaErrorAlarm`: parses Extraction Lambda logs for `ERROR` or timeout strings and raises an alarm.
  - `StatementProcessorAppRunnerErrorTopic`: SNS topic that both alarms publish to. It has email subscriptions for `ollie@dotelastic.com` and `james@dotelastic.com`.

## Monitoring and Notifications

### CloudWatch Alarms
Two CloudWatch metric filters + alarms watch for errors in production:

| Alarm | Log group | Trigger |
|---|---|---|
| `StatementProcessorAppRunnerErrorAlarm` | App Runner application logs | Any log line containing `ERROR` |
| `ExtractionLambdaErrorAlarm` | Extraction Lambda logs | Any log line containing `ERROR` or a timeout string |

Both alarms publish to the same SNS topic (`StatementProcessorAppRunnerErrorTopic`).

### Alarm notifications
The SNS topic has two email subscriptions:
- `ollie@dotelastic.com`
- `james@dotelastic.com`

### Login notification emails
After every successful Xero OAuth callback, the Flask service sends a login notification email via AWS SES (`service/utils/email.py`):
- **Sender:** `info@dotelastic.com`
- **Recipient:** `ollie@dotelastic.com`
- **Subject:** `Statement Processor Login`
- **Content:** tenant name, user's full name, and user's Xero email address, rendered from `service/templates/email/login_notification.html` using a standalone Jinja2 environment (not Flask's `render_template`, so it works without an active Flask app context).
- **Behaviour:** fire-and-forget — failures are caught and logged at ERROR level but never block the login flow.
- **Skipped outside production:** `email.py` reads `STAGE` from `os.environ` at module import time (not from `config.py`, to avoid triggering the SSM secrets fetch in tests). When `STAGE != "prod"`, the function returns early without calling SES.

Rationale: a login notification gives immediate visibility into who is accessing the application in production without relying solely on CloudWatch log tailing.

### Manual post-deploy steps
These one-time steps are required before notifications work in a new AWS environment:

**SES sender identity verification**
AWS SES requires every sender address to be verified before it can be used to send email. The sender is `info@dotelastic.com`.

```bash
aws ses verify-email-identity --email-address info@dotelastic.com --region eu-west-1
```

Check the inbox for `info@dotelastic.com` and click the verification link. Until this step is complete, all `send_email` calls for login notifications will fail with `MessageRejected`.

## Orchestration (Step Functions & Bedrock)

> **Migration note (2026-04):** Statement extraction was migrated from AWS Textract to Amazon Bedrock (Claude haiku-4-5) via the Converse API with forced tool use. See **Textract to Bedrock Migration** section below for rationale and details.

**State machine definitions and entry points**
- `ExtractionStateMachine` is defined in `cdk/stacks/statement_processor.py` as a 2-state workflow: `ProcessStatement` -> `DidStatementProcessingSucceed?`. The previous 6-state Textract polling workflow (`StartTextractDocumentAnalysis` -> `WaitForTextract` -> `GetTextractStatus` -> `IsTextractFinished?` -> `ProcessStatement` -> `DidStatementProcessingSucceed?`) was replaced because Bedrock calls are synchronous — the Lambda sends PDF chunks to Bedrock and receives structured JSON back in one call, eliminating the need for polling.
- Executions are started from the Flask service via `service/utils/workflows.py:start_extraction_state_machine`, invoked during upload in `service/app.py:_process_statement_upload`.

**Step-by-step flow (code-grounded)**
1. Upload handler registers statement metadata and starts the state machine (`service/app.py:_process_statement_upload` -> `service/utils/workflows.py:start_extraction_state_machine`).
2. Step Functions invokes `ExtractionLambda` with S3 keys (`ProcessStatement`).
3. Lambda downloads the PDF from S3, chunks it (up to ~10 pages per chunk with 1-page overlap), and sends each chunk to Bedrock via the Converse API with forced tool use (`extract_statement_rows`) to receive structured JSON back (`lambda_functions/extraction_lambda/core/extraction.py`).
4. Lambda merges chunk results, deduplicates overlap rows, builds statement JSON with self-describing metadata (`header_mapping`, `date_format`, `date_confidence`, `decimal_separator`, `thousands_separator`), persists items to DynamoDB, and writes JSON to S3 (`lambda_functions/extraction_lambda/core/statement_processor.py`). On success it consumes the earlier token reservation; on failure it releases the reservation back to `TenantBillingTable`.
5. `lambda_functions/extraction_lambda/main.py` returns a compact metadata payload (IDs, `jsonKey`, filename/date/item summary); Step Functions branches on `Payload.status` so billing failures and processing failures explicitly fail the execution.

**Timeout configuration**
- Lambda timeout: 660 seconds (Bedrock calls for large multi-chunk PDFs can take longer than the old Textract polling).
- State machine timeout: 720 seconds (down from 30 minutes, since there is no polling loop).

## Textract to Bedrock Migration

### What changed
Statement extraction was migrated from AWS Textract to Amazon Bedrock (Claude haiku-4-5) via the Converse API with forced tool use. The Lambda sends PDF page chunks to Bedrock and receives structured JSON back in a single synchronous call, replacing the asynchronous Textract polling workflow.

### Why
- **65% cheaper** per statement than Textract table extraction.
- **50% faster** end-to-end (no polling loop, no wait states).
- **Identical accuracy** validated across 18 real-world PDFs during testing.
- **Structural understanding**: Textract struggled with structurally diverse PDFs (misidentified section titles as headers, could not handle multi-line headers, required growing workarounds). Bedrock gives structural understanding of the document.

### Self-describing statement JSON
The output JSON now carries metadata that the service reads directly from S3:
- `header_mapping`: detected column-to-field mapping (e.g. `{"date": "Date", "number": "Reference"}`).
- `date_format`: detected date format string (e.g. `DD/MM/YYYY`).
- `date_confidence`: `high` (unambiguous dates found) or `low` (all dates ambiguous).
- `decimal_separator` and `thousands_separator`: detected number formatting.

This eliminates the need for a per-contact `ContactConfig` stored in DynamoDB. The service reads the statement JSON from S3 and uses the embedded metadata directly.

### What was removed
- **ContactConfig (DynamoDB table + code)**: `TenantContactsConfigTable`, all `get_contact_config` and `contact_config_metadata` modules in both service and Lambda.
- **Config suggestion pipeline**: `service/core/config_suggestion.py`, `service/core/bedrock_client.py`, `service/core/date_disambiguation.py`, config suggestion S3 prefix.
- **`/configs` UI route**: No longer needed — users do not manually configure column mappings.
- **Textract polling states**: The 6-state Step Functions workflow (`StartTextractDocumentAnalysis` -> `WaitForTextract` -> `GetTextractStatus` -> `IsTextractFinished?` -> `ProcessStatement` -> `DidStatementProcessingSucceed?`) was replaced with a 2-state workflow (`ProcessStatement` -> `DidStatementProcessingSucceed?`).
- **Textract IAM permissions**: Lambda gets `bedrock:InvokeModel` instead. App Runner no longer needs Textract or Bedrock permissions.
- **S3 bucket policy for Textract**: Textract previously needed to read PDFs from S3 directly; the Lambda now downloads PDFs and sends them to Bedrock.

### CDK changes summary
- Lambda IAM: `bedrock:InvokeModel` replaces `textract:GetDocumentAnalysis`.
- Lambda timeout: 60s -> 660s (Bedrock calls can take longer than Textract polling).
- State machine timeout: 30 minutes -> 720 seconds.
- State machine definition: 2 states instead of 6.
- App Runner: no longer granted Textract or Bedrock permissions.
- `TenantContactsConfigTable`: removed from CDK stack.

### Accuracy test suite
Located at `scripts/accuracy_test/`. Contains 8 synthetic PDF scenarios testing extraction accuracy across different statement layouts and edge cases.

```bash
# Requires CDK deploy for Bedrock permissions (uses deployed Lambda's IAM role)
python3.13 scripts/accuracy_test/run_accuracy_test.py
```

### Bedrock model access
The Bedrock Converse API requires model access to be enabled in the AWS console. The EU cross-region inference profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0` must be enabled in the deployment region. This is a manual console step — CDK only grants the IAM permissions.

## Flask Service

- **App structure**
  - Main application: `service/app.py` (Flask app factory, route handlers, template rendering, orchestration).
  - Templates and UI assets: `service/templates/` (Jinja2 views) and `service/static/` (static assets). See **Frontend Design System** below for details on the CSS architecture.
  - Frontend design reference: static mockups in `new-design/` (index.html, about.html, instructions.html, styles.css) served as the design source of truth during the UI overhaul.
  - Configuration + AWS clients: `service/config.py` (environment-variable loading, boto3 clients/resources).
    - `service/config.py` now uses a local `get_envar(...)` helper that mirrors the Numerint Flask app: required env vars fail fast during import, while a small set of local-development defaults (`DOMAIN_NAME`, `STAGE`, `VALKEY_URL`) remain explicit. AWS clients/resources are now created directly via `boto3.client(...)` / `boto3.resource(...)` rather than a custom `boto3.session.Session(...)`. Rationale: this matches the working Numerint pattern, removes conditional session logic, and makes missing runtime configuration obvious during worker startup.
  - Container startup: `service/start.sh` (manages Nginx, Gunicorn, and Valkey).
    - Nginx reverse proxy listens on port 8080 and forwards to Gunicorn via Unix socket (`/tmp/flask.sock`).
    - When `STAGE=prod`, `start.sh` injects CloudFront protection (`X-Statement-CF` header check) and disables `/static/` serving (CloudFront/S3 handles it).
    - See **Nginx Reverse Proxy** section below for maintenance details.
    - Gunicorn now writes both access logs and error logs to stdout (`--access-logfile - --error-logfile -`). Rationale: App Runner deployment failures were previously only visible as generic service rollbacks, so emitting request-path logs gives direct evidence about whether health checks reach Gunicorn and whether Flask ever returns a response.
  - Logging: `service/logger.py` (structured logger used across modules).
  - Session/auth wiring: Redis-backed server-side sessions in `service/app.py` using Flask-Session + Valkey/ElastiCache.
    - Tenant sync-status checks are read directly from DynamoDB via `service/utils/tenant_status.py` for consistent cross-instance behavior.

- **Main modules/packages**
  - `service/core/`: domain models and logic (e.g. `item_classification.py`, `models.py`). The config-related modules (`contact_config_metadata.py`, `get_contact_config.py`, `config_suggestion.py`, `bedrock_client.py`, `date_disambiguation.py`) have been removed.
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
  - Stripe integration: `service/stripe_service.py` and `service/stripe_repository.py`
    - `service/stripe_service.py` — all Stripe SDK calls (`stripe.Customer.create`, `stripe.checkout.Session.create/retrieve`). Uses dynamic `price_data` (not fixed Price objects) because token count is a free-form integer; the single Stripe Product (`prod_UBMoFkqStKFcjg`) is referenced by `STRIPE_PRODUCT_ID` env var so purchase history is attributed correctly in Stripe reporting. A fresh Stripe Customer is created per checkout with the user-provided billing details so each invoice reflects exactly what was entered for that purchase.
    - `service/stripe_repository.py` — DynamoDB ops for checkout state: idempotency records on `StripeEventStoreTable` only. Imports pre-constructed table objects from `service/config.py` (consistent with all other repositories) rather than constructing its own `ddb.Table` instances.
  - Banner system: `service/banner_service.py` (see **Banner system** below)
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
    - `/tenant_management` (GET): tenant picker/overview (requires Xero auth via `@xero_token_required`). The page reads each tenant's `TokenBalance` snapshot from `TenantBillingTable` so the current tenant chip and tenant list both show available tokens without recomputing ledger totals in-request.
    - `/upload-statements` (GET/POST): upload PDFs and trigger extraction (requires tenant + Xero auth, blocks while loading). The page keeps the lightweight client-side page estimate for instant feedback, but the POST handler now reserves tokens atomically before any S3 upload starts. Reservation writes update `TenantBillingTable`, append `RESERVE` rows to `TenantTokenLedgerTable`, and create the statement header rows with `PdfPageCount`, `ReservationLedgerEntryID`, and `TokenReservationStatus=reserved`. If the web app cannot upload the PDF or start Step Functions after reservation, it immediately releases the reservation and cleans up the statement row again.
    - `/api/upload-statements/preflight` (POST): authoritative upload validation endpoint used by the upload page before submit. It accepts the currently selected PDFs, counts their pages server-side with `pypdf` (while the browser keeps its own lightweight estimate for instant UX), reads `TenantBillingTable.TokenBalance`, and returns per-file counts plus `total_pages`, `available_tokens`, `shortfall`, and `can_submit`. This exists so the UI can warn about insufficient tokens before the final upload request without trusting the browser-only estimate.
    - `/statements` (GET): list and sort statements (requires tenant + Xero auth, blocks while loading). Supports server-side pagination via `page` and `per_page` query params (`per_page` snaps to nearest of 25/50/100, default 25). All data is fetched and sorted in memory first (DynamoDB doesn't support arbitrary sort-then-offset), then sliced to the current page before rendering. Pagination controls (prev/next + per-page selector) appear below the table and compactly in the sticky dock; hidden entirely when all items fit on one page. Sorting and view toggles preserve `per_page` but reset to page 1. Delete redirects preserve the full pagination/sort state.
    - `/statement/<statement_id>` (GET/POST): statement detail view, completion toggles, and XLSX export (requires tenant + Xero auth, blocks while loading). Items are paginated at a fixed 50 per page after filtering (incomplete/completed/all, show/hide payments). Pagination controls appear in both the static footer bar and sticky dock. Changing filters resets to page 1. Completing/incompleting individual items preserves the current page via hidden form fields. Excel export always exports all rows regardless of pagination state. Responsive improvements: a sticky horizontal scrollbar proxy (`#scroll-proxy`) floats at the viewport bottom when the comparison table overflows horizontally, syncing scroll position bidirectionally so users can scroll sideways without reaching the table's native scrollbar at the very bottom. The proxy positions itself above the sticky action dock when both are visible. A landscape orientation tip banner (`.landscape-tip`) appears on phones in portrait mode (≤575.98px) suggesting the user rotate for a better view; it hides automatically in landscape via CSS `orientation` media query.
    - `/statement/<statement_id>/delete` (POST): delete statement + artefacts (requires tenant + Xero auth, blocks while loading).
    - ~~`/configs`~~: **Removed.** Contact mapping configuration UI is no longer needed — Bedrock returns self-describing JSON.
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
    - `/api/banner/dismiss` (POST): permanently dismiss a banner for the current tenant. Accepts `{"dismiss_key": "<key>"}` and writes the key to the tenant's `DismissedBanners` set in `TenantDataTable` via `TenantDataRepository.dismiss_banner`. Returns `204` on success.
    - **Auth behavior for API routes**: When `@xero_token_required` protects a `/api/...` endpoint and the session token is missing or expired, the decorator returns `401` JSON (`{"error": "auth_required"}`) instead of redirecting. The frontend polling/sync code (`service/static/assets/js/main.js`) treats either a 401 response or a redirected login response as a signal to navigate to `/login`, so passive actions still force a full re-login.
  - **Stripe / page purchasing**
    - `/pricing` (GET): public-facing pricing page — no auth required, shows graduated volume pricing (from £0.08/page).
    - `/buy-pages` (GET): page amount input form with live graduated price calculator and tenant selector dropdown; requires Xero auth. Accepts optional `?tenant_id=` query param to pre-select a tenant. **UI naming convention:** the user-facing UI says "pages" everywhere, but the backend continues to use "tokens" internally (DynamoDB attributes, ledger entries, variable names, function names, routes). This keeps the billing unit decoupled from the display name — the token-to-page ratio could change in the future.
    - `/api/checkout/create` (POST): validates billing details, creates or reuses a persistent Stripe Customer per tenant (stored in `TenantBillingTable.StripeCustomerID`), creates a Stripe Checkout Session with graduated pricing from `PricingConfig`, and redirects to Stripe. On first purchase a new Customer is created and its ID persisted; on subsequent purchases the existing Customer's billing details are updated via `stripe.Customer.modify`.
    - `/checkout/success` (GET): called by Stripe on payment completion with `?session_id=cs_xxx`. Retrieves the session from Stripe, verifies `payment_status == "paid"`, checks idempotency via `StripeEventStoreTable`, credits tokens via `BillingService.adjust_token_balance` with `source="stripe-checkout"`, a deterministic `ledger_entry_id`, and the effective `price_per_token_pence` from graduated pricing. Renders the success page with pages credited and updated balance. Safe to refresh — idempotency check prevents double-crediting.
    - `/checkout/cancel` (GET): renders a cancellation page with a "Try Again" link; no pages are credited.
    - `/checkout/failed` (GET): renders an error page with a hex reference ID for support lookup.
    - **Preflight shortfall link**: when `/api/upload-statements/preflight` returns `shortfall > 0`, the response JSON now includes `buy_tokens_url: "/buy-pages"` so the upload page JS can render a "Buy Pages" link in the red shortfall summary.
  - **Auth**
    - `/login` (GET): start Xero OAuth flow.
    - `/callback` (GET): OAuth callback (token validation + tenant load). For first-time tenants, also grants welcome tokens (see **Welcome token grant** below).
    - `/logout` (GET): clear session.
    - `/tenants/select` (POST): set active tenant in session.
    - `/tenants/disconnect` (POST): disconnect tenant from Xero.
    - `/test-login` (GET): local-only route — only registered when `STAGE=local`. Seeds the Flask session with fake Xero auth data to bypass the real OAuth flow for browser/Playwright testing. Requires `PLAYWRIGHT_TENANT_ID` and `PLAYWRIGHT_TENANT_NAME` environment variables to be set. On success, redirects to `/tenant_management`. This route is never registered in non-local environments, so it cannot be called in staging or production.
    - **Cookie consent gate**: Protected routes and `/login` require the browser cookie `cookie_consent=true`. If consent is missing, UI routes redirect to `/cookies`; API routes return `401` JSON with `{"error": "cookie_consent_required", "redirect": "/cookies"}`.
    - **Session-state UI cookie**: Authenticated UI responses set `session_is_set=true` (short-lived helper cookie) so frontend JavaScript can toggle the final navbar item between `Login` and `Logout` without template-time session checks.
    - **Server-side auth sessions (Valkey/ElastiCache)**:
      - Backend/session store now uses Flask-Session with Redis (`SESSION_TYPE='redis'`, `SESSION_REDIS=redis.from_url(VALKEY_URL)`), so browser cookies carry only a signed session identifier while OAuth tokens remain server-side.
      - The app intentionally uses the same explicit session-config style as Numerint (`app.config[...]` assignments followed by `Session(app)`) for consistency and simpler operational debugging.
      - `VALKEY_URL` (default `redis://127.0.0.1:6379/0`) is loaded from environment in `service/config.py`; production deployments should point it at the ElastiCache/Valkey endpoint.
      - Cookie controls remain configured in `service/app.py`: `SESSION_COOKIE_SECURE` (conditional — `True` in all environments except `local`, where it is `False` to allow plain HTTP on localhost; without this the browser silently drops the session cookie over HTTP and every request appears unauthenticated), `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE='Lax'`, and `SESSION_REFRESH_EACH_REQUEST=True`.
      - `SESSION_TTL_SECONDS` (default `900`) is still used to set `PERMANENT_SESSION_LIFETIME`.
      - Required auth/session secrets are now `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`, and `FLASK_SECRET_KEY` (no `SESSION_FERNET_KEY`).
      - In AWS, `cdk/deploy_stack.sh` resolves those values from SSM secure parameters at deployment and passes them into CDK for Lambda environment injection. This keeps deployment-time secret sourcing while reducing cold-start latency by removing runtime secret fetches.
      - The Flask service now mirrors Numerint's Python-side hostname handling: `/login` builds the OAuth callback URL from `DOMAIN_NAME` at request time, using `http://localhost:<port>` when `STAGE=local` and `https://<DOMAIN_NAME>` otherwise. Rationale: this keeps callback URLs on one canonical public host without relying on Flask-side host redirects.
      - `XERO_REDIRECT_URI` is no longer injected into the Flask service. `DOMAIN_NAME` is now the single public-host input for the Python app, while direct App Runner host blocking will be handled later at the edge/proxy layer.
      - Browser-side CSRF delivery is now standardized across the app: `templates/base.html` always emits a `csrf-token` meta tag, and JavaScript `POST` requests send `csrf_token` in the request body instead of the `X-CSRFToken` header. Rationale: the CloudFront -> App Runner path was observed dropping the custom header in production, which caused `400` CSRF failures for tenant sync and upload preflight even though the browser had a valid token.
      - Flask app secret key remains stable across cold starts because it is provided as a fixed environment value rather than generated at runtime.
      - **Container runtime parity for local development**:
        - `service/Dockerfile` installs Valkey, Nginx, and curl and uses `service/start.sh` to run Nginx, Gunicorn, and Valkey in one container.
        - `service/run_as_container.sh` now mirrors the Numerint workflow: it replaces any existing `statement-processor` container, rebuilds, runs on `localhost:8080`, tails logs, and supports `-i/--interactive` shell mode.
        - `service/start.sh` now waits for Valkey readiness with `valkey-cli ping` before starting Gunicorn instead of relying on a fixed one-second sleep. Rationale: App Runner rollbacks have shown intermittent candidate startup failures, and a real readiness probe removes the race between the local session store binding its socket and the Flask worker starting to accept requests.
        - Rationale: Flask-Session now depends on a Redis/Valkey backend, so running cache and web process together keeps local execution aligned with App Runner behavior and avoids a second local service to manage.
  - **Misc**
    - `/.well-known/<path>` (GET): returns 204 for DevTools probes.

- **Upload processing flow** (from `service/app.py`)
  - `upload_statements` validates file/contact counts, enforces PDF MIME/extension rules (`service/utils/storage.py:is_allowed_pdf`), and then calls `service/billing_service.py` to reserve tokens atomically before any upload starts. Contact config validation is no longer needed — Bedrock detects column mappings during extraction.
  - `service/billing_service.py:reserve_statement_uploads`:
    - Decrements `TenantBillingTable.TokenBalance` conditionally.
    - Appends one `RESERVE` row per statement to `TenantTokenLedgerTable`.
    - Creates the initial `TenantStatementsTable` header row with `PdfPageCount`, `ReservationLedgerEntryID`, and `TokenReservationStatus=reserved`.
  - `_process_statement_upload`:
    - Uploads PDF to S3 (`upload_statement_to_s3` → `service/utils/storage.py`).
    - Computes JSON output key (`statement_json_s3_key`) and starts Step Functions (`start_extraction_state_machine` → `service/utils/workflows.py`).
    - If the upload handoff fails after reservation, `service/billing_service.py:release_statement_reservation` returns the tokens and the service deletes the partially created statement row/S3 artefacts.

## Nginx Reverse Proxy

Nginx sits in front of Gunicorn inside the container, providing security hardening, rate limiting, and per-route request validation. The setup mirrors `numerint/dexero/web`.

### Architecture

```
Client → CloudFront → AppRunner → Nginx (:8080) → Gunicorn (unix:/tmp/flask.sock) → Flask
```

### Configuration files

| File | Purpose |
|------|---------|
| `service/nginx.conf` | Main config: security headers, CSP, rate limiting, proxy settings, static file handling |
| `service/nginx-routes.conf` | Auto-generated per-route location blocks (committed to git) |
| `service/nginx_route_config_generator.py` | Generates `nginx-routes.conf` from Flask routes |
| `service/nginx_route_querystring_allow_list.json` | Allowed query parameters per route |
| `service/nginx_route_overrides.json` | Per-route directive overrides (e.g. `client_max_body_size`) |

### When to update Nginx config

| Change | Action |
|--------|--------|
| Adding external CDN/script/font sources | Update CSP in `service/nginx.conf` |
| Adding new Flask routes | Regenerate `nginx-routes.conf` (see command below) |
| Adding query parameters to a route | Update `service/nginx_route_querystring_allow_list.json` and regenerate |
| Adding routes that accept large request bodies | Update `service/nginx_route_overrides.json` and regenerate |
| Making an authenticated route public (removing auth decorator) | Regenerate — public routes without an allow-list entry will have query strings stripped |
| Changing the listen port | Update `listen` directive in `service/nginx.conf` |

> **Important:** The generator strips query strings from public (unauthenticated) routes that are not in the allow list. If a public route needs query parameters (e.g. `/callback` for OAuth), it **must** have an entry in `nginx_route_querystring_allow_list.json` — otherwise the parameters will be silently dropped by nginx before they reach Flask. Always regenerate and review the diff after any route, decorator, or allow-list change.

### Regenerating route config

Run from `service/`:

```bash
python3.13 nginx_route_config_generator.py \
  --app app:app \
  --upstream gunicorn \
  --output nginx-routes.conf \
  --route-params nginx_route_querystring_allow_list.json \
  --route-overrides nginx_route_overrides.json
```

Review the diff and commit the updated `nginx-routes.conf`.

### Security features

- **Rate limiting**: 20 req/s per client IP (burst 50)
- **Request validation**: blocks header injection (CRLF/null), request smuggling, XSS in X-Forwarded-For, empty/oversized User-Agent
- **Per-route method restriction**: only declared HTTP methods allowed
- **Query string validation**: routes whitelist allowed parameters; unrecognised params return 404 or are stripped
- **Body size limiting**: 64KB default, 10MB for upload routes only
- **Security headers**: X-Frame-Options, X-Content-Type-Options, HSTS, CSP, Permissions-Policy, Referrer-Policy
- **CloudFront protection** (`STAGE=prod`): requests without valid `X-Statement-CF` header return 403

### Design decisions

- **Port 8080**: kept from the original setup to avoid AppRunner/CDK changes and to allow running both statement processor and numerint locally without port conflicts.
- **nginx.conf copied to /app/ in Docker**: `start.sh` copies it to `/etc/nginx/nginx.conf` at runtime after applying marker replacements for the current `STAGE`. This avoids baking environment-specific config into the image.
- **nginx-routes.conf committed to git**: despite being auto-generated, committing it makes route changes visible in PRs and prevents accidental omissions.
- **Separate JSON configs**: query string allow list and route overrides are separate files because they address different concerns (security vs. performance).
- **`make run-app` bypasses Nginx**: runs Gunicorn directly on port 8080 for quick local iteration. Use `run_as_container.sh` to test the full Nginx + Gunicorn + Valkey stack locally.

### Production deployment requirement

`X_STATEMENT_CF` must be added to the AppRunner service's environment variables in `cdk/stacks/statement_processor.py` and set to a secret value that CloudFront sends as a custom origin header. The corresponding CloudFront behavior must be configured to inject the same header. Without this, `start.sh` will refuse to start when `STAGE=prod`.

### llms.txt

The `/llms.txt` endpoint serves `service/content/llms.md` as plain text, following the [llmstxt.org](https://llmstxt.org) specification. This file gives LLMs accurate context about the product when users ask questions about Statement Processor.

**When to update `service/content/llms.md`:**
- Pricing changes (token cost, minimum purchase, free token allocation)
- New or removed features
- Product description or target audience changes
- Public page additions or removals (update the Links section)

## Frontend Design System

The site uses Bootstrap 5.3.3 with a custom design token layer in `service/static/assets/css/main.css`. The design is intentionally approachable and trustworthy — targeting SMB finance teams who use Xero. Bootstrap and fonts are self-hosted under `service/static/assets/vendor/` — see **Vendor Assets** below.

- **Fonts**: Source Serif 4 (display/headings) + Outfit (body), served from `service/static/assets/vendor/fonts/` via `@font-face` declarations in `vendor/fonts.css` (included by `base.html`).
- **Colour palette**: Blue-600 primary (`#2563eb`), teal-600 accent (`#0d9488`), slate scale for text/borders, light white backgrounds. All colours are CSS custom properties in `:root`.
- **Bootstrap integration**: Bootstrap's `--bs-*` variables are mapped to the custom tokens, so Bootstrap components (forms, tables, alerts, badges, buttons) automatically use the palette. Custom component classes (`.page-panel`, `.page-kicker`, `.value-card`, etc.) layer on top.
- **Navbar**: Bootstrap navbar markup restyled with frosted-glass `backdrop-filter: blur(12px)`. The `navbar-scrolled` class is toggled by `main.js` on scroll but styled as a subtle no-op (no colour inversion).
- **Page headers**: `.page-header` provides serif h1 + spacing. `.page-header-hero` modifier adds a gradient background (`blue-50 → white`) for marketing pages. Statement detail page is excluded (uses its own `container-fluid` layout).
- **Panels**: `.page-panel` is the universal panel class (clean white, subtle border+shadow). `.page-panel-muted` adds a slate-50 background. `.page-panel-flat` is an alias kept for backwards compatibility.
- **Scroll-reveal**: `.reveal` (marketing pages — 0.6s, 20px translateY, staggered delays) and `.reveal-subtle` (functional pages — 0.3s, 8px) are animated via IntersectionObserver in `main.js`.
- **CTA panels**: `.cta-panel` uses a dark slate-900 background with inverted button colours. Used at the bottom of marketing pages.
- **Statement row colours**: Row colouring on the statement detail page uses `--statement-row-*` CSS custom properties injected by Jinja from `app.py`. These are not part of the design token layer — they are dynamic per-tenant configuration.

### Design decisions
- **Bootstrap kept**: Authenticated pages (forms, tables, modals) depend heavily on Bootstrap grid and components. Replacing Bootstrap would be a disproportionate effort. Instead, Bootstrap's colour system is overridden via `--bs-*` custom properties.
- **No CSS reset added**: The new design system does not add `* { margin:0; padding:0 }` or redefine `.container` — these would break Bootstrap's own layout assumptions.
- **Sticky footer**: `body` uses `display: flex; flex-direction: column; min-height: 100dvh` with `.site-shell-main { flex: 1 }` to push the footer to the bottom on short-content pages (checkout success/cancel/failed).
- **Self-hosted fonts and Bootstrap**: Bootstrap CSS/JS and Google Fonts (Source Serif 4 + Outfit, woff2) are served from `service/static/assets/vendor/` instead of external CDNs. This eliminates runtime dependencies on jsdelivr and Google Fonts, removes the need for CDN entries in the nginx CSP, and avoids privacy implications of third-party font requests. The download script (`service/scripts/update-vendor-assets.sh`) fetches all vendor files and generates `fonts.css` with `@font-face` declarations.

## HTMX Partial Page Updates

The statement detail (`/statement/<id>`) and statements list (`/statements`) pages use [HTMX](https://htmx.org/) for partial page updates. Instead of full page reloads on every interaction, only the dynamic content area swaps via HTMX — filters, pagination, sort, toggle payments, and mark complete all happen without a page flash.

### How it works

- **Content partials**: Each page's dynamic content is extracted into a Jinja partial (`templates/partials/statement_content.html` and `templates/partials/statements_content.html`). The main template `{% include %}`s the partial for the initial full-page load.
- **HX-Request detection**: The Flask route checks for the `HX-Request` header (set automatically by HTMX). If present, it renders just the partial; otherwise it renders the full page with `base.html`. No separate endpoints needed — same URL serves both.
- **Graceful degradation**: All links keep their `href` and forms keep their `action`/`method`, so the pages work without JavaScript.
- **URL bar sync**: GET interactions use `hx-push-url="true"` so the browser URL, back/forward, and bookmarks all work correctly.
- **Scroll preservation**: All swaps use `hx-swap="outerHTML scroll:no-scroll"` to prevent scroll position resetting.

### S3 JSON disk cache

The statement detail page fetches the statement JSON from S3 on every load. To avoid this network round-trip on repeat interactions, `fetch_json_statement` caches the S3 JSON to local disk (`/tmp/data/{tenant_id}/statements/{statement_id}.json`) with a 15-minute TTL. This follows the same pattern used by the Xero dataset cache in `xero_repository.py`. The S3 JSON is effectively immutable for a given statement ID during normal use — re-uploads create new statement IDs.

### Statement view cache (Redis)

After the initial page load completes the full build pipeline (S3 fetch, Xero data load, matching, classification, row building), the computed view data (`statement_rows` + `display_headers`) is cached in Redis for 120 seconds (`statement_view_cache.py`). Subsequent HTMX swaps — filter toggles, pagination — hit the cache and skip the entire pipeline, going straight to filtering + rendering (~20-50ms instead of ~350ms).

- **Cache key**: `stmt_view:{tenant_id}:{statement_id}`
- **Invalidation**: POST actions (mark complete/incomplete) delete the cache key before re-rendering so the pipeline rebuilds with fresh DynamoDB data.
- **Excel downloads**: Bypass the cache because they need intermediate pipeline data not stored in the cache.
- **Failure mode**: Redis errors are logged but never raised — a cache failure just means the pipeline re-runs (same as pre-cache behaviour).

### Per-contact Xero data index

At sync time, `build_per_contact_index()` in `sync.py` groups the flat dataset files (`invoices.json`, `credit_notes.json`, `payments.json`) by `contact_id` and writes per-contact JSON files to `{tenant_id}/data/xero_by_contact/{contact_id}.json` (local disk + S3). The statement detail page reads this single small file via `get_xero_data_by_contact()` in `xero_repository.py` instead of loading three full tenant datasets and filtering in-memory.

- **Backward compatible**: If the per-contact file doesn't exist (pre-migration tenants), `get_xero_data_by_contact()` falls back to loading the full datasets and filtering — no re-sync required.
- **Rebuilt every sync**: Both full and incremental syncs rebuild all per-contact files from the updated flat files. The cost is negligible (in-memory JSON grouping + a few S3 PUTs).
- **Tenant erasure**: No changes needed — the erasure Lambda deletes by `{tenant_id}/` prefix, which covers `xero_by_contact/`.

### JS re-initialisation

When HTMX swaps content, DOM elements that had event listeners or IntersectionObservers attached are replaced. The `setupStickyActionDocks`, `setupScrollProxy`, and `setupPaginationJump` functions in `main.js` use `AbortController` for listener cleanup and are re-run via `htmx:afterSwap` to safely re-initialise after each swap.

### Delete with count refresh (statements list page)

Statement deletion on the list page uses `hx-swap="delete"` to remove the row, then the server returns an `HX-Trigger: listUpdated` header. A client-side listener fetches the current count from `/statements/count` (an endpoint that returns OOB `<span>` elements) to update both the footer and sticky dock count chips with the authoritative server count.

## Vendor Assets (Self-Hosted)

All third-party CSS, JS, and fonts are self-hosted under `service/static/assets/vendor/`
instead of loading from external CDNs. This eliminates runtime dependencies on jsdelivr
and Google Fonts.

### What's vendored
- **Bootstrap 5.3.3** — CSS and JS bundle
- **HTMX 2.0.4** — partial page updates (see below)
- **Google Fonts** — Source Serif 4 (display) and Outfit (body), woff2 format

### Updating vendor assets
Run the download script to fetch the latest files:
```bash
./service/scripts/update-vendor-assets.sh
```

To update Bootstrap, change `BOOTSTRAP_VERSION` at the top of the script and re-run.
To change fonts or weights, update `GOOGLE_FONTS_URL` in the script and re-run.

The script downloads files into `service/static/assets/vendor/` and generates
`fonts.css` with `@font-face` declarations. Commit the updated files after running.

## Data Model

**Overview**
- Primary stores are DynamoDB tables for statement data/config/status and S3 for statement artefacts and cached Xero datasets (tables/bucket created in `cdk/stacks/statement_processor.py`).
- The structured statement JSON schema is produced by the Extraction Lambda (`lambda_functions/extraction_lambda/core/statement_processor.py`, `lambda_functions/extraction_lambda/core/models.py`) via Bedrock (Claude haiku-4-5) and consumed by the Flask service (`service/app.py`, `service/utils/storage.py`). The JSON is self-describing: it includes `header_mapping`, `date_format`, `date_confidence`, `decimal_separator`, and `thousands_separator` so the service does not need an external config lookup.

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
  - `RecordType` distinguishes row types: `"statement"` for headers (`service/billing_service.py:reserve_statement_uploads`) and `"statement_item"` for line items (`lambda_functions/extraction_lambda/core/statement_processor.py:_persist_statement_items`).
- **Writers**
  - Statement headers: `service/billing_service.py:reserve_statement_uploads` (initial record with billing metadata and `ProcessingStage=queued`).
  - Processing progress: `lambda_functions/extraction_lambda/core/processing_progress.py:update_processing_stage` (stage transitions during extraction).
  - Item rows + header updates: `lambda_functions/extraction_lambda/core/statement_processor.py` (writes item rows; sets `EarliestItemDate` and `LatestItemDate` on header).
  - Status updates: `service/utils/dynamo.py` (completion flags, item type updates, and `repair_processing_stage` read-repair on failure).
- **Readers**
  - `service/utils/dynamo.py` (list statements, read header + item status, delete statement data).
  - `service/app.py` (statement list/detail flows).
  - `lambda_functions/extraction_lambda/core/statement_processor.py` (reads header to preserve completion status during re-processing).
- **Example header item** (created by `service/billing_service.py:reserve_statement_uploads`, later updated by the Lambda):
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
  "PdfPageCount": 8,
  "ReservationLedgerEntryID": "reserve#<statement_id>",
  "TokenReservationStatus": "reserved",
  "ProcessingStage": "queued",
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
- `statement_item.total` is now treated as a dict-only `{label: value}` mapping in both service and extraction code paths; legacy list-style totals are no longer supported.
- **Processing stage lifecycle** — tracks extraction progress on statement header rows for the UI. S3 JSON existence remains the source of truth for "done vs not done" (avoids ordering issues between S3 upload and DynamoDB update); `ProcessingStage` only enriches the processing UI with granular progress.

  | Stage | Set by | Meaning |
  |---|---|---|
  | `queued` | Flask upload (`billing_service.py`) | Statement uploaded, waiting for Lambda |
  | `chunking` | Lambda (`statement_processor.py`) | Lambda started, splitting PDF into sections |
  | `extracting` | Lambda (`statement_processor.py`) | Bedrock extracting data from each section |
  | `post_processing` | Lambda (`statement_processor.py`) | Extraction complete, running validation |
  | `complete` | Lambda (`statement_processor.py`) | All processing finished, results available |
  | `failed` | Lambda (`main.py`) / Flask read-repair | Processing failed, tokens refunded |

  - `ProcessingProgress` (e.g. `"3/10"`) and `ProcessingTotalSections` (e.g. `10`) are transient — set during `extracting` stage, removed at `post_processing`.
  - If `ProcessingStage` is missing after migration, treat as `"failed"`.
  - Writers: `service/billing_service.py` (sets `queued`), `lambda_functions/extraction_lambda/core/processing_progress.py` (all other transitions), `service/utils/dynamo.py:repair_processing_stage` (read-repair on failure).

**TenantContactsConfigTable** — **REMOVED**
- This table has been removed as part of the Textract-to-Bedrock migration. Bedrock returns self-describing statement JSON that includes `header_mapping`, `date_format`, `date_confidence`, `decimal_separator`, and `thousands_separator` directly in the output. The service reads these metadata fields from the statement JSON in S3 instead of needing a per-contact `ContactConfig` in DynamoDB.
- The associated DynamoDB table, config suggestion pipeline, `/configs` UI route, and all `get_contact_config` / `contact_config_metadata` modules have been removed from both the service and Lambda codebases.

**TenantDataTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
- **Concept**
  - Tracks tenant‑level sync and load state only. Billing balance no longer lives here because this item is already updated by sync/load flows, so mixing mutable token balance into the same row would make atomic ledger+balance writes harder to reason about.
- **Writers**
  - `service/sync.py:update_tenant_status` (sets `TenantStatus`, `LastSyncTime`).
  - `service/sync.py:check_load_required` (seeds a row with `TenantStatus=LOADING`; also grants welcome tokens via `BillingService.adjust_token_balance` so new tenants can try the system immediately).
- **Readers**
  - `service/tenant_data_repository.py` and `service/app.py` (tenant status APIs/UI gating only).
- **Example item**:
```json
{
  "TenantID": "<tenant_id>",
  "TenantStatus": "LOADING",
  "LastSyncTime": 1706448896000
}
```

**TenantBillingTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
- **Concept**
  - Holds the current available token snapshot for each tenant. This table exists separately from `TenantDataTable` so billing services can update `TokenBalance` atomically with `TenantTokenLedgerTable` writes without sharing an item that is also mutated by sync/load workflows.
- **Writers**
  - `service/billing_service.py` reserves tokens on upload submit and releases them again if the web app cannot start processing.
  - `lambda_functions/extraction_lambda/core/billing.py` consumes reserved tokens on successful extraction and releases them on asynchronous workflow failure.
  - `scripts/manual_token_adjustment/manual_token_adjustment.py` applies manual grants/removals through the same atomic snapshot+ledger transaction logic. This exists so operator top-ups for test tenants do not bypass the audit trail or drift away from `TenantTokenLedgerTable`.
- **Readers**
  - `service/tenant_billing_repository.py`, `service/app.py`, and `service/utils/statement_upload_validation.py` (tenant-management token balance display and upload balance/preflight checks).
- **Example item**:
```json
{
  "TenantID": "<tenant_id>",
  "TokenBalance": 125,
  "UpdatedAt": "2026-03-16T12:05:10+00:00",
  "LastLedgerEntryID": "reserve#<statement_id>",
  "LastMutationType": "RESERVE",
  "LastMutationSource": "upload-submit"
}
```

**TenantTokenLedgerTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `TenantID`
  - Sort key: `LedgerEntryID`
- **Concept**
  - Append-only token/billing audit log for each tenant. The ledger is now the durable audit trail for upload reservations and later consume/release settlement.
- **Writers/readers**
  - `service/billing_service.py` writes `RESERVE` rows during upload submit.
  - `lambda_functions/extraction_lambda/core/billing.py` writes `CONSUME` or `RELEASE` rows after the Step Functions workflow reaches a terminal outcome.
  - Future billing/account pages in `service/` will query tenant ledger history.

**TenantTokenLedgerTable — Stripe purchase entries**
- `source="stripe-checkout"` (`LAST_MUTATION_SOURCE_STRIPE_CHECKOUT` in `service/billing_service.py`) identifies purchase credits in the ledger.
- `LedgerEntryID` for purchases follows the pattern `purchase#<session_id>` (e.g. `purchase#cs_test_xxx`), which cross-references the matching `StripeEventStoreTable` record for audit lookups.
- The `adjust_token_balance()` method accepts an optional `ledger_entry_id` kwarg; when supplied it is used directly instead of generating a random UUID, enabling conditional idempotency on the ledger write via `attribute_not_exists`.

**Welcome token grant**
- When a new tenant is first seen during the OAuth callback, `service/sync.py:check_load_required` grants `WELCOME_GRANT_TOKENS` (5) via `BillingService.adjust_token_balance` with `source="welcome-grant"` (`LAST_MUTATION_SOURCE_WELCOME_GRANT` in `service/billing_service.py`).
- The grant runs inside the `put_item` success path (after seeding the tenant row with `ConditionExpression="attribute_not_exists(TenantID)"`), so it only fires for genuinely new tenants.
- Grant failure is non-fatal — a nested `try/except` logs the error and allows login to continue. This means a new tenant who hits a transient DynamoDB issue during the grant will still be able to log in but will have zero tokens until manually adjusted.
- The `adjust_token_balance` call uses `if_not_exists(TokenBalance, :zero) + :token_delta` so it handles the case where no billing row exists yet.

**Banner system**
- `service/banner_service.py` implements a reusable provider-registry pattern for site-wide notification banners. Each provider is a `(tenant_id) -> Banner | None` callable registered at module import time.
- `service/app.py` injects banners into every authenticated page via the `inject_banners` context processor, which calls `get_banners()` with the tenant's dismissed-key set loaded from `TenantDataRepository.get_dismissed_banners`.
- `service/templates/base.html` renders the banner list as Bootstrap alerts, with an optional dismiss button that POSTs to `/api/banner/dismiss`.
- Dismissed banners are persisted as a string set (`DismissedBanners`) on the tenant's `TenantDataTable` row via `TenantDataRepository.dismiss_banner`, so dismissals survive across sessions.
- Current providers:
  - `welcome_grant_banner_provider` — unconditionally returns a success banner telling the tenant they received 5 free tokens, with a link to `/upload-statements`. Dismissible via `dismiss_key="welcome-grant"`.
  - `config_review_banner_provider` has been removed (config suggestion pipeline removed).
- Adding a new banner: write a function matching the `BannerProvider` protocol, call `register_banner_provider(fn)` at module level, and import the module during app startup.

**Manual token adjustments**
- Script: `scripts/manual_token_adjustment/manual_token_adjustment.py`
- Usage:
  - `python3.13 scripts/manual_token_adjustment/manual_token_adjustment.py <tenant_id> <token_delta>`
  - Example grant: `python3.13 scripts/manual_token_adjustment/manual_token_adjustment.py tenant-123 50`
  - Example removal: `python3.13 scripts/manual_token_adjustment/manual_token_adjustment.py tenant-123 -20`
- Behavior:
  - Loads `service/.env` by default so it targets the same AWS account/region/table names as the web app.
  - Prints current balance, proposed delta, and expected balance, then asks for one confirmation unless `--yes` is supplied.
  - Calls `service/billing_service.py:BillingService.adjust_token_balance`, which updates `TenantBillingTable` and writes a matching `ADJUSTMENT` row to `TenantTokenLedgerTable` in one DynamoDB transaction.
- Why this exists:
  - Manual DynamoDB edits are unsafe for billing because changing only `TokenBalance` would break the ledger audit trail. The script keeps the snapshot and ledger consistent.


**StripeEventStoreTable** (`cdk/stacks/statement_processor.py`)
- **Keys**
  - Partition key: `StripeEventID`
- **Concept**
  - Checkout-session idempotency store. Persisting processed session IDs here prevents double-crediting if the user refreshes `/checkout/success` after a successful payment. Keyed by the Stripe checkout session ID (`cs_xxx`) rather than a webhook event ID, because the MVP uses the success-redirect pattern rather than webhooks. When webhooks are added for subscriptions, the same table will absorb `invoice.paid` and other event IDs without schema changes.
- **Writers**
  - `service/stripe_repository.py:StripeRepository.record_processed_session` — written after tokens are credited on `/checkout/success`.
- **Readers**
  - `service/stripe_repository.py:StripeRepository.is_session_processed` — checked at the start of `/checkout/success` to short-circuit re-crediting.
  - `service/stripe_repository.py:StripeRepository.get_processed_session` — reads the stored record so the success page can display the original token count on refresh without re-calling Stripe.
- **Example item**:
```json
{
  "StripeEventID": "cs_test_xxx",
  "EventType": "checkout.session.completed",
  "TenantID": "<tenant_id>",
  "TokensCredited": 50,
  "LedgerEntryID": "purchase#cs_test_xxx",
  "ProcessedAt": "2026-03-20T10:00:00+00:00"
}
```

### S3 Layout
**Bucket**
- Name pattern: `dexero-statement-processor-{stage}` (`cdk/stacks/statement_processor.py`).

**Key structure for statements** (defined in `service/utils/storage.py`)
- PDFs: `{tenant_id}/statements/{statement_id}.pdf`
  - Written by: `service/app.py:_process_statement_upload` via `upload_statement_to_s3`.
  - Read by: Extraction Lambda (downloads PDF, chunks it, and sends pages to Bedrock).
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- JSON outputs: `{tenant_id}/statements/{statement_id}.json`
  - Written by: `lambda_functions/extraction_lambda/core/statement_processor.py:run_extraction`.
  - Read by: `service/utils/storage.py:fetch_json_statement` (used in `service/app.py` statement detail view).
  - Updated by: `service/app.py:_persist_classification_updates` (re‑uploads JSON after item type changes).
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- Key sanitisation: `_statement_s3_key` rejects path separators in `tenant_id`/`statement_id` to avoid path traversal in keys (`service/utils/storage.py`).
- Config suggestions: **Removed.** The `{tenant_id}/config-suggestions/` prefix is no longer used. Bedrock returns self-describing JSON with column mappings embedded, so the config suggestion pipeline has been removed entirely.

**Key structure for cached Xero datasets**
- `{tenant_id}/data/{resource}.json` where `resource` is one of `contacts`, `invoices`, `credit_notes`, `payments` (`service/xero_repository.py`, `service/sync.py`).
  - Written by: `service/sync.py` after fetching from Xero.
  - Read by: `service/xero_repository.py` (download to local cache when missing).
- `{tenant_id}/data/xero_by_contact/{contact_id}.json` — combined invoices, credit notes, and payments for a single contact.
  - Written by: `service/sync.py:build_per_contact_index()` after each sync.
  - Read by: `service/xero_repository.py:get_xero_data_by_contact()` on the statement detail page.

## Auto Config Suggestion — REMOVED

The auto config suggestion pipeline has been removed as part of the Textract-to-Bedrock migration. Bedrock returns self-describing statement JSON that includes column mappings (`header_mapping`), date format, and number formatting metadata directly in the extraction output. There is no longer a separate suggestion/confirmation step — uploads go straight to extraction.

Removed components: `service/core/config_suggestion.py`, `service/core/bedrock_client.py`, `service/core/date_disambiguation.py`, `/configs` route, `/api/configs/confirm` and `/api/configs/confirm-all` endpoints, config suggestion S3 prefix.

## Anomaly Detection Logic

**Anomaly detection and validation** (Extraction Lambda; `lambda_functions/extraction_lambda`)

- **Rule: Flag invalid dates on extraction**
  - Logic: In `_map_row_to_item(...)`, if a `date` field contains text but parsing with the configured format returns `None`, the item gets an `invalid-date` flag (`lambda_functions/extraction_lambda/core/transform.py`).
  - Why it exists: highlights rows where configured date parsing fails, signalling potentially incorrect mappings or malformed input.
  - Example: Statement date “32/13/2024” with format `DD/MM/YYYY` yields `invalid-date`.

- **Rule: Keyword‑based outlier flagging (`ml-outlier`)**
  - Logic: `apply_outlier_flags(...)` flags items when:
    - `number` is missing (`missing-number` issue), or
    - `number` / `reference` contains balance/summary keywords from `SUSPECT_TOKEN_RULES` (e.g. “brought forward”, “closing balance”, “amount due”).  
    The single‑token `balance` rule only triggers when the text is short (≤3 tokens) and contains no digits; `summary` only triggers when short (≤3 tokens) (`lambda_functions/extraction_lambda/core/validation/anomaly_detection.py`).
  - Why it exists: it is intended to catch non‑transaction rows like balances and summary lines that often appear in statements.
  - Example: “Balance brought forward” or “Amount due” in a reference field is flagged; “Balance 2023” is not flagged by the single‑token “balance” rule because it includes digits.

- **Rule: Flags are additive and preserved**
  - Logic: Flagged items get `_flags` (list of strings) plus `FlagDetails[FLAG_LABEL]` with structured issues/details; `remove=False` keeps rows and only annotates them. `run_extraction` calls `apply_outlier_flags(..., remove=False)` so items are preserved (`lambda_functions/extraction_lambda/core/validation/anomaly_detection.py`, `lambda_functions/extraction_lambda/core/statement_processor.py`).
  - Why it exists: enables UI warnings without dropping data. `_flags` is kept as legacy to not break UI logic currently. Once UI is updated it will be completely replaced by `FlagDetails`
  - Example: A row with `ml-outlier` is still shown in the UI but highlighted as anomalous.

## Xero Matching Logic

- Payment-looking references are excluded from substring matching

## Clear Data Script (`scripts/clear_ddb_and_s3/clear_ddb_and_s3.py`)

This script clears the resources configured in `service/.env`:
- `S3_BUCKET_NAME`
- `TENANT_STATEMENTS_TABLE_NAME`
- `TENANT_DATA_TABLE_NAME`

### Scope
- Default behaviour is unchanged: running the script without a tenant filter deletes data for **all tenants** from the configured bucket and tables.
- `--tenant-id <tenant_id>` narrows the delete to one tenant:
  - DynamoDB deletes only rows where the partition key is `TenantID=<tenant_id>`.
  - S3 deletes only keys under `{tenant_id}/`.
- The script intentionally does not expand into billing or ledger tables. That preserves the existing reset workflow and only reduces the blast radius when you need a tenant-specific cleanup.

### Why tenant-scoped deletion works
- The targeted DynamoDB tables are keyed by `TenantID`, so the script can issue a partition query instead of scanning and deleting unrelated tenants.
- Statement artefacts and cached Xero datasets are stored under tenant prefixes such as `{tenant_id}/statements/...` and `{tenant_id}/data/...`, so an S3 prefix delete cleanly maps to one tenant.

### Example usage
```bash
cd statement-processor

# Delete data for every tenant in the configured resources
python3.13 scripts/clear_ddb_and_s3/clear_ddb_and_s3.py

# Delete data for one tenant only
python3.13 scripts/clear_ddb_and_s3/clear_ddb_and_s3.py --tenant-id <tenant_id>

# Skip the confirmation prompt
python3.13 scripts/clear_ddb_and_s3/clear_ddb_and_s3.py --tenant-id <tenant_id> --yes
```

## Tenant Snapshot Script (`scripts/tenant_snapshot/tenant_snapshot.py`)

This script backs up and restores **statement PDFs** for a single tenant using environment variables only (no CLI args).

### What it does
- **Backup mode** (`TENANT_SNAPSHOT_MODE=backup`):
  - Reads statement header rows for `TENANT_ID` from `TenantStatementsTable` (`RecordType=statement` / missing).
  - Downloads each statement PDF from `s3://$S3_BUCKET_NAME/{tenant_id}/statements/{statement_id}.pdf`.
  - Writes snapshot files under `TENANT_SNAPSHOT_DIR/<TENANT_ID>/`:
    - `statements_manifest.json`
    - `pdfs/<statement_id>.pdf`
- **Restore mode** (`TENANT_SNAPSHOT_MODE=restore`):
  - Re-uploads PDFs from `pdfs/` to S3 using **new** statement IDs.
  - Recreates statement header rows in `TenantStatementsTable` (new IDs, fresh `UploadedAt`, `Completed=false`).
  - Optionally starts Extraction Step Functions for each restored statement to regenerate JSON/item rows via Bedrock.

### Environment variables
- Required:
  - `TENANT_ID`
  - `TENANT_SNAPSHOT_MODE` (`backup` or `restore`)
- Usually loaded from `service/.env` (or set manually):
  - `S3_BUCKET_NAME`
  - `TENANT_STATEMENTS_TABLE_NAME`
- Optional:
  - `TENANT_SNAPSHOT_ENV_FILE` (default: `service/.env`)
  - `TENANT_SNAPSHOT_DIR` (default: `scripts/tenant_snapshot/snapshots`)
  - `TENANT_SNAPSHOT_YES=true` (skip confirmation prompt)
  - `TENANT_SNAPSHOT_START_WORKFLOWS=true|false` (restore only; default `true`)
  - `TENANT_SNAPSHOT_WORKFLOW_DELAY_SECONDS` (restore only; default `1`, waits between workflow starts to reduce throughput errors)
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
- Restore intentionally creates **new statement IDs**. Statement JSON and item rows are regenerated by extraction.
- If you disable workflow starts (`TENANT_SNAPSHOT_START_WORKFLOWS=false`), PDFs + statement headers are restored but JSON/item rows will not exist until processing is triggered later.
- This script is designed for operational reset/reseed workflows, not perfect forensic restoration of every historical field.

## Sonnet Extraction Test Script (`scripts/replace_textract_test/run.py`)

Exploratory test script that validated the feasibility of replacing Textract with an LLM (Sonnet 4.6 via Bedrock) for statement extraction. This script was the precursor to the production migration, which uses haiku-4-5 instead of Sonnet for cost reasons (see **Textract to Bedrock Migration** section above).

### How it works

1. Reads all PDFs from `scripts/replace_textract_test/pdfs/`.
2. Chunks large PDFs at ~10 pages per request with **1-page overlap** between chunks. The overlap ensures rows spanning page boundaries are captured. If any chunk exceeds 4 MB (Bedrock document block limit), it is recursively halved.
3. Calls Bedrock Converse API (Sonnet 4.6) with forced tool use (`extract_statement_rows`) for structured JSON output.
4. **Header propagation:** chunk 1's detected column headers are passed to subsequent chunks so the LLM can map columns even when headers only appear on page 1. Single-chunk PDFs skip continuation prompts entirely.
5. Post-processes monetary strings to floats — handles trailing minus (`126.50-`), parenthetical negatives (`(126.50)`), and configurable decimal/thousands separators.
6. Writes per-PDF JSON to `output/` and a `run_summary.json` with per-PDF stats and cost estimates.

### Why 10-page chunks with overlap

Large PDFs (up to 70 pages) are chunked to stay within Sonnet's context window (200K tokens) and to avoid "lost in the middle" accuracy degradation. The 1-page overlap handles rows that span page boundaries — the continuation prompt tells the LLM to skip already-extracted rows from the overlap page.

### Retry logic

Retries transient Bedrock errors (`ThrottlingException`, `InternalServerException`, `ServiceUnavailableException`) up to 2 times with exponential backoff. Fails immediately on client/validation errors. If a chunk fails after all retries, the entire PDF is failed (partial results are not useful) and the script moves to the next PDF.

### Cost model

Uses Sonnet 4.6 pricing ($3/M input tokens, $15/M output tokens). Token counts come from the Bedrock response `usage` field. Cost is summed per PDF and across the full run.

### Setup and usage

```bash
cd scripts/replace_textract_test

# Create venv and install deps
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Copy test PDFs into the pdfs/ directory
cp /path/to/test/pdfs/*.pdf pdfs/

# Run (uses AWS_PROFILE=dotelastic-production by default)
python3.13 run.py

# Override AWS profile
AWS_PROFILE=my-profile python3.13 run.py
```

### Output

- Per-PDF JSON: `output/{pdf_stem}.json` — contains `detected_headers`, `date_format`, `decimal_separator`, `thousands_separator`, `items` (with numeric totals), item count, timing, and cost estimate.
- Run summary: `output/run_summary.json` — per-PDF stats (filename, page count, chunk count, item count, time, cost, status) and totals.

## Stripe Setup

### One-time dashboard steps
1. A Product named **"Statement Processing Pages"** already exists in Stripe test mode with ID `prod_UBMoFkqStKFcjg`. No Price objects are needed — pricing is fully dynamic via `price_data` in each checkout session.
2. Enable Invoicing on the Stripe account (Dashboard → Settings → Billing → Invoices). Required because checkout sessions are created with `invoice_creation={"enabled": True}`.
3. For live mode: repeat the above with live-mode keys and update `STRIPE_PRODUCT_ID` and `STRIPE_API_KEY_SSM_PATH` accordingly.

### SSM parameter
```bash
aws ssm put-parameter \
  --name "/StatementProcessor/STRIPE_API_KEY" \
  --type SecureString \
  --value "sk_test_xxx"   # or sk_live_xxx for production
```
The path is read at startup via `STRIPE_API_KEY_SSM_PATH` env var (already set in `service/.env` and the CDK stack).

### Environment variables
| Variable | Example value | Purpose |
|---|---|---|
| `STRIPE_API_KEY_SSM_PATH` | `/StatementProcessor/STRIPE_API_KEY` | SSM path for the secret key — resolved at startup |
| `STRIPE_PRODUCT_ID` | `prod_UBMoFkqStKFcjg` | Stripe Product ID for page purchases |
| `STRIPE_PRICE_PER_TOKEN_PENCE` | `10` | **Legacy** — no longer read by app code. Graduated pricing is now defined in `service/pricing_config.py` |
| `STRIPE_CURRENCY` | `gbp` | Stripe currency code |
| `STRIPE_MIN_TOKENS` | `10` | **Legacy** — min/max now defined in `service/pricing_config.py` (`MIN_TOKENS`/`MAX_TOKENS`) |
| `STRIPE_MAX_TOKENS` | `10000` | **Legacy** — see above |

All non-secret variables are plain env vars (in `service/.env` for local dev; in the CDK `environment_variables` block for AppRunner). Only the secret key is stored in SSM.

### Design decisions
- **No webhooks for MVP.** Token crediting happens on the success redirect: the session is retrieved from Stripe, `payment_status` is verified, and tokens are credited. Idempotency prevents double-crediting on page refresh. If the user's browser closes before the redirect fires, tokens are credited manually via the admin adjustment tool. Webhooks will be added when subscriptions require reliable async credit.
- **Dynamic `price_data` not fixed Prices.** Page count is a free-form integer with graduated pricing, so a fixed Price object cannot represent every possible purchase. One Product is reused across all purchases for correct Stripe reporting attribution.
- **Persistent Stripe Customer per tenant.** A single Stripe Customer is created on first purchase and its ID stored in `TenantBillingTable.StripeCustomerID`. Subsequent purchases call `stripe.Customer.modify` to update billing details before creating the checkout session. Invoices snapshot billing details at creation time, so historical invoices retain the details entered for that specific purchase. This replaces the earlier per-checkout customer approach.
- **Graduated pricing via `PricingConfig`.** `service/pricing_config.py` defines graduated tiers as the single source of truth for both Python (server-side validation, Stripe session creation) and JavaScript (live price calculator via JSON serialisation). The effective per-token rate is stored in ledger entries (`PricePerTokenPence`) for audit trail; the Stripe invoice remains authoritative for exact per-tier breakdowns.
- **No VAT.** Not VAT-registered (UK businesses below the £90k threshold). No `tax_rates` on line items.

### Testing
Use Stripe test cards:
- `4242 4242 4242 4242` — successful payment
- `4000 0000 0000 0002` — declined card

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
   - Bedrock will auto-detect column mappings during extraction (no manual config step needed).
4) Populate Xero from the extracted statement JSON:
   - Run `python3.13 scripts/populate_xero/populate_xero.py`.
   - The script defaults to Demo Company (UK) and the Test Statements Ltd statement/contact IDs; override as needed with `TENANT_ID`, `STATEMENT_ID`, and `CONTACT_ID` env vars.
6) Capture the Excel baseline:
   - From the statement detail page, click “Download Excel”.
   - Save it as `service/playwright_tests/fixtures/expected/test_statements_ltd.xlsx`.

### Notes
- The population script intentionally skips “no match”, “balance forward”, and invalid date rows so the UI shows both matched and unmatched cases.
- If the Demo Company tenant resets, repeat the setup steps above to restore the fixture.
