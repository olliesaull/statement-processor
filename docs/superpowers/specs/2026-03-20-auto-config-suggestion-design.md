# Auto Config Suggestion — Design Spec

## Problem

Users must manually configure column-to-field mappings for each contact before uploading a statement. This requires opening the PDF separately, reading the column headers, and typing them into the config form. This is the biggest friction point in the product.

## Goal

Reduce first-time config friction by ~90% through automatic config suggestion. The system reads the statement, proposes a mapping, and the user confirms with one click — only intervening when the system can't determine something (e.g. ambiguous date formats).

## Approach

Upload-first flow with LLM-powered auto-config suggestion. On upload, if a contact has no config, the system runs Textract on page 1 (sync API), sends headers + all data rows to Claude Haiku 4.5 on Bedrock, and presents a pre-filled config for user confirmation.

---

## Section 1: New Upload Flow

### Current flow

```
Config exists? → NO  → Block upload with error
Config exists? → YES → Upload PDF → S3 → Start Step Function → Textract → Extract → Done
```

### New flow

```
Config exists? → YES → Upload PDF → S3 → Start Step Function (unchanged)
Config exists? → NO  → Upload PDF → S3 → Background task via ThreadPoolExecutor:
                         1. Textract AnalyzeDocument (page 1 only, sync API, FeatureTypes=["TABLES"])
                         2. Pass headers + all rows to Bedrock Haiku 4.5 (in memory, not persisted)
                         3. Save suggested config to S3: <tenant_id>/config-suggestions/<statement_id>.json
                         4. Set statement status → "pending_config_review"
                       User confirms config later →
                         1. Backend validates mandatory fields (number, date, total, date_format)
                         2. Config saved to TenantContactsConfigTable
                         3. Full Step Function kicked off (existing flow)
```

### Token reservation

First-time config suggestion uploads are **free** (no token reservation). Tokens are reserved later when the user confirms the config and the full Step Function kicks off. This avoids locked tokens for abandoned suggestions.

### Key decisions

- Textract sync API (`AnalyzeDocument`) with `FeatureTypes=["TABLES"]` for single page — no polling needed, ~10s response.
- Raw first-page Textract output is ephemeral (held in memory, passed to LLM, discarded). The full Step Function run produces its own Textract output.
- Suggested config stored in S3 separately from confirmed config. Only persisted to `TenantContactsConfigTable` after user confirmation.
- New statement status value: `pending_config_review`.

---

## Section 2: LLM Config Suggestion (Bedrock Haiku 4.5)

### Input

- Extracted column headers from page 1 (e.g. `["Date", "Reference", "Invoice No", "Debit", "Credit", "Balance"]`)
- All data rows from page 1
- The `ContactConfig` schema definition

### Prompt strategy

- Use Bedrock tool use (structured output) with forced tool call (`tool_choice: {"type": "tool", "name": "suggest_config"}`)
- Prompt instructs the model to:
  1. Map headers to `number`, `date`, `due_date`, `total` fields
  2. Infer `date_format` from sample date values using the **Supplier Date Format (SDF) token set** (see below)
  3. Infer `decimal_separator` and `thousands_separator` from sample numeric values
  4. Return empty string for fields it can't confidently map
- The `raw` field from `ContactConfig` is **intentionally excluded** from auto-suggestion. Raw passthrough headers are auto-discovered during full extraction and do not need user configuration.

### Supplier Date Format (SDF) token reference

The LLM prompt must include this exact token set. The system uses a custom date format language — **not** Python `strftime` and **not** Java `SimpleDateFormat`.

| Token | Meaning | Example match |
|---|---|---|
| `YYYY` | 4-digit year | 2026 |
| `YY` | 2-digit year (2000–2099) | 26 |
| `MMMM` | Full month name | January |
| `MMM` | Abbreviated month (3+ chars) | Jan |
| `MM` | Zero-padded month | 01 |
| `M` | Month (1–2 digits) | 1 |
| `DD` | Zero-padded day | 05 |
| `D` | Day (1–2 digits) | 5 |
| `Do` | Ordinal day | 5th |
| `dddd` | Weekday name (used in optional brackets) | Monday |

**Examples:** `DD/MM/YYYY`, `D MMMM YYYY`, `MM-DD-YY`, `Do MMM YYYY`, `[dddd, ]DD/MM/YYYY`

The prompt must include this table and at least 3 examples so the LLM outputs in the correct format.

### Tool schema

```json
{
  "name": "suggest_config",
  "input_schema": {
    "type": "object",
    "properties": {
      "number": { "type": "string", "description": "Column header for invoice/document number" },
      "date": { "type": "string", "description": "Column header for transaction date" },
      "due_date": { "type": "string", "description": "Column header for due date, empty if not present" },
      "total": { "type": "array", "items": { "type": "string" }, "description": "Column header(s) for amount/total" },
      "date_format": { "type": "string", "description": "Date format pattern using SDF tokens (see token reference)" },
      "decimal_separator": { "type": "string", "enum": [".", ","] },
      "thousands_separator": { "type": "string", "enum": ["", ",", ".", " ", "'"] },
      "confidence_notes": { "type": "string", "description": "Any ambiguities or low-confidence mappings to show to the user" }
    },
    "required": ["number", "date", "total", "date_format", "decimal_separator", "thousands_separator"]
  }
}
```

### Date format disambiguation (hybrid approach)

1. LLM proposes a date format from sample values using SDF tokens.
2. Post-processing scans all date values from page 1: if any value has day > 12, that disambiguates DD/MM vs MM/DD for the whole document.
3. If all dates are ambiguous (day and month both <= 12), `date_format` is set to empty string → confirmation UI asks the user explicitly.

### Cost

~$0.002 per config suggestion at Haiku 4.5 pricing. Negligible at any realistic volume.

---

## Section 3: Confirmation UI (Redesigned `/configs` Page)

### Page structure

The existing `/configs` page is redesigned to handle both LLM-suggested configs (pending review) and manual config editing/viewing. Single page, single mental model.

- **Top section:** Pending review cards (statements needing config confirmation)
- **Below:** Existing configs for viewing/editing

### Notification

Persistent banner/badge visible across pages (dashboard, upload, statements) when `pending_config_review` statements exist: "X statements need config review."

### Pending review card layout

```
[1/3] Supplier ABC — Invoice_Jan2026.pdf
  Number:      [Invoice No    ▼]
  Date:        [Date          ▼]
  Due Date:    [Due Date      ▼]
  Total:       [Amount        ▼] [+ Add column]
  Date Format: [DD/MM/YYYY    ] ← highlighted if empty
  Decimal:     [.  ▼]  Thousands: [,  ▼]
  ℹ️ "Chose 'Invoice No' over 'Reference' — values match invoice number patterns"
  [Confirm ✓]
```

### Field inputs

- Mapped fields (`number`, `date`, `due_date`, `total`) are **dropdowns populated with actual detected headers** from that statement's Textract output — not free text.
- Each header can only be selected once across fields, **except** `total` which allows multiple selections (for debit/credit patterns). Selected headers are greyed out in other dropdowns.
- `date_format`, `decimal_separator`, `thousands_separator` use the same input types as the existing config form.

### Confidence notes display

The `confidence_notes` field from the LLM response is displayed as a subtle info note below the fields in each card. If the LLM returns an empty string for `confidence_notes`, the info note area is hidden entirely.

### Validation

- **Client-side:** Confirm button disabled until `number`, `date`, `total` (at least one), and `date_format` are all non-empty.
- **Server-side (source of truth):** Backend validates the same mandatory field rules before saving. Rejects incomplete configs regardless of client state.

### On confirm (per statement or "Confirm All")

1. POST to backend with config payload.
2. Backend validates mandatory fields (same logic as existing `/configs` save).
3. Config saved to `TenantContactsConfigTable`.
4. Full Step Function kicked off for that statement.
5. Statement status updated from `pending_config_review` → processing.
6. Card shows "Submitted" briefly, then removed from the pending list.
7. User can track processing status on `/statements`.

**"Confirm All" behaviour:** Only submits cards where all mandatory fields are populated. Cards with missing fields are skipped and remain in the pending list. A message indicates how many were skipped and why (e.g. "2 of 3 confirmed. 1 skipped — missing date format.").

### Escape hatch

"Edit in full config page" link per card for edge cases where user wants the full manual config experience.

---

## Section 4: Backend Architecture

### New modules

**`service/core/config_suggestion.py`** — orchestrates the config suggestion flow:
- `suggest_config_for_statement(tenant_id, contact_id, statement_id, pdf_s3_key)` — main entry point submitted to the thread pool.
- Calls Textract sync API (`AnalyzeDocument`, `FeatureTypes=["TABLES"]`) for page 1.
- Parses headers + all rows from page 1.
- Calls Bedrock Haiku 4.5.
- Saves suggestion to S3.
- Updates statement status to `pending_config_review`.

**`service/core/bedrock_client.py`** — thin wrapper around Bedrock runtime:
- `suggest_column_mapping(headers, rows)` — sends headers + data, returns structured `ContactConfig` suggestion via tool use.
- Uses `botocore.config.Config(retries={"max_attempts": 3, "mode": "adaptive"})` for throttling resilience.

**`service/core/date_disambiguation.py`** — post-processing for date format:
- `disambiguate_date_format(date_values, llm_suggested_format)` — scans all date values, looks for day > 12.
- Returns confirmed format or empty string if truly ambiguous.

### Modified modules

**`service/utils/statement_upload_validation.py`** — `prepare_statement_uploads()` no longer blocks on missing config. Returns `needs_config_review: bool` flag per statement.

**`service/app.py`** — upload handler changes:
- Statements with config → start Step Function (as today).
- Statements without config → submit to thread pool calling `suggest_config_for_statement()`.
- New POST endpoint on `/configs` for confirming suggested configs.

**`service/templates/configs.html`** — redesigned page per Section 3.

### Threading & concurrency

- Uses the **existing `ThreadPoolExecutor`** in `app.py` (currently `max_workers=2` for Xero sync). Increase `max_workers` to accommodate config suggestion tasks alongside sync work. `ThreadPoolExecutor` handles concurrency capping and task queuing automatically — tasks exceeding the worker count queue until a worker is free.
- AppRunner at 0.5 vCPU / 1 GB RAM handles this comfortably — tasks are I/O bound (waiting on API calls), not CPU bound.

### Retry & error handling

- Textract and Bedrock clients configured with boto3 adaptive retry: `botocore.config.Config(retries={"max_attempts": 3, "mode": "adaptive"})`. Handles throttling with token-bucket rate limiting + exponential backoff automatically.
- If all retries fail → statement status set to `config_suggestion_failed` → user falls back to manual config.

### S3 layout

- `<tenant_id>/config-suggestions/<statement_id>.json` — temporary suggestion file, deleted after user confirms or deletes the statement.

### DynamoDB

- No schema changes. `TenantStatementsTable` statement header row gets new status values (`pending_config_review`, `config_suggestion_failed`) — status is already a string attribute.

### Dependencies

- No new production dependencies. `boto3` Bedrock runtime client is available — boto3 is already in use for Textract, S3, DynamoDB.

---

## Section 5: Error Handling & Edge Cases

### Statement status lifecycle

```
Upload (no config) → pending_config_review → [user confirms] → processing → completed
                   → config_suggestion_failed → [user manually configures] → processing → completed
                   → config_suggestion_failed → [user deletes statement] → cleaned up

Upload (has config) → processing → completed  (unchanged)
```

### Edge cases

1. **Multiple statements for the same new contact:** First statement triggers LLM suggestion. Subsequent statements for the same contact reuse the suggestion (or confirmed config if already confirmed). Background task checks if a config or suggestion already exists before calling Textract + Bedrock. **Note:** There is a TOCTOU race where two tasks could both check and both proceed. This results in redundant API calls but not incorrect state (the second suggestion overwrites the first in S3). Acceptable for v1; see Future Enhancements for per-contact locking.

2. **User navigates away during processing:** Background tasks complete independently. Statements show up as `pending_config_review` or `config_suggestion_failed` whenever the user returns. Notification banner makes them visible.

3. **LLM returns a bad mapping:** User catches this in the confirmation step. Dropdowns make it easy to reassign. Real validation happens during extraction.

4. **PDF has no tables on page 1:** Textract returns no table data → `config_suggestion_failed` → manual config fallback. Full extraction Step Function may still find tables on later pages.

5. **Textract sync API fails:** boto3 adaptive retry handles transient failures and throttling (up to 3 attempts with exponential backoff). If all retries fail → `config_suggestion_failed` → manual config with free text inputs (no detected headers available for dropdowns).

6. **Cross-user Textract throttling:** The thread pool caps per-instance concurrency. boto3 adaptive retry handles account-level throttling from multiple users/instances. After 3 failed attempts → manual config fallback. User is never stuck.

7. **Contact deleted from Xero between upload and config confirmation:** Config save still succeeds (stored by `ContactID`). Reconciliation surfaces the missing contact issue as it does today.

### Config suggestion S3 cleanup

Suggestion files at `<tenant_id>/config-suggestions/<statement_id>.json` are deleted when:
- User confirms → config saved to DDB, suggestion file deleted.
- User deletes statement → suggestion file + PDF cleaned up.

### Logging

Per CLAUDE.md structured logging requirements (aws_lambda_powertools style):
- All Textract/Bedrock calls logged with `tenant_id`, `contact_id`, `statement_id`.
- LLM suggestion logged (input headers + output mapping) for debugging accuracy.
- Failures logged with error details and which fallback path was taken.
- No sensitive financial data in logs — only headers and metadata, not row values.

---

## Section 6: Infrastructure Changes (CDK)

### New IAM permissions for AppRunner instance role

- `bedrock:InvokeModel` — scoped to Haiku 4.5 model ARN in the relevant region.
- Verify `textract:AnalyzeDocument` (sync API) is permitted — existing role may only have async API actions (`StartDocumentAnalysis`, `GetDocumentAnalysis`).

### No new AWS resources

- No new DynamoDB tables.
- No new S3 buckets (config suggestions use existing bucket under `<tenant_id>/config-suggestions/` prefix).
- No new Step Functions, Lambdas, SQS, or SNS.

### Bedrock model access

Bedrock model access must be enabled for Claude Haiku 4.5 in the AWS console (one-time manual step per account/region). Not CDK-managed.

### AppRunner configuration

No changes. Current 0.5 vCPU / 1 GB RAM is sufficient.

---

## Section 7: Future Enhancements (Out of Scope for v1)

1. **S3 lifecycle rule for abandoned config suggestions** — TODO: auto-delete suggestion files older than 30 days via S3 lifecycle policy.
2. **Config suggestion accuracy tracking** — log how often users modify the LLM suggestion vs. accepting as-is, to measure and improve accuracy over time.
3. **Caching raw Textract output** — if full Step Function re-running Textract on page 1 proves wasteful, cache and reuse. Not worth the complexity for v1.
4. **Model upgrade path** — if Haiku 4.5 accuracy proves insufficient for certain edge cases, swap to a larger model for specific contacts/suppliers flagged as problematic.
5. **Template configs** — for common suppliers (e.g. large suppliers that many users share), offer pre-built config templates. Requires enough user data to identify common patterns.
6. **Per-contact locking for suggestion deduplication** — TODO: prevent redundant Textract + Bedrock calls when multiple statements for the same new contact are uploaded simultaneously. Currently results in redundant but not incorrect work.
7. **Token reservation for config suggestion uploads** — TODO: currently first-time config suggestion uploads are free. If this is exploited or volume grows, consider reserving tokens at upload and releasing on abandonment.
