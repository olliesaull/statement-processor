# Project Overview

This repository implements a supplier statement reconciliation system for Xero.
Users upload supplier statement PDFs, the system extracts line items with Textract, maps them to Xero bills/credit notes/payments, and presents a reconciliation UI plus Excel export.

## Purpose

- Primary user outcome: reconcile supplier statements against Xero records faster and with fewer manual errors.
- Primary users: finance/bookkeeping users operating within a selected Xero tenant.
- Core constraints:
  - Multi-tenant data isolation by `TenantID`.
  - Financial-document correctness is more important than speed for final output.
  - Extraction is asynchronous (Textract + Step Functions), so UI must handle processing states.
  - Step Functions payload size is limited, so full extracted JSON is stored in S3, not passed between states.

## Runtime Architecture

### Web application (`service/`)
- Flask app in [`service/app.py`](service/app.py) serves server-rendered Jinja pages and JSON APIs.
- Deployed as a containerized Lambda (`StatementProcessorWebLambda`) using Lambda Web Adapter (see [`service/Dockerfile`](service/Dockerfile)).

### Extraction pipeline (`lambda_functions/textraction_lambda/`)
- Container Lambda entry point: [`lambda_functions/textraction_lambda/main.py`](lambda_functions/textraction_lambda/main.py).
- Converts Textract table blocks into structured statement JSON, applies anomaly flags, persists item rows to DynamoDB, uploads JSON to S3, and updates statement metadata.
- Main orchestrator: [`lambda_functions/textraction_lambda/core/textract_statement.py`](lambda_functions/textraction_lambda/core/textract_statement.py).

### Workflow orchestration (Step Functions)
- State machine defined in [`cdk/stacks/statement_processor.py`](cdk/stacks/statement_processor.py):
  - `StartTextractDocumentAnalysis`
  - `WaitForTextract` (10s poll interval)
  - `GetTextractStatus`
  - `ProcessStatement` (invoke Textraction Lambda on `SUCCEEDED` or `PARTIAL_SUCCESS`)
  - `TextractFailed` on `FAILED`
- Started by web app upload flow via [`service/utils/workflows.py`](service/utils/workflows.py).

### Data stores
- DynamoDB:
  - `TenantStatementsTable`
  - `TenantContactsConfigTable`
  - `TenantDataTable`
- S3 bucket: `dexero-statement-processor-{stage}` for statement files and cached Xero datasets.

### External integrations
- Xero OAuth + Accounting API:
  - OAuth login/callback in Flask.
  - Background sync caches contacts/invoices/payments/credit notes.
- AWS Textract:
  - Async table extraction (`StartDocumentAnalysis` + `GetDocumentAnalysis`).

## Core Flows

### Statement upload and extraction
1. User uploads PDF(s) on `/upload-statements`.
2. Service validates contact mapping exists and file is PDF.
3. PDF uploaded to S3 at `<tenant_id>/statements/<statement_id>.pdf`.
4. Statement header row is written to `TenantStatementsTable`.
5. Step Functions execution starts with tenant/contact/statement/S3 keys.
6. Lambda processes Textract output and writes JSON to `<tenant_id>/statements/<statement_id>.json`.
7. Statement item rows are written to `TenantStatementsTable` using item IDs like `<statement_id>#item-0001`.

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

- Extraction validation (`validate_references_roundtrip`) is best-effort and does not fail the main pipeline.
- Anomaly detection flag name is `ml-outlier`, but implementation is keyword/rule-based, not ML inference.
- Reprocessing a statement preserves per-item completion status by reading existing item rows before rewrite.
- Matching logic intentionally allows exact and substring invoice-number matches to handle supplier formatting differences.
- Statement row colors are centrally defined in Python and shared by both UI CSS variables and Excel exports.

## Directory Responsibilities

- `cdk/`: AWS infrastructure definitions for web Lambda, textraction Lambda, Step Functions, DynamoDB, S3, CloudWatch/SNS.
- `service/`: Flask web app, reconciliation logic, Xero sync/cache access, templates/static assets, unit + Playwright tests.
- `lambda_functions/textraction_lambda/`: Textract result reconstruction, mapping/normalization, anomaly flagging, persistence.
