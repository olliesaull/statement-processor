# Project Overview

This repository implements a supplier statement reconciliation system for Xero.
Users upload supplier statement PDFs, the system extracts line items with Bedrock, maps them to Xero bills/credit notes/payments, and presents a reconciliation UI plus Excel export.

## Purpose

- Primary user outcome: reconcile supplier statements against Xero records faster and with fewer manual errors.
- Primary users: finance/bookkeeping users operating within a selected Xero tenant.
- Core constraints:
  - Multi-tenant data isolation by `TenantID`.
  - Financial-document correctness is more important than speed for final output.
  - Extraction is asynchronous (Bedrock + Step Functions), so UI must handle processing states.
  - Step Functions payload size is limited, so full extracted JSON is stored in S3, not passed between states.

## Runtime Architecture

### Web application (`service/`)
- Flask app in [`service/app.py`](service/app.py) serves server-rendered Jinja pages and JSON APIs.
- Deployed on AWS AppRunner (see [`service/Dockerfile`](service/Dockerfile)).
- Nginx reverse proxy sits in front of Gunicorn inside the container, providing security headers, CSP, rate limiting, and per-route query string / body size validation. Config lives in `service/nginx.conf` and auto-generated `service/nginx-routes.conf`. See the README "Nginx Reverse Proxy" section for maintenance details.
- **Nginx regeneration required** when adding/removing Flask routes, changing auth decorators (which affects public-page detection), or adding query parameters to routes. Public routes have query strings stripped unless they have an entry in `service/nginx_route_querystring_allow_list.json`. Run the generator from `service/` and review the `nginx-routes.conf` diff.

### Extraction pipeline (`lambda_functions/extraction_lambda/`)
- Container Lambda entry point: [`lambda_functions/extraction_lambda/main.py`](lambda_functions/extraction_lambda/main.py).
- Converts PDF pages into structured statement JSON via Bedrock, applies anomaly flags, persists item rows to DynamoDB, uploads JSON to S3, and updates statement metadata.
- Main orchestrator: [`lambda_functions/extraction_lambda/core/statement_processor.py`](lambda_functions/extraction_lambda/core/statement_processor.py).

### Workflow orchestration (Step Functions)
- State machine defined in [`cdk/stacks/statement_processor.py`](cdk/stacks/statement_processor.py):
  - `ProcessStatement` (invoke Extraction Lambda)
  - `DidStatementProcessingSucceed?` to branch on the Lambda payload status before the execution ends
- Started by web app upload flow via [`service/utils/workflows.py`](service/utils/workflows.py).

### Data stores
- DynamoDB:
  - `TenantStatementsTable`
  - `TenantContactsConfigTable`
  - `TenantDataTable`
  - `TenantBillingTable`
  - `TenantTokenLedgerTable`
- S3 bucket: `dexero-statement-processor-{stage}` for statement files and cached Xero datasets.

### External integrations
- Xero OAuth + Accounting API:
  - OAuth login/callback in Flask.
  - Background sync caches contacts/invoices/payments/credit notes.
- Amazon Bedrock (Claude Haiku 4.5):
  - Statement extraction via Converse API.

## Core Flows

### Statement upload and extraction
1. User uploads PDF(s) on `/upload-statements`.
2. Service validates contact mapping exists and file is PDF.
3. Service reserves tokens atomically in `TenantBillingTable` + `TenantTokenLedgerTable` and creates the statement header row with `PdfPageCount`, `ReservationLedgerEntryID`, and `TokenReservationStatus=reserved`.
4. PDF uploaded to S3 at `<tenant_id>/statements/<statement_id>.pdf`.
5. Step Functions execution starts with tenant/contact/statement/S3 keys.
6. Lambda processes Bedrock output and writes JSON to `<tenant_id>/statements/<statement_id>.json`.
7. Lambda consumes the earlier reservation on success, or releases it on workflow failure, and statement item rows are written to `TenantStatementsTable` using item IDs like `<statement_id>#item-0001`.

### Statement detail reconciliation
1. `/statement/<statement_id>` loads statement header from DynamoDB.
2. If JSON is missing in S3, page renders processing state and auto-refreshes.
3. When JSON exists, service:
  - loads contact config,
  - loads cached Xero docs for that tenant/contact,
  - matches statement rows to invoices/credit notes,
  - infers payments,
  - applies heuristic item-type classification,
  - persists updated item types back to S3 + DynamoDB.
4. User can mark rows complete/incomplete and export XLSX.

### Tenant sync lifecycle
- Tenant status tracked in `TenantDataTable` (`LOADING`, `SYNCING`, `FREE`).
- First tenant access seeds `LOADING` status and triggers initial sync.
- Manual sync available via `/api/tenants/<tenant_id>/sync`.
- Cached datasets are written locally and to S3; local cache falls back to S3 download when missing.

## Data Contracts

### `TenantStatementsTable` usage
- Partition key: `TenantID`
- Sort key: `StatementID`
- Row types:
  - Statement header row: `RecordType="statement"`, `StatementID=<statement_id>`
  - Statement item row: `RecordType="statement_item"`, `StatementID=<statement_id>#item-XXXX`, plus `ParentStatementID=<statement_id>`
- Important header attributes:
  - `Completed` (`"true"` / `"false"` string)
  - `EarliestItemDate`, `LatestItemDate`
  - `JobId`
  - `PdfPageCount`, `ReservationLedgerEntryID`, `TokenReservationStatus` for billing lifecycle tracking

### `TenantContactsConfigTable`
- Key: `TenantID` + `ContactID`
- Stores `config` payload used by both service and extraction Lambda.
- Required mapping behavior:
  - `number` and `total` are mandatory in UI before save.
  - `date_format` is required at extraction time (`table_to_json` raises if missing).

### S3 object layout
- Statement PDF: `<tenant_id>/statements/<statement_id>.pdf`
- Statement JSON: `<tenant_id>/statements/<statement_id>.json`
- Cached Xero datasets: `<tenant_id>/data/{contacts|invoices|payments|credit_notes}.json`

## Auth and Session Model

- Route protection uses decorators in [`service/utils/auth.py`](service/utils/auth.py).
- `@xero_token_required` enforces:
  - cookie consent,
  - active tenant,
  - non-expired Xero token.
- API auth failures return `401` JSON; UI routes redirect.
- Session storage is encrypted and chunked into multiple cookies (`EncryptedChunkedSessionInterface`) with TTL enforcement on decrypt.

## Non-Obvious Constraints and Invariants

- Anomaly detection flag name is `ml-outlier`, but implementation is keyword/rule-based, not ML inference.
- Reprocessing a statement preserves per-item completion status by reading existing item rows before rewrite.
- Matching logic intentionally allows exact and substring invoice-number matches to handle supplier formatting differences.
- Statement row colors are centrally defined in Python and shared by both UI CSS variables and Excel exports.

## Directory Responsibilities

- `cdk/`: AWS infrastructure definitions for web Lambda, extraction Lambda, Step Functions, DynamoDB, S3, CloudWatch/SNS.
- `service/`: Flask web app, reconciliation logic, Xero sync/cache access, templates/static assets, unit + Playwright tests.
- `lambda_functions/extraction_lambda/`: Bedrock extraction, mapping/normalization, anomaly flagging, persistence.
- `scripts/clear_ddb_and_s3/`: operator reset tool for the configured S3 bucket and tenant data tables; it supports full-environment clears or one-tenant deletes by `TenantID`/S3 prefix so the existing workflow can be narrowed without changing which resources are touched.
- `scripts/manual_token_adjustment/`: operator-only tool for manual token grants/removals; intentionally reuses the service billing transaction logic so snapshot and ledger writes stay atomic.
