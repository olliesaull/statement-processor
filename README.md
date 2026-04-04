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
│   ├── populate_xero/
│   │   ├── populate_xero.py
│   │   └── requirements.txt
│   └── replace_textract_test/
│       ├── run.py
│       ├── system_prompt.md
│       └── requirements.txt
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
    ├── sync.py
    ├── tenant_data_repository.py
    ├── xero_repository.py
    ├── core/
    │   ├── __init__.py
    │   ├── bedrock_client.py
    │   ├── config_suggestion.py
    │   ├── contact_config_metadata.py
    │   ├── date_disambiguation.py
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
  - `TenantDataTable` (`tenant_data_table`): shared tenant state table wired into both App Runner and the Textraction Lambda via env vars and IAM grants; this now stays focused on sync/load metadata rather than mutable billing balance state.
  - `TenantBillingTable` (`tenant_billing_table`): dedicated tenant billing snapshot table keyed by `TenantID`; shared by App Runner and the Textraction Lambda because uploads reserve tokens in the web app while asynchronous consume/release settlement happens after the Step Functions workflow finishes. Keeping this snapshot separate from `TenantDataTable` lets balance writes stay atomic with the token ledger without colliding with sync/load metadata.
  - `TenantTokenLedgerTable` (`tenant_token_ledger_table`): append-only tenant billing ledger table keyed by `TenantID` + `LedgerEntryID`; shared by App Runner and the Textraction Lambda because both runtimes now participate in the token lifecycle (`RESERVE` on upload, `CONSUME` on success, `RELEASE` on failure).
  - `StripeEventStoreTable` (`stripe_event_store_table`): Stripe webhook idempotency table keyed by `StripeEventID`; exposed only to App Runner because webhook verification and deduplication terminate in the Flask service, not the Textraction Lambda.
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
  - App Runner now receives explicit `AWS_REGION` and `AWS_DEFAULT_REGION` runtime env vars from CDK instead of relying on platform-injected defaults. Rationale: `service/config.py` creates boto3 clients during module import, and the Flask worker boot path must not depend on whether App Runner happens to inject a default region variable.
  - App Runner health checks now use HTTP against `/healthz` instead of raw TCP. The Flask route returns `200` with an empty body. Rationale: TCP only proves the Gunicorn master socket is open; it does not prove a worker successfully booted or that Flask can return a response. `/healthz` is a tiny unauthenticated route with no template or business-logic work, so it is a better signal for whether Flask can actually handle requests.
  - Lambda does not set `AWS_REGION` manually in CDK. Rationale: Lambda reserves that environment variable name and already injects the runtime region automatically.
  - Production public-domain settings are configured in `cdk/app.py` (`PROD_DOMAIN_NAME`) and consumed by `cdk/stacks/statement_processor.py` to set CloudFront aliases and the OAuth callback host consistently.
- **IAM roles and policies**
  - `Statement Processor App Runner Instance Role` (`statement_processor_instance_role`): grants App Runner access to CloudWatch metrics, Textract, and Step Functions; table and S3 permissions are added via grants.
  - Web Lambda runtime no longer requires `ssm:GetParameter`/`kms:Decrypt` for Xero/session secrets; `cdk/deploy_stack.sh` reads SSM secure parameters before deploy and passes them into CDK as deploy-time environment variables for Lambda. This removes per-cold-start SSM/KMS network calls from the Flask service startup path.
  - `cdk/deploy_stack.sh` now runs a bounded Docker multi-arch preflight before `cdk deploy`: it reuses any existing `buildx` builder that already advertises `linux/arm64` (preferring the active/default builder before creating the repo-specific `multiarch` builder), skips the privileged `tonistiigi/binfmt` refresh when an initial `linux/arm64` smoke test already succeeds, and wraps bootstrap/runtime checks in explicit progress messages plus timeouts. Rationale: first-run image pulls and stale custom builders were previously hidden behind `/dev/null`, which made deploys look stuck at the Docker multi-arch step even when Docker was still bootstrapping emulation.
  - Textract permissions added to both Lambda and state machine roles to allow `StartDocumentAnalysis` and `GetDocumentAnalysis`.
- **CloudWatch + SNS**
  - `StatementProcessorAppRunnerErrorMetricFilter` + `StatementProcessorAppRunnerErrorAlarm`: parses App Runner application logs for `ERROR` and raises an alarm.
  - `TextractionLambdaErrorMetricFilter` + `TextractionLambdaErrorAlarm`: parses Textraction Lambda logs for `ERROR` or timeout strings and raises an alarm.
  - `StatementProcessorAppRunnerErrorTopic`: SNS topic that both alarms publish to. It has email subscriptions for `ollie@dotelastic.com` and `james@dotelastic.com`.

## Monitoring and Notifications

### CloudWatch Alarms
Two CloudWatch metric filters + alarms watch for errors in production:

| Alarm | Log group | Trigger |
|---|---|---|
| `StatementProcessorAppRunnerErrorAlarm` | App Runner application logs | Any log line containing `ERROR` |
| `TextractionLambdaErrorAlarm` | Textraction Lambda logs | Any log line containing `ERROR` or a timeout string |

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

## Orchestration (Step Functions & Textract)
**State machine definitions and entry points**
- `TextractionStateMachine` is defined in `cdk/stacks/statement_processor.py` as a single chainable state machine built from `StartTextractDocumentAnalysis` -> `WaitForTextract` -> `GetTextractStatus` -> `IsTextractFinished?` -> `ProcessStatement` -> `DidStatementProcessingSucceed?`. The workflow now invokes the same Lambda on both Textract success and Textract failure so billing settlement always runs before the execution ends.
- Executions are started from the Flask service via `service/utils/workflows.py:start_textraction_state_machine`, invoked during upload in `service/app.py:_process_statement_upload`.

**Step-by-step flow (code-grounded)**
1. Upload handler registers statement metadata and starts the state machine (`service/app.py:_process_statement_upload` -> `service/utils/workflows.py:start_textraction_state_machine`).
2. Step Functions calls Textract `startDocumentAnalysis` with the S3 PDF location (`StartTextractDocumentAnalysis` in `cdk/stacks/statement_processor.py`).
3. Workflow waits 10 seconds (`WaitForTextract`).
4. Workflow calls `getDocumentAnalysis` to check `JobStatus` (`GetTextractStatus`).
5. If status is `SUCCEEDED` or `PARTIAL_SUCCESS`, invoke `TextractionLambda` with job id + S3 keys (`ProcessStatement`).
6. If status is `FAILED`, invoke the same Lambda with `textractStatus=FAILED` so it can release the earlier token reservation instead of leaving tokens stuck in `reserved`.
7. Otherwise, loop back to wait and poll again until timeout.
8. Lambda retrieves paginated Textract results, builds statement JSON, persists items, and writes JSON to S3 (`lambda_functions/textraction_lambda/core/extraction.py` + `lambda_functions/textraction_lambda/core/textract_statement.py`). On success it consumes the earlier reservation; on failure it releases the reservation back to `TenantBillingTable`.
9. `lambda_functions/textraction_lambda/main.py` returns a compact metadata payload (IDs, `jsonKey`, filename/date/item summary) instead of embedding the full statement JSON in state output; Step Functions now branches on `Payload.status` so billing failures and processing failures explicitly fail the execution.

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
    - `/upload-statements` (GET/POST): upload PDFs and trigger textraction (requires tenant + Xero auth, blocks while loading). The page keeps the lightweight client-side page estimate for instant feedback, but the POST handler now reserves tokens atomically before any S3 upload starts. Reservation writes update `TenantBillingTable`, append `RESERVE` rows to `TenantTokenLedgerTable`, and create the statement header rows with `PdfPageCount`, `ReservationLedgerEntryID`, and `TokenReservationStatus=reserved`. If the web app cannot upload the PDF or start Step Functions after reservation, it immediately releases the reservation and cleans up the statement row again.
    - `/api/upload-statements/preflight` (POST): authoritative upload validation endpoint used by the upload page before submit. It accepts the currently selected PDFs, counts their pages server-side with `pypdf` (while the browser keeps its own lightweight estimate for instant UX), reads `TenantBillingTable.TokenBalance`, and returns per-file counts plus `total_pages`, `available_tokens`, `shortfall`, and `can_submit`. This exists so the UI can warn about insufficient tokens before the final upload request without trusting the browser-only estimate.
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
    - `/api/banner/dismiss` (POST): permanently dismiss a banner for the current tenant. Accepts `{"dismiss_key": "<key>"}` and writes the key to the tenant's `DismissedBanners` set in `TenantDataTable` via `TenantDataRepository.dismiss_banner`. Returns `204` on success.
    - **Auth behavior for API routes**: When `@xero_token_required` protects a `/api/...` endpoint and the session token is missing or expired, the decorator returns `401` JSON (`{"error": "auth_required"}`) instead of redirecting. The frontend polling/sync code (`service/static/assets/js/main.js`) treats either a 401 response or a redirected login response as a signal to navigate to `/login`, so passive actions still force a full re-login.
  - **Stripe / token purchasing**
    - `/pricing` (GET): public-facing pricing page — no auth required, explains token pricing (£0.10/token, min 10 tokens).
    - `/buy-tokens` (GET): token amount input form with live price display; requires Xero auth.
    - `/api/checkout/create` (POST): validates billing details submitted from the billing details form, creates a fresh Stripe Customer for this purchase with the user-provided name, email, and address, creates a Stripe Checkout Session with dynamic `price_data`, and redirects to the Stripe-hosted checkout page. A new Customer is created per checkout so each invoice is permanently attached to the billing details entered for that specific purchase.
    - `/checkout/success` (GET): called by Stripe on payment completion with `?session_id=cs_xxx`. Retrieves the session from Stripe, verifies `payment_status == "paid"`, checks idempotency via `StripeEventStoreTable`, credits tokens via `BillingService.adjust_token_balance` with `source="stripe-checkout"` and a deterministic `ledger_entry_id`, then records the session as processed. Renders the success page with tokens credited and updated balance. Safe to refresh — idempotency check prevents double-crediting.
    - `/checkout/cancel` (GET): renders a cancellation page with a "Try Again" link; no tokens are credited.
    - `/checkout/failed` (GET): renders an error page with a hex reference ID for support lookup.
    - **Preflight shortfall link**: when `/api/upload-statements/preflight` returns `shortfall > 0`, the response JSON now includes `buy_tokens_url: "/buy-tokens"` so the upload page JS can render a "Buy Tokens" link in the red shortfall summary.
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
  - `upload_statements` validates file/contact counts, enforces PDF MIME/extension rules (`service/utils/storage.py:is_allowed_pdf`), verifies a contact config exists (`_ensure_contact_config`), and then calls `service/billing_service.py` to reserve tokens atomically before any upload starts.
  - `service/billing_service.py:reserve_statement_uploads`:
    - Decrements `TenantBillingTable.TokenBalance` conditionally.
    - Appends one `RESERVE` row per statement to `TenantTokenLedgerTable`.
    - Creates the initial `TenantStatementsTable` header row with `PdfPageCount`, `ReservationLedgerEntryID`, and `TokenReservationStatus=reserved`.
  - `_process_statement_upload`:
    - Uploads PDF to S3 (`upload_statement_to_s3` → `service/utils/storage.py`).
    - Computes JSON output key (`statement_json_s3_key`) and starts Step Functions (`start_textraction_state_machine` → `service/utils/workflows.py`).
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
| Changing the listen port | Update `listen` directive in `service/nginx.conf` |

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

## Frontend Design System

The site uses Bootstrap 5.3.3 (loaded via CDN) with a custom design token layer in `service/static/assets/css/main.css`. The design is intentionally approachable and trustworthy — targeting SMB finance teams who use Xero.

- **Fonts**: Source Serif 4 (display/headings) + Outfit (body), loaded via Google Fonts CDN in `base.html`.
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
- **Google Fonts CDN over self-hosting**: Simpler to implement. Fonts are loaded via `<link>` tags in `base.html`, not `@import` in CSS (avoids render-blocking waterfall).

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
  - `RecordType` distinguishes row types: `"statement"` for headers (`service/billing_service.py:reserve_statement_uploads`) and `"statement_item"` for line items (`lambda_functions/textraction_lambda/core/textract_statement.py:_persist_statement_items`).
- **Writers**
  - Statement headers: `service/billing_service.py:reserve_statement_uploads` (initial record with billing metadata).
  - Item rows + header updates: `lambda_functions/textraction_lambda/core/textract_statement.py` (writes item rows; sets `EarliestItemDate`, `LatestItemDate`, `JobId` on header).
  - Status updates: `service/utils/dynamo.py` (completion flags and item type updates).
- **Readers**
  - `service/utils/dynamo.py` (list statements, read header + item status, delete statement data).
  - `service/app.py` (statement list/detail flows).
  - `lambda_functions/textraction_lambda/core/textract_statement.py` (reads header to preserve completion status during re‑processing).
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
  - `lambda_functions/textraction_lambda/core/billing.py` consumes reserved tokens on successful textraction and releases them on asynchronous workflow failure.
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
  - `lambda_functions/textraction_lambda/core/billing.py` writes `CONSUME` or `RELEASE` rows after the Step Functions workflow reaches a terminal outcome.
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
  - `config_review_banner_provider` — shows an info banner when the tenant has pending config review suggestions (count > 0), with a link to `/configs`. Not dismissible because the count is dynamic.
  - `welcome_grant_banner_provider` — unconditionally returns a success banner telling the tenant they received 5 free tokens, with a link to `/upload-statements`. Dismissible via `dismiss_key="welcome-grant"`.
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
  - Read by: Textract (via Step Functions) and the Textraction Lambda.
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- JSON outputs: `{tenant_id}/statements/{statement_id}.json`
  - Written by: `lambda_functions/textraction_lambda/core/textract_statement.py:run_textraction`.
  - Read by: `service/utils/storage.py:fetch_json_statement` (used in `service/app.py` statement detail view).
  - Updated by: `service/app.py:_persist_classification_updates` (re‑uploads JSON after item type changes).
  - Deleted by: `service/utils/dynamo.py:delete_statement_data`.
- Key sanitisation: `_statement_s3_key` rejects path separators in `tenant_id`/`statement_id` to avoid path traversal in keys (`service/utils/storage.py`).
- Config suggestions: `{tenant_id}/config-suggestions/{statement_id}.json`
  - Written by: `service/core/config_suggestion.py:suggest_config_for_statement` after Textract + Bedrock analysis.
  - Read by: `service/core/config_suggestion.py:get_pending_suggestions` (loaded on `/configs` page).
  - Deleted by: `service/core/config_suggestion.py:delete_suggestion` (after user confirms config).
  - Contents: `ConfigSuggestion` model — contact metadata, detected headers, suggested column mappings, and confidence notes.

**Key structure for cached Xero datasets**
- `{tenant_id}/data/{resource}.json` where `resource` is one of `contacts`, `invoices`, `credit_notes`, `payments` (`service/xero_repository.py`, `service/sync.py`).
  - Written by: `service/sync.py` after fetching from Xero.
  - Read by: `service/xero_repository.py` (download to local cache when missing).

## Auto Config Suggestion

When a statement is uploaded for a contact that has no saved config, the system auto-detects column mappings instead of blocking the upload.

### How it works
1. **Upload**: The PDF is uploaded to S3 and a DynamoDB header row is created with `Status=pending_config_review`. No tokens are reserved at this stage.
2. **Textract**: A background thread extracts page 1 as a single-page PDF (to avoid the sync API's multi-page limitation) and runs `AnalyzeDocument` with `FeatureTypes=["TABLES"]`.
3. **Bedrock Haiku 4.5**: The detected headers and sample rows are sent to Bedrock via the Converse API with a forced tool call (`suggest_config`). The prompt includes a full SDF token reference table so the LLM returns date formats in the correct syntax.
4. **Date disambiguation**: A post-processing step scans date values for components > 12 to confirm or reject the LLM's DD/MM vs MM/DD proposal. If all values are ambiguous (both components <= 12), the date format is left empty for the user to fill in.
5. **S3 save**: The suggestion (including detected headers, suggested config, and confidence notes) is saved to `{tenant_id}/config-suggestions/{statement_id}.json`.
6. **User confirmation**: The `/configs` page shows pending review cards with dropdowns pre-filled from the detected headers. Users can adjust mappings and confirm. On confirmation, tokens are reserved via `BillingService.reserve_confirmed_statement()` (deducting from the tenant balance and stamping reservation metadata on the statement header), the config is saved to DynamoDB, the suggestion file is deleted, and the extraction step function is started. Token reservation is deferred to this point (rather than upload time) to avoid locking tokens for abandoned suggestions.

### Status values
- `pending_config_review`: Suggestion generated successfully, awaiting user confirmation.
- `config_suggestion_failed`: Textract or Bedrock failed — the user must configure manually via the full config editor.

### Bedrock model access
The Bedrock Converse API requires model access to be enabled in the AWS console. The EU cross-region inference profile `eu.anthropic.claude-haiku-4-5-20251001-v1:0` must be enabled in the deployment region. This is a manual console step — CDK only grants the IAM permissions.

### New files
| File | Purpose |
|------|---------|
| `service/core/date_disambiguation.py` | DD/MM vs MM/DD disambiguation from date values |
| `service/core/bedrock_client.py` | Bedrock Converse API wrapper with tool use for config suggestion |
| `service/core/config_suggestion.py` | Orchestrator: Textract → Bedrock → S3 → DynamoDB status |

### API endpoints
- `POST /api/configs/confirm` — Confirm a single suggested config and start extraction.
- `POST /api/configs/confirm-all` — Confirm multiple configs in one request. Skips invalid ones.

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

## Clear Data Script (`scripts/clear_ddb_and_s3/clear_ddb_and_s3.py`)

This script clears the resources configured in `service/.env`:
- `S3_BUCKET_NAME`
- `TENANT_CONTACTS_CONFIG_TABLE_NAME`
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

## Sonnet Extraction Test Script (`scripts/replace_textract_test/run.py`)

Test script that validates replacing Textract with Sonnet 4.6 (via Bedrock) for statement line-item extraction. Textract struggles with structurally diverse PDFs — misidentifies section titles as headers, can't handle multi-line headers, and requires growing workarounds. Sonnet gives structural understanding at comparable cost (~10% more).

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
1. A Product named **"Statement Processor Tokens"** already exists in Stripe test mode with ID `prod_UBMoFkqStKFcjg`. No Price objects are needed — pricing is fully dynamic via `price_data` in each checkout session.
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
| `STRIPE_PRODUCT_ID` | `prod_UBMoFkqStKFcjg` | Stripe Product ID for token purchases |
| `STRIPE_PRICE_PER_TOKEN_PENCE` | `10` | Price per token in pence (10p = £0.10) |
| `STRIPE_CURRENCY` | `gbp` | Stripe currency code |
| `STRIPE_MIN_TOKENS` | `10` | Minimum tokens per purchase (£1.00 minimum) |
| `STRIPE_MAX_TOKENS` | `10000` | Maximum tokens per purchase |

All non-secret variables are plain env vars (in `service/.env` for local dev; in the CDK `environment_variables` block for AppRunner). Only the secret key is stored in SSM.

### Design decisions
- **No webhooks for MVP.** Token crediting happens on the success redirect: the session is retrieved from Stripe, `payment_status` is verified, and tokens are credited. Idempotency prevents double-crediting on page refresh. If the user's browser closes before the redirect fires, tokens are credited manually via the admin adjustment tool. Webhooks will be added when subscriptions require reliable async credit.
- **Dynamic `price_data` not fixed Prices.** Token count is a free-form integer, so a fixed Price object cannot represent every possible purchase. One Product is reused across all purchases for correct Stripe reporting attribution.
- **Fresh Stripe Customer per checkout, not a persistent one per tenant.** A new Stripe Customer is created on every purchase using the billing details the user enters at that point. The previous approach reused a single persistent Customer per tenant and called `stripe.Customer.modify` on every purchase to overwrite its name, email, and address — which corrupted the customer record on subsequent purchases, since the Stripe Customer (and its attached invoice) for an earlier purchase would then show the billing details from a later one. Creating a fresh Customer per checkout means each invoice is permanently attached to a Customer record whose name, email, and address exactly reflect what the user entered for that specific purchase. Because there is no persistent Customer to pre-fill from, the billing address fields are always blank on the form; only name and email are pre-filled from the active Xero session.
- **No billing address pre-fill from DynamoDB.** The previous implementation cached billing details (name, email, address) in `TenantBillingTable` after each purchase and read them back to pre-fill the billing details form on repeat visits. This caching is no longer done — since each checkout creates a fresh Customer, there is no persistent record to pre-fill address data from. Name and email are still pre-filled from the Xero session (not DynamoDB) as a convenience.
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
