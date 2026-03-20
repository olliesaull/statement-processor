# Auto Config Suggestion Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers-extended-cc:subagent-driven-development (if subagents available) or superpowers-extended-cc:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect statement column mappings via Textract + Bedrock Haiku 4.5 so users confirm a pre-filled config instead of building one from scratch.

**Architecture:** On upload without config, a background thread runs Textract sync API on page 1, sends headers + rows to Bedrock for structured mapping suggestion, saves to S3, and sets status to `pending_config_review`. A redesigned `/configs` page shows pending reviews with header dropdowns for confirmation.

**Tech Stack:** Python 3.13, Flask, boto3 (Textract sync API, Bedrock Runtime, S3, DynamoDB), Jinja2, Bootstrap 5, CDK (IAM permissions)

**Spec:** `docs/superpowers/specs/2026-03-20-auto-config-suggestion-design.md`

**Test command:** `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests -v`

**Lint/format command:** `cd /home/ollie/statement-processor/service && make dev`

---

## File Structure

### New files

| File | Responsibility |
|---|---|
| `service/core/date_disambiguation.py` | Scan date values for day > 12 to disambiguate DD/MM vs MM/DD |
| `service/core/bedrock_client.py` | Thin Bedrock wrapper — build prompt, invoke Haiku 4.5, parse tool response |
| `service/core/config_suggestion.py` | Orchestrator — Textract page 1 → Bedrock → save suggestion to S3 → update status |
| `service/tests/test_date_disambiguation.py` | Tests for date disambiguation logic |
| `service/tests/test_bedrock_client.py` | Tests for Bedrock prompt construction and response parsing |
| `service/tests/test_config_suggestion.py` | Tests for orchestrator flow |

### Modified files

| File | Changes |
|---|---|
| `service/config.py` | Add `textract_client` and `bedrock_runtime_client` |
| `service/core/models.py` | Add `ConfigSuggestion` model for S3 suggestion payload |
| `service/utils/statement_upload_validation.py` | `_ensure_contact_config()` returns status enum instead of blocking; `prepare_statement_uploads()` returns `needs_config_review` flag |
| `service/app.py` | Increase `ThreadPoolExecutor` workers; upload handler branches on config existence; new `/api/configs/confirm` endpoint; updated `/configs` route to load pending reviews |
| `service/templates/configs.html` | Redesigned — pending review cards at top, header dropdowns, confirm/confirm-all |
| `service/templates/base.html` | Add notification banner for pending config reviews |
| `service/tests/test_statement_upload_validation.py` | Update tests for new non-blocking behaviour |
| `cdk/stacks/statement_processor.py` | Add `textract:AnalyzeDocument` + `bedrock:InvokeModel` to AppRunner role |

---

### Task 1: CDK — Add IAM Permissions

**Files:**
- Modify: `cdk/stacks/statement_processor.py:332-340`

- [ ] **Step 1: Add `textract:AnalyzeDocument` and `bedrock:InvokeModel` to AppRunner role**

In the existing inline policy (lines 332-340), add the two new actions. The Textract sync API uses `AnalyzeDocument` (distinct from the existing `StartDocumentAnalysis`/`GetDocumentAnalysis`). Bedrock is scoped to the Haiku 4.5 model ARN.

```python
# Inside the existing PolicyStatement actions list, add:
"textract:AnalyzeDocument",

# Add a new PolicyStatement for Bedrock:
iam.PolicyStatement(
    actions=["bedrock:InvokeModel"],
    resources=[
        f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-haiku-4-5-*"
    ],
)
```

- [ ] **Step 2: Verify CDK synths successfully**

Run: `cd /home/ollie/statement-processor/cdk && npx cdk synth --quiet 2>&1 | tail -5`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add cdk/stacks/statement_processor.py
git commit -m "feat(cdk): add Textract AnalyzeDocument and Bedrock InvokeModel permissions"
```

---

### Task 2: Add Textract and Bedrock Clients to Config

**Files:**
- Modify: `service/config.py:85-87`

- [ ] **Step 1: Add boto3 clients for Textract and Bedrock Runtime**

After the existing client declarations (line 85-87), add:

```python
import botocore.config

_adaptive_retry = botocore.config.Config(retries={"max_attempts": 3, "mode": "adaptive"})
textract_client = boto3.client("textract", config=_adaptive_retry)
bedrock_runtime_client = boto3.client("bedrock-runtime", config=_adaptive_retry)
```

- [ ] **Step 2: Commit**

```bash
git add service/config.py
git commit -m "feat: add textract and bedrock-runtime boto3 clients to config"
```

---

### Task 3: Date Disambiguation Module

**Files:**
- Create: `service/core/date_disambiguation.py`
- Create: `service/tests/test_date_disambiguation.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for date format disambiguation logic."""

from core.date_disambiguation import disambiguate_date_format


def test_unambiguous_dd_mm_when_day_exceeds_12() -> None:
    """A value like '15/03/2025' proves DD/MM ordering."""
    result = disambiguate_date_format(
        ["03/05/2025", "15/03/2025", "07/08/2025"],
        "DD/MM/YYYY",
    )
    assert result == "DD/MM/YYYY"


def test_unambiguous_mm_dd_when_day_exceeds_12_in_second_position() -> None:
    """A value like '03/15/2025' proves MM/DD ordering."""
    result = disambiguate_date_format(
        ["05/03/2025", "03/15/2025"],
        "MM/DD/YYYY",
    )
    assert result == "MM/DD/YYYY"


def test_fully_ambiguous_returns_empty() -> None:
    """When all dates have day and month <= 12, result is empty."""
    result = disambiguate_date_format(
        ["03/05/2025", "07/08/2025", "01/12/2025"],
        "DD/MM/YYYY",
    )
    assert result == ""


def test_empty_date_list_returns_empty() -> None:
    """No dates to analyze means ambiguous."""
    result = disambiguate_date_format([], "DD/MM/YYYY")
    assert result == ""


def test_non_numeric_dates_passthrough() -> None:
    """Dates with month names (e.g. 'D MMMM YYYY') are never ambiguous."""
    result = disambiguate_date_format(
        ["5 January 2025", "3 March 2025"],
        "D MMMM YYYY",
    )
    assert result == "D MMMM YYYY"


def test_corrects_llm_format_when_data_contradicts() -> None:
    """If LLM says MM/DD but data proves DD/MM, the format should be corrected."""
    result = disambiguate_date_format(
        ["15/03/2025", "20/06/2025"],
        "MM/DD/YYYY",  # LLM got it wrong
    )
    assert result == "DD/MM/YYYY"


def test_preserves_llm_format_when_unambiguous() -> None:
    """The LLM-suggested format string is returned as-is when confirmed."""
    result = disambiguate_date_format(
        ["25/03/2025"],
        "DD/MM/YYYY",
    )
    assert result == "DD/MM/YYYY"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_date_disambiguation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.date_disambiguation'`

- [ ] **Step 3: Write implementation**

```python
"""Post-processing to disambiguate DD/MM vs MM/DD date formats.

The LLM proposes a date format from sample values. This module scans all
date values to confirm or reject that proposal. If any value has a
component > 12 in the position that would be the day, it disambiguates
the entire document. If all values are ambiguous, returns empty string
so the user is prompted.
"""

import re

from logger import logger

# Matches date strings where the first two numeric components could be day/month.
# E.g. "15/03/2025", "03-15-2025", "07.08.25"
_NUMERIC_DATE_RE = re.compile(
    r"^(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\s*[/\-\.]\s*(\d{2,4})$"
)


def disambiguate_date_format(
    date_values: list[str],
    llm_suggested_format: str,
) -> str:
    """Confirm or reject an LLM-suggested date format by scanning actual values.

    Scans ``date_values`` for numeric date strings. If any value has a
    component > 12 in first or second position, that disambiguates
    DD/MM vs MM/DD for the whole document.

    Args:
        date_values: Raw date strings extracted from the statement.
        llm_suggested_format: The SDF format string proposed by the LLM.

    Returns:
        The confirmed format string, or empty string if ambiguous.
    """
    if not date_values:
        return ""

    # If the format uses month names (MMM/MMMM), there's no DD/MM ambiguity.
    if "MMM" in llm_suggested_format:
        return llm_suggested_format

    first_positions: list[int] = []
    second_positions: list[int] = []

    for raw in date_values:
        match = _NUMERIC_DATE_RE.match(raw.strip())
        if not match:
            continue
        first_positions.append(int(match.group(1)))
        second_positions.append(int(match.group(2)))

    if not first_positions:
        # No parseable numeric dates found.
        return ""

    first_has_gt12 = any(v > 12 for v in first_positions)
    second_has_gt12 = any(v > 12 for v in second_positions)

    if first_has_gt12 and not second_has_gt12:
        # First position must be day (DD/MM). Correct the LLM format if it
        # suggested MM/DD by swapping the day/month tokens.
        corrected = _ensure_dd_mm(llm_suggested_format)
        logger.info(
            "Date format disambiguated as DD/MM",
            sample_count=len(first_positions),
            max_first=max(first_positions),
            corrected_format=corrected,
        )
        return corrected

    if second_has_gt12 and not first_has_gt12:
        # Second position must be day (MM/DD). Correct the LLM format if it
        # suggested DD/MM by swapping the day/month tokens.
        corrected = _ensure_mm_dd(llm_suggested_format)
        logger.info(
            "Date format disambiguated as MM/DD",
            sample_count=len(second_positions),
            max_second=max(second_positions),
            corrected_format=corrected,
        )
        return corrected

    # Both <= 12 everywhere: genuinely ambiguous.
    logger.info(
        "Date format is ambiguous — all values have day and month <= 12",
        sample_count=len(first_positions),
    )
    return ""


def _ensure_dd_mm(fmt: str) -> str:
    """If format has MM before DD, swap them so day comes first."""
    # Handle token pairs: DD/MM, D/M, DD/M, D/MM etc.
    # Simple approach: if format starts with M-type token before D-type, swap.
    import re as _re
    # Match leading M-token followed by separator then D-token
    pattern = _re.compile(r"^(M{1,2})([\s/\-\.]+)(D{1,2}|Do)")
    match = pattern.match(fmt)
    if match:
        return fmt[:match.start()] + match.group(3) + match.group(2) + match.group(1) + fmt[match.end():]
    return fmt


def _ensure_mm_dd(fmt: str) -> str:
    """If format has DD before MM, swap them so month comes first."""
    import re as _re
    pattern = _re.compile(r"^(D{1,2}|Do)([\s/\-\.]+)(M{1,2})")
    match = pattern.match(fmt)
    if match:
        return fmt[:match.start()] + match.group(3) + match.group(2) + match.group(1) + fmt[match.end():]
    return fmt
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_date_disambiguation.py -v`
Expected: All PASS

- [ ] **Step 5: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Format, lint, type-check, tests all pass

- [ ] **Step 6: Commit**

```bash
git add service/core/date_disambiguation.py service/tests/test_date_disambiguation.py
git commit -m "feat: add date format disambiguation module"
```

---

### Task 4: Bedrock Client Module

**Files:**
- Create: `service/core/bedrock_client.py`
- Create: `service/tests/test_bedrock_client.py`

- [ ] **Step 1: Write failing tests**

Tests should verify: prompt construction includes SDF tokens, tool schema is correct, response parsing extracts `ContactConfig` fields, and `confidence_notes` is captured. Mock the boto3 Bedrock client — do NOT call real Bedrock.

```python
"""Tests for Bedrock config suggestion client."""

import json

import core.bedrock_client as bedrock_client_module
from core.bedrock_client import build_suggestion_prompt, parse_suggestion_response, suggest_column_mapping


def test_build_suggestion_prompt_includes_sdf_tokens() -> None:
    """Prompt must include the SDF token table so the LLM outputs correct format."""
    headers = ["Date", "Invoice No", "Amount"]
    rows = [["15/03/2025", "INV-001", "1,234.56"]]
    prompt = build_suggestion_prompt(headers, rows)
    assert "YYYY" in prompt
    assert "DD/MM/YYYY" in prompt
    assert "Invoice No" in prompt
    assert "1,234.56" in prompt


def test_parse_suggestion_response_extracts_config() -> None:
    """Tool use response should be parsed into config dict + confidence notes."""
    tool_input = {
        "number": "Invoice No",
        "date": "Date",
        "due_date": "",
        "total": ["Amount"],
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "confidence_notes": "High confidence mapping",
    }
    mock_response = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "suggest_config",
                            "toolUseId": "test-id",
                            "input": tool_input,
                        }
                    }
                ]
            }
        },
        "stopReason": "tool_use",
    }
    config, notes = parse_suggestion_response(mock_response)
    assert config["number"] == "Invoice No"
    assert config["date"] == "Date"
    assert config["total"] == ["Amount"]
    assert config["date_format"] == "DD/MM/YYYY"
    assert notes == "High confidence mapping"


def test_parse_suggestion_response_raises_on_missing_tool_use() -> None:
    """Should raise ValueError when response has no tool use block."""
    mock_response = {
        "output": {"message": {"content": [{"text": "No tool use here"}]}},
        "stopReason": "end_turn",
    }
    try:
        parse_suggestion_response(mock_response)
        assert False, "Expected ValueError"
    except ValueError:
        pass


def test_suggest_column_mapping_calls_bedrock_and_returns_config(monkeypatch) -> None:
    """Integration: verify the full suggest flow calls Bedrock and returns parsed result."""
    tool_input = {
        "number": "Ref",
        "date": "Date",
        "due_date": "",
        "total": ["Debit", "Credit"],
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "confidence_notes": "",
    }
    fake_response = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "suggest_config",
                            "toolUseId": "test-id",
                            "input": tool_input,
                        }
                    }
                ]
            }
        },
        "stopReason": "tool_use",
    }

    class FakeBedrock:
        def converse(self, **kwargs):
            return fake_response

    monkeypatch.setattr(bedrock_client_module, "bedrock_runtime_client", FakeBedrock())

    config, notes = suggest_column_mapping(
        headers=["Date", "Ref", "Debit", "Credit"],
        rows=[["15/03/2025", "INV-001", "100.00", ""], ["20/03/2025", "INV-002", "", "50.00"]],
    )
    assert config["number"] == "Ref"
    assert config["total"] == ["Debit", "Credit"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_bedrock_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

Create `service/core/bedrock_client.py`. Key elements:
- `HAIKU_MODEL_ID = "anthropic.claude-haiku-4-5-20251001"` — model ID for Bedrock Converse API
- `SUGGEST_CONFIG_TOOL` — tool definition dict matching the spec's JSON schema. **Important:** The Bedrock Converse API uses camelCase keys — use `inputSchema` (not `input_schema` from the spec)
- `build_suggestion_prompt(headers, rows)` — builds the user message with headers, sample rows, and the SDF token reference table
- `suggest_column_mapping(headers, rows)` — calls `bedrock_runtime_client.converse()` with the tool, forced `tool_choice`, parses response
- `parse_suggestion_response(response)` — extracts tool use input from Converse API response, returns `(config_dict, confidence_notes)`

The prompt must include:
- The full SDF token table (YYYY, YY, MMMM, MMM, MM, M, DD, D, Do, dddd)
- At least 3 SDF examples (DD/MM/YYYY, D MMMM YYYY, MM-DD-YY)
- Clear instruction: "Do NOT use Python strftime or Java SimpleDateFormat. Use ONLY the SDF tokens listed above."
- The headers and all rows formatted as a table
- Instruction to return empty string for fields that can't be confidently mapped

Use Bedrock **Converse API** (not `invoke_model`) as it natively supports tool use:
```python
response = bedrock_runtime_client.converse(
    modelId=HAIKU_MODEL_ID,
    messages=[{"role": "user", "content": [{"text": prompt}]}],
    toolConfig={
        "tools": [{"toolSpec": SUGGEST_CONFIG_TOOL}],
        "toolChoice": {"tool": {"name": "suggest_config"}},
    },
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_bedrock_client.py -v`
Expected: All PASS

- [ ] **Step 5: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 6: Commit**

```bash
git add service/core/bedrock_client.py service/tests/test_bedrock_client.py
git commit -m "feat: add Bedrock client for config suggestion via tool use"
```

---

### Task 5: ConfigSuggestion Model

**Files:**
- Modify: `service/core/models.py:66-88`

- [ ] **Step 1: Add ConfigSuggestion model after ContactConfig**

This model represents what gets saved to S3 at `<tenant_id>/config-suggestions/<statement_id>.json`. It wraps the LLM output plus metadata needed by the confirmation UI.

```python
class ConfigSuggestion(BaseModel):
    """LLM-suggested config stored in S3 for user confirmation."""

    contact_id: str
    contact_name: str
    statement_id: str
    filename: str
    page_count: int
    suggested_config: dict[str, Any]
    detected_headers: list[str]
    confidence_notes: str = ""
```

- [ ] **Step 2: Commit**

```bash
git add service/core/models.py
git commit -m "feat: add ConfigSuggestion model for S3 suggestion payload"
```

---

### Task 6: Config Suggestion Orchestrator

**Files:**
- Create: `service/core/config_suggestion.py`
- Create: `service/tests/test_config_suggestion.py`

- [ ] **Step 1: Write failing tests**

Test the orchestrator function `suggest_config_for_statement()`. Mock Textract, Bedrock client, S3, and DynamoDB. Verify:
- Textract is called with `FeatureTypes=["TABLES"]` and page 1 only
- Headers + rows are extracted from Textract table response
- Bedrock client is called with extracted headers/rows
- Date disambiguation is applied
- Suggestion is saved to S3 at correct key
- Statement status updated to `pending_config_review` in DynamoDB
- On Textract failure: status set to `config_suggestion_failed`

```python
"""Tests for config suggestion orchestrator."""

import json

import core.config_suggestion as config_suggestion_module
from core.config_suggestion import suggest_config_for_statement


def _make_textract_response(headers: list[str], rows: list[list[str]]) -> dict:
    """Build a minimal Textract AnalyzeDocument response with one table."""
    # Build cells: row 1 = headers, subsequent rows = data
    cells = []
    for col_idx, header in enumerate(headers, 1):
        cells.append({
            "BlockType": "CELL",
            "RowIndex": 1,
            "ColumnIndex": col_idx,
            "Text": header,
        })
    for row_idx, row in enumerate(rows, 2):
        for col_idx, value in enumerate(row, 1):
            cells.append({
                "BlockType": "CELL",
                "RowIndex": row_idx,
                "ColumnIndex": col_idx,
                "Text": value,
            })
    return {"Blocks": [{"BlockType": "TABLE"}] + cells}


def test_suggest_config_happy_path(monkeypatch) -> None:
    """Full flow: Textract → Bedrock → S3 → DynamoDB status update."""
    headers = ["Date", "Invoice No", "Amount"]
    rows = [["15/03/2025", "INV-001", "1,234.56"]]
    textract_response = _make_textract_response(headers, rows)

    # Mock Textract
    class FakeTextract:
        def analyze_document(self, **kwargs):
            assert kwargs["FeatureTypes"] == ["TABLES"]
            return textract_response

    # Mock Bedrock
    suggested = {
        "number": "Invoice No",
        "date": "Date",
        "due_date": "",
        "total": ["Amount"],
        "date_format": "DD/MM/YYYY",
        "decimal_separator": ".",
        "thousands_separator": ",",
    }
    monkeypatch.setattr(
        config_suggestion_module,
        "suggest_column_mapping",
        lambda headers, rows: (suggested, "High confidence"),
    )

    # Mock date disambiguation
    monkeypatch.setattr(
        config_suggestion_module,
        "disambiguate_date_format",
        lambda dates, fmt: fmt,
    )

    # Mock S3
    s3_puts = []

    class FakeS3:
        def get_object(self, **kwargs):
            raise Exception("NoSuchKey")

        def put_object(self, **kwargs):
            s3_puts.append(kwargs)

    # Mock DynamoDB
    ddb_updates = []

    class FakeTable:
        def update_item(self, **kwargs):
            ddb_updates.append(kwargs)

    monkeypatch.setattr(config_suggestion_module, "textract_client", FakeTextract())
    monkeypatch.setattr(config_suggestion_module, "s3_client", FakeS3())
    monkeypatch.setattr(config_suggestion_module, "S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setattr(config_suggestion_module, "tenant_statements_table", FakeTable())

    suggest_config_for_statement(
        tenant_id="t1",
        contact_id="c1",
        contact_name="Acme Ltd",
        statement_id="s1",
        pdf_s3_key="t1/statements/s1.pdf",
        filename="invoice.pdf",
    )

    # Verify S3 suggestion saved
    assert len(s3_puts) == 1
    assert "config-suggestions" in s3_puts[0]["Key"]
    body = json.loads(s3_puts[0]["Body"])
    assert body["suggested_config"]["number"] == "Invoice No"
    assert body["detected_headers"] == headers
    assert body["confidence_notes"] == "High confidence"

    # Verify DynamoDB status update
    assert len(ddb_updates) == 1
    assert "pending_config_review" in str(ddb_updates[0])


def test_suggest_config_textract_failure_sets_failed_status(monkeypatch) -> None:
    """When Textract fails after retries, status should be config_suggestion_failed."""

    class FakeTextract:
        def analyze_document(self, **kwargs):
            raise Exception("Textract error")

    ddb_updates = []

    class FakeTable:
        def update_item(self, **kwargs):
            ddb_updates.append(kwargs)

    monkeypatch.setattr(config_suggestion_module, "textract_client", FakeTextract())
    monkeypatch.setattr(config_suggestion_module, "tenant_statements_table", FakeTable())

    suggest_config_for_statement(
        tenant_id="t1",
        contact_id="c1",
        contact_name="Acme Ltd",
        statement_id="s1",
        pdf_s3_key="t1/statements/s1.pdf",
        filename="invoice.pdf",
    )

    assert len(ddb_updates) == 1
    assert "config_suggestion_failed" in str(ddb_updates[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_config_suggestion.py -v`
Expected: FAIL

- [ ] **Step 3: Write implementation**

Create `service/core/config_suggestion.py`. Key elements:

```python
"""Orchestrator for auto-suggesting contact config from statement PDFs.

Runs Textract sync API on page 1, sends extracted headers and rows to
Bedrock Haiku 4.5, applies date disambiguation, saves the suggestion
to S3, and updates statement status in DynamoDB.
"""

import json

from config import S3_BUCKET_NAME, s3_client, textract_client, tenant_statements_table
from core.bedrock_client import suggest_column_mapping
from core.date_disambiguation import disambiguate_date_format
from core.models import ConfigSuggestion
from logger import logger


def suggest_config_for_statement(
    tenant_id: str,
    contact_id: str,
    contact_name: str,
    statement_id: str,
    pdf_s3_key: str,
    filename: str,
    page_count: int = 0,
) -> None:
    """Run the full config suggestion pipeline for a single statement.

    This is the entry point called from the ThreadPoolExecutor in the
    upload handler. It must not raise — failures are captured as status
    updates in DynamoDB.
    """
    try:
        # 1. Textract sync on page 1
        headers, rows, date_values = _extract_page_one(pdf_s3_key)

        if not headers:
            logger.warning(
                "No table headers found on page 1",
                tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id,
            )
            _set_statement_status(tenant_id, statement_id, "config_suggestion_failed")
            return

        # 2. LLM suggestion
        suggested_config, confidence_notes = suggest_column_mapping(headers, rows)

        # 3. Date disambiguation
        date_format = suggested_config.get("date_format", "")
        if date_format:
            confirmed = disambiguate_date_format(date_values, date_format)
            suggested_config["date_format"] = confirmed

        # 4. Save suggestion to S3
        suggestion = ConfigSuggestion(
            contact_id=contact_id,
            contact_name=contact_name,
            statement_id=statement_id,
            filename=filename,
            page_count=page_count,
            suggested_config=suggested_config,
            detected_headers=headers,
            confidence_notes=confidence_notes,
        )
        suggestion_key = f"{tenant_id}/config-suggestions/{statement_id}.json"
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=suggestion_key,
            Body=suggestion.model_dump_json(),
            ContentType="application/json",
        )

        # 5. Update statement status
        _set_statement_status(tenant_id, statement_id, "pending_config_review")

        logger.info(
            "Config suggestion saved",
            tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id,
            detected_headers=headers,
            suggested_number=suggested_config.get("number"),
            suggested_date_format=suggested_config.get("date_format"),
        )

    except Exception:
        logger.exception(
            "Config suggestion failed",
            tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id,
        )
        _set_statement_status(tenant_id, statement_id, "config_suggestion_failed")
```

Also implement:
- `_extract_page_one(pdf_s3_key)` — calls `textract_client.analyze_document()` with S3 object reference and `FeatureTypes=["TABLES"]`, parses the Blocks response to extract headers (row 1), data rows (row 2+), and date column values. Returns `(headers, rows, date_values)`.
- `_set_statement_status(tenant_id, statement_id, status)` — updates `TenantStatementsTable` header row.
- `get_pending_suggestions(tenant_id)` — lists config suggestion files from S3 prefix `<tenant_id>/config-suggestions/`, loads and returns as list of `ConfigSuggestion`.
- `delete_suggestion(tenant_id, statement_id)` — deletes suggestion file from S3.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_config_suggestion.py -v`
Expected: All PASS

- [ ] **Step 5: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 6: Commit**

```bash
git add service/core/config_suggestion.py service/tests/test_config_suggestion.py
git commit -m "feat: add config suggestion orchestrator (Textract + Bedrock + S3)"
```

---

### Task 7: Modify Upload Validation to Allow Missing Config

**Files:**
- Modify: `service/utils/statement_upload_validation.py:115-159`
- Modify: `service/tests/test_statement_upload_validation.py`

- [ ] **Step 1: Update existing test and add new test for non-blocking behaviour**

The existing `test_prepare_statement_uploads_returns_valid_rows_and_collects_errors` mocks `get_contact_config` to succeed. Add a new test where config is missing and verify the upload is still returned but flagged with `needs_config_review=True`.

```python
def test_prepare_uploads_flags_missing_config_as_needs_review(monkeypatch) -> None:
    """Uploads without config should be returned with needs_config_review=True, not rejected."""

    monkeypatch.setattr(statement_upload_validation, "count_uploaded_pdf_pages", lambda tid, f: UploadPageCountResult(filename=f.filename or "", page_count=2))
    monkeypatch.setattr(statement_upload_validation, "get_contact_config", _raise_key_error)

    error_messages: list[str] = []
    prepared = prepare_statement_uploads(
        "tenant-1",
        [_make_upload("new-supplier.pdf")],
        ["New Supplier Ltd"],
        {"New Supplier Ltd": "contact-new"},
        error_messages,
    )

    assert len(prepared) == 1
    assert prepared[0].needs_config_review is True
    assert not error_messages  # No error — it's not a failure


def _raise_key_error(*args, **kwargs):
    raise KeyError("Config not found")
```

Also update the existing test to verify `needs_config_review=False` for uploads with config.

- [ ] **Step 2: Run tests to verify new test fails**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_statement_upload_validation.py -v`
Expected: New test FAILS (attribute `needs_config_review` doesn't exist)

- [ ] **Step 3: Modify PreparedStatementUpload and _ensure_contact_config**

Add `needs_config_review: bool = False` to `PreparedStatementUpload` dataclass.

Change `_ensure_contact_config()` to return a status instead of appending errors:
- Config exists → return `"ok"`
- `KeyError` (no config) → return `"needs_config_review"` (no error appended)
- Other exception → return `"error"` (append error message)

Update `prepare_statement_uploads()` to:
- Call `_ensure_contact_config()` and check the return value
- If `"needs_config_review"`: set `needs_config_review=True` on the `PreparedStatementUpload`
- If `"error"`: skip the file (existing behaviour)
- If `"ok"`: proceed normally

- [ ] **Step 4: Run all tests to verify they pass**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests/test_statement_upload_validation.py -v`
Expected: All PASS

- [ ] **Step 5: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 6: Commit**

```bash
git add service/utils/statement_upload_validation.py service/tests/test_statement_upload_validation.py
git commit -m "feat: allow uploads without config, flag as needs_config_review"
```

---

### Task 8: Modify Upload Handler to Branch on Config

**Files:**
- Modify: `service/app.py:107` (ThreadPoolExecutor)
- Modify: `service/app.py:462-569` (upload handling functions)

- [ ] **Step 1: Increase ThreadPoolExecutor max_workers**

Change line 107:
```python
_executor = ThreadPoolExecutor(max_workers=5)
```

- [ ] **Step 2: Import config_suggestion module**

Add to imports in `app.py`:
```python
from core.config_suggestion import suggest_config_for_statement
```

- [ ] **Step 3: Modify `_handle_upload_statements_post` to branch**

After `prepare_statement_uploads()` returns, split prepared uploads into two groups:
- `ready_uploads` — those with `needs_config_review=False` → process as today (reserve tokens, upload S3, start Step Function)
- `review_uploads` — those with `needs_config_review=True` → upload PDF to S3, create statement header row with status `pending_config_review`, submit `suggest_config_for_statement` to `_executor`

For `review_uploads`, do NOT reserve tokens (they're free until confirmation).

```python
ready_uploads = [u for u in prepared if not u.needs_config_review]
review_uploads = [u for u in prepared if u.needs_config_review]

# Process ready uploads as before (reserve, upload, start step function)
# ...

# Submit review uploads to thread pool (no token reservation — free until confirm)
for upload in review_uploads:
    # Generate statement_id using the same pattern as existing uploads (uuid4 hex)
    statement_id = uuid.uuid4().hex

    # Create a minimal DynamoDB header row so the statement appears in /statements
    # with status "pending_config_review". Include: TenantID, StatementID,
    # RecordType="statement", ContactID, ContactName, Filename, PdfPageCount,
    # Status="pending_config_review", UploadedAt=ISO timestamp.
    _create_review_statement_header(tenant_id, statement_id, upload)

    # Upload PDF to S3 at the standard path (reuse existing upload_statement_to_s3)
    pdf_key = f"{tenant_id}/statements/{statement_id}.pdf"
    upload_statement_to_s3(tenant_id, statement_id, upload.uploaded_file)

    _executor.submit(
        suggest_config_for_statement,
        tenant_id=tenant_id,
        contact_id=upload.contact_id,
        contact_name=upload.contact_name,
        statement_id=statement_id,
        pdf_s3_key=pdf_key,
        filename=upload.uploaded_file.filename or "statement.pdf",
        page_count=upload.page_count,
    )
```

The `_create_review_statement_header` helper writes a DynamoDB row to `TenantStatementsTable` with the same key structure as existing statement headers (`TenantID` + `StatementID`), but with `Status="pending_config_review"` instead of the usual processing status, and no billing reservation fields.

- [ ] **Step 4: Update upload success messaging**

Update the response to reflect both counts:
- `success_count` for ready uploads that started processing
- `review_count` for uploads submitted for config review

Add flash/message: "X statements processing. Y statements need config review — go to Configs to confirm."

- [ ] **Step 5: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 6: Commit**

```bash
git add service/app.py
git commit -m "feat: upload handler branches on config existence, submits suggestions to thread pool"
```

---

### Task 9: Add Confirm Config API Endpoint

**Files:**
- Modify: `service/app.py` (add new route)

- [ ] **Step 1: Add `/api/configs/confirm` POST endpoint**

This endpoint handles confirmation of a single suggested config:

```python
@app.route("/api/configs/confirm", methods=["POST"])
@active_tenant_required("Please select a tenant.")
@xero_token_required
@route_handler_logging
def confirm_config_suggestion():
    """Confirm an LLM-suggested config and kick off full extraction."""
    tenant_id = session.get("xero_tenant_id")
    data = request.get_json()

    contact_id = data.get("contact_id", "")
    statement_id = data.get("statement_id", "")
    config_payload = data.get("config", {})

    # Validate mandatory fields (server-side source of truth)
    errors = _validate_config_mandatory_fields(config_payload)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # Save config to DynamoDB
    config = ContactConfig.model_validate(config_payload)
    set_contact_config(tenant_id, contact_id, config)

    # Load suggestion to get page_count for token reservation
    suggestion = get_suggestion(tenant_id, statement_id)
    page_count = suggestion.page_count if suggestion else 0

    # Delete suggestion from S3
    delete_suggestion(tenant_id, statement_id)

    # Reserve tokens using page_count from the suggestion
    # Use BillingService to reserve tokens for page_count pages
    # Then start the Step Function for this statement
    # The PDF is already in S3 from the upload step
    pdf_key = f"{tenant_id}/statements/{statement_id}.pdf"
    json_key = f"{tenant_id}/statements/{statement_id}.json"
    start_textraction_state_machine(tenant_id, contact_id, statement_id, pdf_key, json_key)

    return jsonify({"ok": True, "statement_id": statement_id})
```

- [ ] **Step 2: Add `_validate_config_mandatory_fields` helper**

```python
def _validate_config_mandatory_fields(config: dict) -> list[str]:
    """Validate mandatory config fields, return list of error messages."""
    errors = []
    if not config.get("number"):
        errors.append("'number' (document number column) is required.")
    if not config.get("date"):
        errors.append("'date' (transaction date column) is required.")
    if not config.get("total") or not any(config["total"]):
        errors.append("At least one 'total' column is required.")
    if not config.get("date_format"):
        errors.append("'date_format' is required.")
    return errors
```

- [ ] **Step 3: Add `/api/configs/confirm-all` POST endpoint**

Accepts a list of configs to confirm. Validates each, skips invalid ones, returns result summary:

```python
@app.route("/api/configs/confirm-all", methods=["POST"])
@active_tenant_required("Please select a tenant.")
@xero_token_required
@route_handler_logging
def confirm_all_config_suggestions():
    """Confirm multiple suggested configs. Skips invalid ones."""
    tenant_id = session.get("xero_tenant_id")
    items = request.get_json().get("items", [])

    confirmed = []
    skipped = []
    for item in items:
        errors = _validate_config_mandatory_fields(item.get("config", {}))
        if errors:
            skipped.append({"statement_id": item.get("statement_id"), "errors": errors})
            continue
        # Save, delete suggestion, reserve tokens, start step function
        # ...
        confirmed.append(item.get("statement_id"))

    return jsonify({"confirmed": confirmed, "skipped": skipped})
```

- [ ] **Step 4: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 5: Commit**

```bash
git add service/app.py
git commit -m "feat: add config confirm and confirm-all API endpoints"
```

---

### Task 10: Redesign `/configs` Route and Template

**Files:**
- Modify: `service/app.py:1374-1425` (configs route)
- Modify: `service/templates/configs.html`

- [ ] **Step 1: Update configs route to load pending suggestions**

In the `configs()` GET handler, after loading contacts, also load pending suggestions:

```python
from core.config_suggestion import get_pending_suggestions

# In configs() GET:
pending_suggestions = get_pending_suggestions(tenant_id)
# Add to context:
context["pending_suggestions"] = [s.model_dump() for s in pending_suggestions]
```

- [ ] **Step 2: Redesign configs.html template**

The template needs two sections:

**Section 1 — Pending Review Cards** (shown only when `pending_suggestions` is non-empty):
- Loop over `pending_suggestions`
- Each card shows: contact name, filename, counter (1/N)
- Dropdowns for `number`, `date`, `due_date` populated with `detected_headers`
- Multi-select for `total` populated with `detected_headers`
- Header uniqueness constraint: JavaScript removes selected options from other dropdowns (except `total` allows shared)
- Text input for `date_format` (pre-filled, highlighted red if empty)
- Select inputs for `decimal_separator`, `thousands_separator`
- `confidence_notes` shown as info alert (hidden if empty)
- Per-card "Confirm" button (disabled until mandatory fields filled)
- "Edit in full config page" link
- "Confirm All" button at bottom (disabled unless all cards have valid mandatory fields, confirms only valid cards)

**Section 2 — Existing Config Editor** (existing functionality, preserved):
- Contact selector with datalist
- Load/save config form
- Keep all existing behaviour

JavaScript additions:
- Header uniqueness: on dropdown change, disable that option in sibling dropdowns
- Mandatory field validation: enable/disable confirm button based on field state
- Confirm button: POST to `/api/configs/confirm`, on success show "Submitted" then remove card
- Confirm All: POST to `/api/configs/confirm-all`, handle partial success messaging

- [ ] **Step 3: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 4: Manual test**

Verify the `/configs` page renders correctly with both sections. If no pending suggestions, only the existing config editor shows.

- [ ] **Step 5: Commit**

```bash
git add service/app.py service/templates/configs.html
git commit -m "feat: redesign /configs page with pending review cards and header dropdowns"
```

---

### Task 11: Add Notification Banner to Base Template

**Files:**
- Modify: `service/templates/base.html`
- Modify: `service/app.py` (add context processor)

- [ ] **Step 1: Add Flask context processor for pending review count**

In `app.py`, add a context processor that injects the pending review count into all templates:

```python
import time

@app.context_processor
def inject_pending_review_count():
    """Make pending config review count available to all templates.

    Caches the count in the session for 60 seconds to avoid an S3 list
    call on every page load.
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        return {"pending_config_review_count": 0}

    cache_key = "_pending_review_count"
    cache_ts_key = "_pending_review_count_ts"
    now = time.time()

    # Return cached value if fresh (< 60s old)
    cached_ts = session.get(cache_ts_key, 0)
    if now - cached_ts < 60:
        return {"pending_config_review_count": session.get(cache_key, 0)}

    try:
        from core.config_suggestion import get_pending_suggestion_count
        count = get_pending_suggestion_count(tenant_id)
    except Exception:
        count = 0

    session[cache_key] = count
    session[cache_ts_key] = now
    return {"pending_config_review_count": count}
```

Add `get_pending_suggestion_count(tenant_id)` to `config_suggestion.py` — a lightweight S3 `list_objects_v2` call that counts objects under the `config-suggestions/` prefix without loading them. Define this in Task 6 alongside `get_pending_suggestions` and `delete_suggestion`.

- [ ] **Step 2: Add banner to base.html**

After the nav and before `{% block content %}`, add:

```html
{% if pending_config_review_count > 0 %}
<div class="alert alert-info alert-dismissible mx-3 mt-3 mb-0" role="alert">
    <strong>{{ pending_config_review_count }} statement{{ 's' if pending_config_review_count != 1 }} need{{ 's' if pending_config_review_count == 1 }} config review.</strong>
    <a href="{{ url_for('configs') }}" class="alert-link">Review now</a>
    <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
</div>
{% endif %}
```

- [ ] **Step 3: Run `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Pass

- [ ] **Step 4: Commit**

```bash
git add service/app.py service/templates/base.html service/core/config_suggestion.py
git commit -m "feat: add notification banner for pending config reviews across all pages"
```

---

### Task 12: Integration Test and Cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run full test suite**

Run: `cd /home/ollie/statement-processor/service && source venv/bin/activate && python3.13 -m pytest tests -v`
Expected: All pass

- [ ] **Step 2: Run full `make dev`**

Run: `cd /home/ollie/statement-processor/service && make dev`
Expected: Format, lint, type-check, test, security all pass. Review output carefully — make targets use `|| true`.

- [ ] **Step 3: Update README.md**

Check `/home/ollie/statement-processor/README.md` and add documentation about:
- The auto config suggestion feature
- How it works (Textract page 1 → Bedrock Haiku 4.5 → user confirms)
- The new status values (`pending_config_review`, `config_suggestion_failed`)
- The Bedrock model access requirement (manual console step)

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "docs: update README with auto config suggestion feature"
```
