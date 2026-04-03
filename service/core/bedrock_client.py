"""Thin Bedrock wrapper for config suggestion via tool use.

Builds a prompt from statement headers and sample rows, invokes
Haiku 4.5 via the Bedrock Converse API with a forced tool call,
and parses the structured response into a config dict.
"""

from typing import Any

from config import bedrock_runtime_client
from logger import logger

# EU cross-region inference profile ID for Bedrock Converse API.
# Newer Bedrock models require an inference profile (region-prefixed)
# rather than the raw foundation model ID for on-demand invocation.
HAIKU_MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

# Tool definition for the Bedrock Converse API (camelCase keys required).
SUGGEST_CONFIG_TOOL: dict[str, Any] = {
    "name": "suggest_config",
    "description": "Suggest column mappings for a supplier statement based on detected headers and sample data rows.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "Header name for the document/invoice number column. Empty string if not found."},
                "date": {"type": "string", "description": "Header name for the transaction date column. Empty string if not found."},
                "due_date": {"type": "string", "description": "Header name for the due date column. Empty string if not applicable."},
                "total": {"type": "array", "items": {"type": "string"}, "description": "Header name(s) for monetary amount columns (e.g. ['Amount'] or ['Debit', 'Credit'])."},
                "date_format": {"type": "string", "description": "SDF date format string for the date column (e.g. 'DD/MM/YYYY'). Use ONLY SDF tokens."},
                "decimal_separator": {"type": "string", "description": "Character used as decimal separator in amounts ('.' or ',')."},
                "thousands_separator": {"type": "string", "description": "Character used as thousands separator in amounts (',' or '.' or '' for none)."},
                "confidence_notes": {"type": "string", "description": "Brief notes about mapping confidence or ambiguities."},
            },
            "required": ["number", "date", "due_date", "total", "date_format", "decimal_separator", "thousands_separator", "confidence_notes"],
        }
    },
}

# SDF token reference included in every prompt so the LLM uses the correct format tokens.
_SDF_REFERENCE = """
## SDF (Supplier Date Format) Token Reference

| Token  | Meaning            | Example     |
|--------|--------------------|-------------|
| YYYY   | 4-digit year       | 2025        |
| YY     | 2-digit year       | 25          |
| MMMM   | Full month name    | January     |
| MMM    | Abbreviated month  | Jan         |
| MM     | Zero-padded month  | 03          |
| M      | Month (no padding) | 3           |
| DD     | Zero-padded day    | 05          |
| D      | Day (no padding)   | 5           |
| Do     | Day with ordinal   | 5th         |
| dddd   | Full day name      | Monday      |

### SDF Examples
- DD/MM/YYYY  →  15/03/2025
- D MMMM YYYY  →  5 January 2025
- MM-DD-YY  →  03-15-25
- YYYY-MM-DD  →  2025-03-15

Do NOT use Python strftime or Java SimpleDateFormat. Use ONLY the SDF tokens listed above.
"""


def build_suggestion_prompt(headers: list[str], rows: list[list[str]]) -> str:
    """Build the user message for the config suggestion LLM call.

    Args:
        headers: Column headers detected from the statement table.
        rows: Sample data rows (list of cell values per row).

    Returns:
        Formatted prompt string including headers, rows, and SDF reference.
    """
    # Format sample data as a readable table.
    table_lines = [" | ".join(headers)]
    table_lines.append(" | ".join("---" for _ in headers))
    for row in rows:
        # Pad row to match header count in case of ragged rows.
        padded = row + [""] * (len(headers) - len(row))
        table_lines.append(" | ".join(padded[: len(headers)]))

    table_str = "\n".join(table_lines)

    return f"""You are analysing a supplier statement PDF. Below are the column headers and sample data rows extracted from the first page.

## Detected Headers and Sample Data

{table_str}

{_SDF_REFERENCE}

## Instructions

Map the headers to the following fields:
- **number**: The document or invoice number column
- **date**: The transaction or invoice date column
- **due_date**: The due/payment date column (empty string if not present)
- **total**: One or more monetary amount columns (e.g. ["Amount"] or ["Debit", "Credit"])
- **date_format**: The SDF format string matching the date values in the data
- **decimal_separator**: The decimal separator used in monetary amounts ("." or ",")
- **thousands_separator**: The thousands separator used in monetary amounts ("," or "." or "" for none)
- **confidence_notes**: Any notes about uncertain or ambiguous mappings

Return empty string for any field you cannot confidently map. Use the suggest_config tool to return your answer."""


def suggest_column_mapping(headers: list[str], rows: list[list[str]]) -> tuple[dict[str, Any], str]:
    """Call Bedrock Haiku to suggest column mappings for a statement.

    Args:
        headers: Column headers from the statement.
        rows: Sample data rows.

    Returns:
        Tuple of (config_dict, confidence_notes).
    """
    prompt = build_suggestion_prompt(headers, rows)

    logger.info("Invoking Bedrock for config suggestion", model_id=HAIKU_MODEL_ID, header_count=len(headers), row_count=len(rows))

    response = bedrock_runtime_client.converse(
        modelId=HAIKU_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        toolConfig={"tools": [{"toolSpec": SUGGEST_CONFIG_TOOL}], "toolChoice": {"tool": {"name": "suggest_config"}}},
    )

    return parse_suggestion_response(response)


def parse_suggestion_response(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Extract the tool use result from a Bedrock Converse API response.

    Args:
        response: Raw Bedrock Converse API response dict.

    Returns:
        Tuple of (config_dict, confidence_notes).

    Raises:
        ValueError: If no tool use block is found in the response.
    """
    content_blocks = response.get("output", {}).get("message", {}).get("content", [])

    for block in content_blocks:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == "suggest_config":
            tool_input = tool_use["input"]
            confidence_notes = tool_input.pop("confidence_notes", "")
            return tool_input, confidence_notes

    raise ValueError("Bedrock response did not contain a suggest_config tool use block")
