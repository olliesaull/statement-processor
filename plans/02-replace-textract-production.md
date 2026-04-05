# Plan 2: Replace Textract with Bedrock Haiku in Production

## Context

Test script (Plan 1) validated Sonnet and Haiku extraction across 18 real supplier PDFs. Haiku 4.5 matched Sonnet's accuracy at 65% less cost and 50% less latency. Array-of-arrays output format cut output tokens by ~45%. Parallel chunk processing validated for multi-chunk PDFs.

This plan replaces Textract with Bedrock Haiku 4.5 across the full production pipeline: Lambda extraction, Step Functions orchestration, service upload flow, and CDK infrastructure. ContactConfig is dropped entirely — the LLM handles header detection, field mapping, date format detection, and separator detection directly.

**Not in production yet.** Only 3 users (developer, colleague, beta tester). Backwards compatibility is not a concern — DynamoDB tables can be wiped if needed.

---

## Architecture: Extraction Interface Contract

All calling code interacts with extraction through a single function with a fixed input/output contract. The implementation (currently Bedrock Haiku) is an internal detail.

```python
def extract_statement(
    pdf_bytes: bytes,
    page_count: int,
) -> ExtractionResult:
    """Extract structured line items from a statement PDF.

    This is the sole entry point for statement extraction. Callers
    depend only on this function signature and ExtractionResult.
    """
```

**Input:** Raw PDF bytes + page count. No contact config, no job ID, no tenant context. The extraction layer doesn't know about the business domain.

**Output:**

```python
class ExtractionResult(BaseModel):
    """Output contract for the extraction layer."""
    items: list[StatementItem]
    detected_headers: list[str]
    date_format: str
    date_confidence: str  # "high" or "low"
    input_tokens: int
    output_tokens: int
```

`StatementItem` stays as-is from `core/models.py` — `date`, `number`, `total` (dict of numeric values), `due_date`, `reference`, `raw` (dict with ALL columns for debugging).

**Inside the boundary:** PDF chunking, Bedrock API calls (with retry), parallel chunk processing, array-of-arrays → dict reconstruction, numeric post-processing (including currency stripping), chunk-boundary deduplication, system prompt + tool schema.

**Outside the boundary:** DynamoDB persistence, S3 upload, anomaly detection, billing/token settlement, date parsing, date range calculation (`_derive_date_range` logic — min/max of parsed dates — is preserved in the orchestrator).

### Why no `decimal_separator` / `thousands_separator` in output

The LLM still detects and returns separators internally — `extract_statement` needs them to run `convert_amount()`. But by the time `ExtractionResult` reaches the caller, `StatementItem.total` already contains floats. Separators are an internal implementation detail of the numeric conversion — callers never need them.

`date_format` and `date_confidence` remain in the output because the orchestrator needs them for the ambiguous date strategy (Xero cross-match, user prompt fallback).

---

## Lambda Internals

### Model

Haiku 4.5 (`eu.anthropic.claude-haiku-4-5-20251001-v1:0`). Test results showed identical accuracy to Sonnet across 18 PDFs including dense 12-page statements (762+ items). 65% cheaper, 50% faster.

### Tool schema

Array-of-arrays format. `column_order` defined once, items as flat arrays. The LLM also returns `detected_headers`, `date_format`, `date_confidence`, `decimal_separator`, `thousands_separator`.

### Processing flow inside `extract_statement(pdf_bytes, page_count)`

1. **Chunk the PDF** — split into ~10-page chunks with 1-page overlap using pypdf. Safety valve splits further if any chunk exceeds 4MB (Bedrock document block limit).

2. **Process chunk 1** — send to Bedrock with system prompt + forced tool use. Extract `column_order`, `detected_headers`, metadata.

3. **Process chunks 2+ in parallel** — `ThreadPoolExecutor`. Each gets the continuation prompt with chunk 1's `detected_headers` AND `column_order` so field mapping is consistent (fixes the Ferreira Fresh regression from testing where ambiguous column names like "Reference" were mis-mapped without the column_order context).

4. **Reconstruct items** — convert array-of-arrays to `StatementItem` dicts using `column_order`. Standard fields (`date`, `number`, `due_date`, `reference`) go to named keys. Non-standard columns with numeric values go to `total`. The `raw` dict contains ALL columns (including those already in standard fields and total) as a complete row snapshot for production debugging — unlike the test script which only stored unmapped leftovers.

5. **Numeric post-processing** — `convert_amount()` parses raw strings to floats using detected separators. Includes currency symbol stripping (regex `^[A-Za-z]{1,3}\s*` for R, $, €, ZAR, USD, etc.) before negative-sign detection. Guards against same-separator ambiguity.

6. **Chunk-boundary deduplication** — consecutive exact-match across all fields. After merging chunks in page order, scan for adjacent items identical across every field (date, number, all totals, reference, raw). Drop the second. This is safe because:
   - Chunk-boundary duplicates are always adjacent (end of chunk N, start of chunk N+1)
   - Avoids false-positive on payments referencing invoices (non-adjacent, different field values)
   - Handles both unique-number items (INV, CRN) and generic rows (EFT, Payment, BBF) uniformly
   - All dedup actions logged for auditability

7. **Return `ExtractionResult`**.

### System prompt

Same as the validated test script prompt with additions:
- `date_confidence` field instructions ("high" if any day > 12 disambiguates, "low" if all dates ≤ 12)
- "Include Balance Brought Forward rows" fix identified during testing
- Separate markdown file (`core/extraction_prompt.md`) for easy iteration

### Retry / timeout

- Retry transient errors: `ThrottlingException`, `InternalServerException`, `ServiceUnavailableException`
- Exponential backoff, max 2 retries
- Fail immediately on client/validation errors
- Boto3 read timeout: **600 seconds** (socket idle timeout — resets on each data chunk received)

---

## Step Functions Simplification

### Current workflow (6 states, polling loop)

```
StartTextractDocumentAnalysis → Wait 10s → GetTextractStatus →
  IsTextractFinished? → (no) loop back → (yes) ProcessStatement Lambda
```

### New workflow (2 states)

```
ProcessStatement Lambda → DidProcessingSucceed?
```

The Lambda calls Bedrock directly (synchronous API). No polling needed.

### Timeouts

- **Lambda timeout:** 660 seconds (11 minutes). Greater than boto3 read timeout (600s) so the Lambda can handle a boto3 timeout gracefully rather than being killed by the runtime.
- **State machine timeout:** 10 minutes (down from 30).
- **TBD:** Max page count from beta tester. Current timeouts cover ~200 pages comfortably. Adjustable via a single CDK constant.

### Why keep Step Functions

The web app needs async execution — can't hold a request open for minutes. Step Functions provide asynchrony + error handling + execution visibility in the AWS console. Each PDF upload triggers its own execution, running independently — small PDFs finish fast, large ones take longer, no blocking.

### Bedrock throttling at scale

Default Haiku 4.5 quotas (confirmed via CLI): ~10,000 RPM, ~5,000,000 TPM (output tokens have 5x burndown rate).

For 3 users × 5 PDFs (15 concurrent): ~23% TPM utilisation. Throttling would require ~60-70 concurrent dense multi-page PDFs (~12-15 users each uploading 5 dense PDFs simultaneously). Non-issue for current and near-term scale. Cross-region inference profile quotas are adjustable if needed. Worst case: requests get throttled and retry with backoff — slower but not broken.

---

## Upload Flow Simplification

### Current flow

1. Upload PDFs
2. Check ContactConfig exists for contact
3. **If no config:** run config suggestion pipeline (Textract sync → Bedrock Haiku → S3 suggestion → `pending_config_review` status). User must review and accept.
4. **If config exists:** reserve tokens → S3 → Step Functions

### New flow

1. Upload PDFs
2. Reserve tokens → S3 → Step Functions

No config check, no suggestion pipeline, no pending review state. Every statement goes straight to processing.

---

## Config Suggestion Pipeline Removal

### Service files to delete

| File | Reason |
|------|--------|
| `core/config_suggestion.py` | Entire Textract sync + LLM suggestion pipeline |
| `core/bedrock_client.py` | Only consumer was config suggestion |
| `core/get_contact_config.py` | DynamoDB config CRUD |
| `core/contact_config_metadata.py` | Field descriptions, example config |
| `templates/configs.html` | Config editor UI (553 lines) |
| `tests/test_bedrock_client.py` | Tests for removed client |
| `tests/test_config_suggestion.py` | Tests for removed pipeline |
| `playwright_tests/helpers/configs.py` | Playwright helpers for config UI |

### Service files to modify

| File | Changes |
|------|---------|
| `app.py` | Remove: `/configs` routes, `/api/configs/confirm`, `/api/configs/confirm-all`, all config helper functions (`_build_config_rows`, `_load_config_context`, `_save_config_context`, `_auto_confirm_pending_suggestions`, `_validate_config_mandatory_fields`, separator normalizers). Simplify: upload flow — remove `ready_uploads`/`review_uploads` split, remove `_create_review_statement_header`, remove `pending_config_review` status. |
| `utils/statement_upload_validation.py` | Remove `_ensure_contact_config()`, remove `needs_config_review` flag from `PreparedStatementUpload` |
| `utils/statement_view.py` | Rework `get_date_format_from_config()`, `get_number_separators_from_config()`, `prepare_display_mappings()` to read from statement JSON instead of ContactConfig |
| `core/models.py` | Remove `ContactConfig` and `ConfigSuggestion` models |
| `config.py` | Remove `bedrock_runtime_client`, `textract_client`, `tenant_contacts_config_table` |

---

## Self-Describing Statement JSON

Each statement JSON in S3 carries its own extraction metadata. The service reads formatting info from the JSON itself — no DynamoDB config lookup needed.

```json
{
  "statement_items": [...],
  "earliest_item_date": "2023-07-17",
  "latest_item_date": "2023-08-08",
  "date_format": "DD.MM.YYYY",
  "date_confidence": "high",
  "detected_headers": ["Doc date", "Invoice No.", "Cross Ref", ...]
}
```

`earliest_item_date` and `latest_item_date` are calculated in Python (not by the LLM) using `_derive_date_range()` — sort all parsed item dates, return first and last. This logic moves from `transform.py` (being removed) to the orchestrator.

---

## Date Ambiguity Handling

The LLM detects date formats by scanning all dates — if any day > 12, it disambiguates DD vs MM. If all dates fall on days 1-12, the format is genuinely ambiguous.

### Layer 1: LLM confidence (MVP)

Tool schema includes `date_confidence`. LLM returns `"high"` if disambiguated, `"low"` if all dates ≤ 12. Travels through `ExtractionResult` to the statement JSON.

### Layer 2: Xero cross-match correction (MVP)

During reconciliation, if an item matches on invoice number but date is DD/MM vs MM/DD swapped vs Xero's date, treat as match and flip to Xero's date. Invoice number match gives high confidence. Enhancement to existing matching logic.

### Layer 3: User prompt on ambiguity (backlog)

If `date_confidence` is `"low"`, show notice on statement detail page: "Date format is ambiguous — DD/MM/YYYY or MM/DD/YYYY?" with a toggle. One selection fixes all dates. The chosen format needs to be persisted (e.g. on the statement header in DynamoDB or in the S3 JSON) so it survives reloads.

---

## CDK Infrastructure Changes

### Remove

| What | Lines | Why |
|------|-------|-----|
| `TenantContactsConfigTable` definition + constant | 44, 84-92 | Dropping ContactConfig |
| `tenant_contacts_config_table.grant_read_write_data(textraction_lambda)` | 206 | Table removed |
| `tenant_contacts_config_table.grant_read_write_data(statement_processor_instance_role)` | 356 | Table removed |
| Lambda env var `TENANT_CONTACTS_CONFIG_TABLE_NAME` | 190 | Table removed |
| AppRunner env var `TENANT_CONTACTS_CONFIG_TABLE_NAME` | 414 | Table removed |
| S3 bucket policy `AllowTextractReadStatements` | 147-158 | Textract no longer reads S3 |
| Lambda `textract:GetDocumentAnalysis` | 198-203 | No more Textract |
| State machine `textract:StartDocumentAnalysis` + `GetDocumentAnalysis` | 309-314 | No more Textract |
| `s3_bucket.grant_read(state_machine.role)` | 315 | State machine no longer reads S3 |
| AppRunner Textract permissions (3 actions) | 331-334 | No more Textract in service |
| AppRunner `bedrock:InvokeModel` for Haiku | 340-354 | Config suggestion removed — service no longer calls Bedrock |
| Entire Textract polling loop states | 216-299 | Replaced by direct Lambda invoke |

### Add

| What | Why |
|------|-----|
| Lambda `bedrock:InvokeModel` for Haiku 4.5 | Lambda now calls Bedrock directly (same ARN pattern as current AppRunner permission) |

### Modify

| What | From | To |
|------|------|-----|
| Lambda timeout | 60s | 660s |
| State machine definition | StartTextract → Poll → Lambda | Lambda → CheckResult |
| State machine timeout | 30 min | 10 min |
| Lambda description | "Perform statement textraction using Textract and PDF Plumber" | Updated |
| Lambda payload | includes `jobId`, `textractStatus` | `tenantId`, `contactId`, `statementId`, `s3Bucket`, `pdfKey`, `jsonKey` |

### DynamoDB table deletion

Remove `TenantContactsConfigTable` from CDK (it will be orphaned due to `RemovalPolicy.RETAIN` in production). Manually delete the orphaned table via AWS console or CLI after CDK deploy.

### Net effect

Bedrock permission moves from AppRunner to Lambda. All Textract permissions removed across the board.

---

## Lambda Code Changes

### Kept as-is

| File | Purpose |
|------|---------|
| `core/models.py` | `StatementItem`, `SupplierStatement`, `TextractionEvent` (input event schema updated) |
| `core/billing.py` | Token settlement (consume/release) |
| `core/date_utils.py` | Date parsing with SDF tokens |
| `core/validation/anomaly_detection.py` | Keyword-based flagging |
| `core/validation/validate_item_count.py` | PDF cross-reference check |
| `exceptions.py` | Custom exceptions |

### Reworked

| File | Changes |
|------|---------|
| `main.py` | Remove `textractStatus` handling, remove `jobId`. Read PDF from S3, call `extract_statement()`, pass result to persistence/validation. |
| `core/textract_statement.py` → `core/statement_processor.py` | Keep orchestration (persist items to DynamoDB, upload JSON to S3, run anomaly detection, run validation, calculate date range). Replace `get_tables_for_job()` with `extract_statement()`. Remove `table_to_json()`. |
| `config.py` | Remove `textract_client`, `tenant_contacts_config_table`. Add `bedrock_runtime_client` with 600s read timeout. |

### New

| File | Purpose |
|------|---------|
| `core/extraction.py` | Complete rewrite. The extraction boundary. Ported from test script. |
| `core/extraction_prompt.md` | System prompt (separate file for easy iteration) |

### Removed

| File | Why |
|------|-----|
| `core/extraction.py` (old) | Textract block-to-grid reconstruction |
| `core/transform.py` | Header detection, grid mapping, `best_header_row`, `_sanitize_grid`, `_dedupe_grid_columns`, `select_relevant_tables_per_page` — all replaced by LLM |
| `core/get_contact_config.py` | ContactConfig DynamoDB lookup |

---

## Testing Strategy

### Unit tests for new extraction module

- `convert_amount()` — currency stripping (R, $, €, ZAR), trailing minus, parenthetical negatives, thousands/decimal separator handling, same-separator guard, empty string
- `reconstruct_items()` — array-of-arrays → dict mapping, standard field routing, non-standard fields to total/raw
- `chunk_pdf()` — correct page ranges, 1-page overlap, size safety valve splitting
- `_derive_date_range()` — min/max date calculation, empty items, missing dates

### Chunk-boundary deduplication tests

- Two identical items at chunk boundary → second dropped
- Two consecutive EFT/Payment rows with same amount but different dates → both kept
- Two consecutive EFT/Payment rows identical across ALL fields → second dropped
- Two consecutive EFT/Payment rows same amount, same date, different raw/description → both kept
- Non-adjacent items with same number (invoice + later payment referencing it) → both kept
- Three consecutive identical items → reduced to one

### Integration tests

- Mock Bedrock responses, verify full `extract_statement()` pipeline produces correct `ExtractionResult`
- Multi-chunk mock: verify header/column_order propagation and chunk merging
- Verify metadata resolution (chunk 1's date_format wins over later chunks)

### Existing tests

- **Keep:** anomaly detection, reference validation, billing settlement, date utils
- **Remove:** `test_bedrock_client.py` (service), `test_config_suggestion.py` (service), Lambda tests for Textract extraction and transform.py grid mapping
- **Update:** Playwright upload flow tests (no more config review gate), remove config page helpers/scenarios

---

## Post-MVP

### End-to-end accuracy suite

Create ~20 synthetic PDFs programmatically (known content → known expected JSON). Variety: clean tables, messy headers, multi-page, multi-sub-statement, bad formatting. Run against real Bedrock Haiku (not mocked). Compare extracted JSON against expected output. Similar to the test script but with deterministic ground truth. Not run in CI — manual trigger for accuracy regression testing over time. Cost: ~$0.50-1.00 per run.

### Chunk-boundary improvements (backlog)

Smarter chunking: detect sub-statement boundaries via lightweight PDF pre-scan for repeated header patterns, chunk at those boundaries instead of fixed page counts. Eliminates root cause of duplication rather than patching with dedup.

### Date format user toggle (backlog)

When `date_confidence` is `"low"`, show toggle on statement detail page. Persisted format needs storage location (statement header in DynamoDB or S3 JSON).
