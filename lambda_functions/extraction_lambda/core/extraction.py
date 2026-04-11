"""Bedrock extraction boundary.

This module is the sole interface between the Lambda and Bedrock for
statement extraction. All PDF chunking, LLM calls, post-processing,
and chunk-boundary deduplication happen here.

Public API:
    extract_statement(pdf_bytes, page_count) -> ExtractionResult
"""

import functools
import io
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from core.models import ExtractionResult, StatementItem
from logger import logger

# region Constants

MODEL_ID = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"

# Pages per chunk and overlap for multi-chunk PDFs.
CHUNK_SIZE = 10

# Bedrock document block size limit (~4.5 MB). Use 4 MB as safety margin.
MAX_CHUNK_BYTES = 4 * 1024 * 1024

# Concurrency for parallel chunk processing.
MAX_PARALLEL_CHUNKS = 10

# Retry config for transient Bedrock errors.
MAX_RETRIES = 2
BASE_RETRY_DELAY = 2.0

# Standard fields that map to named keys in the output dict.
STANDARD_FIELDS = {"date", "number", "due_date", "reference"}

# Currency prefix pattern: 1-3 letters optionally followed by whitespace.
# Strips R, $, EUR, ZAR, USD, etc. before numeric parsing.
_CURRENCY_PREFIX_RE = re.compile(r"^[A-Za-z\u20ac$\u00a3\u00a5\u20b9]{1,3}\s*")

# System prompt loaded once from adjacent markdown file.
_PROMPT_PATH = Path(__file__).parent / "extraction_prompt.md"

# endregion

# region Data structures


@dataclass(frozen=True)
class PdfChunk:
    """A self-contained PDF chunk with its page range.

    Replaces the raw (bytes, start_page, end_page) tuple for clarity
    at call sites that previously unpacked positional indices.
    """

    pdf_bytes: bytes
    start_page: int  # 1-indexed
    end_page: int  # 1-indexed, inclusive


@dataclass(frozen=True)
class BedrockResponse:
    """Parsed response from a single Bedrock Converse API call.

    Bundles the tool output with token usage and request ID so callers
    don't need to remember tuple positions.
    """

    tool_input: dict[str, Any]
    input_tokens: int
    output_tokens: int
    request_id: str


@dataclass(frozen=True)
class ChunkResult:
    """Result of processing a single continuation chunk.

    Captures everything needed to merge a chunk back into the main
    item list: the items, token usage, and any metadata warnings.
    """

    chunk_index: int
    items: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    request_id: str
    warnings: dict[str, str]


# endregion

# region Tool schema

EXTRACT_TOOL: dict[str, Any] = {
    "name": "extract_statement_rows",
    "description": "Extract structured line items from a supplier statement PDF.",
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "detected_headers": {"type": "array", "items": {"type": "string"}, "description": "The column headers detected in the main statement table."},
                "date_format": {"type": "string", "description": "Detected date format using SDF tokens (e.g. 'DD.MM.YYYY')."},
                "column_order": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ordered list of column names for each item array. "
                        "Use 'date', 'number', 'due_date', 'reference' for standard fields. "
                        "Use the PDF column header name for monetary and extra columns."
                    ),
                },
                "items": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}, "description": "One row as a flat array of values matching column_order."},
                    "description": "All rows as arrays of strings, one per line item.",
                },
            },
            "required": ["detected_headers", "date_format", "column_order", "items"],
        }
    },
}


# endregion

# region PDF chunking


def chunk_pdf(reader: PdfReader) -> list[PdfChunk]:
    """Split a PDF into overlapping page chunks.

    Each chunk is a self-contained PDF (as bytes) with 1-page overlap
    between consecutive chunks so rows spanning page boundaries are
    captured. Chunks exceeding MAX_CHUNK_BYTES are recursively halved.

    Returns:
        List of PdfChunk (pdf_bytes, start_page, end_page) with 1-indexed pages.
    """
    total_pages = len(reader.pages)

    # Build page ranges with 1-page overlap.
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total_pages:
        end = min(start + CHUNK_SIZE, total_pages)
        ranges.append((start, end))
        start = end - 1 if end < total_pages else end

    chunks: list[PdfChunk] = []
    for page_start, page_end in ranges:
        sub_chunks = _build_chunk_bytes(reader, page_start, page_end)
        chunks.extend(sub_chunks)

    return chunks


def _build_chunk_bytes(reader: PdfReader, page_start: int, page_end: int) -> list[PdfChunk]:
    """Build PDF bytes for a page range, splitting if over size limit."""
    writer = PdfWriter()
    for i in range(page_start, page_end):
        writer.add_page(reader.pages[i])

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    if len(pdf_bytes) <= MAX_CHUNK_BYTES or (page_end - page_start) <= 1:
        if len(pdf_bytes) > MAX_CHUNK_BYTES:
            logger.error("Single page exceeds Bedrock document block size limit", page=page_start + 1, chunk_bytes=len(pdf_bytes), max_bytes=MAX_CHUNK_BYTES)
        return [PdfChunk(pdf_bytes=pdf_bytes, start_page=page_start + 1, end_page=page_end)]

    # Too large -- split in half and recurse.
    mid = page_start + (page_end - page_start) // 2
    left = _build_chunk_bytes(reader, page_start, mid)
    right = _build_chunk_bytes(reader, mid, page_end)
    return left + right


# endregion

# region Post-processing


def convert_amount(raw: str) -> float | str:
    """Convert a raw monetary string to a float.

    Handles currency prefixes (R, $, ZAR, EUR, etc.), trailing minus
    signs, parenthetical negatives. Uses a heuristic to detect
    decimal vs thousands separators based on digit count after the
    last separator:
    - 2 digits after → decimal separator
    - 3+ digits after → thousands separator (no decimal shown)
    - 1 digit after → decimal separator

    Returns float or original string if conversion fails.
    """
    s = raw.strip()
    if not s:
        return s

    # Strip currency prefix (R, $, EUR, ZAR, USD, etc.)
    s = _CURRENCY_PREFIX_RE.sub("", s).strip()

    # Detect negative: trailing minus, parentheses, or leading minus.
    negative = False
    if s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    elif s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    elif s.startswith("-"):
        negative = True
        s = s[1:].strip()

    # Heuristic: find the last separator and determine its role
    # based on how many digits follow it.
    last_dot = s.rfind(".")
    last_comma = s.rfind(",")
    last_sep_pos = max(last_dot, last_comma)

    if last_sep_pos >= 0:
        digits_after = len(s) - last_sep_pos - 1
        last_sep = s[last_sep_pos]
        other_sep = "," if last_sep == "." else "."

        if digits_after <= 2:
            # Last separator is decimal — strip all other separators.
            s = s.replace(other_sep, "").replace(" ", "").replace("'", "")
            if last_sep != ".":
                s = s.replace(last_sep, ".")
        else:
            # Last separator is thousands (3+ digits after) — no decimal.
            s = s.replace(",", "").replace(".", "").replace(" ", "").replace("'", "")
    else:
        # No separator at all — strip spaces/apostrophes only.
        s = s.replace(" ", "").replace("'", "")

    try:
        value = float(s)
        return -value if negative else value
    except ValueError:
        return raw


def reconstruct_items(column_order: list[str], raw_rows: list[list[str]]) -> list[dict[str, Any]]:
    """Reconstruct item dicts from compact array-of-arrays format.

    Maps each flat value array into a structured dict with standard
    fields (date, number, due_date, reference), monetary columns in
    ``total``, and ALL columns in ``raw`` for debugging.
    """
    items: list[dict[str, Any]] = []
    for row in raw_rows:
        item: dict[str, Any] = {"date": "", "number": "", "due_date": "", "reference": "", "total": {}, "raw": {}}
        for idx, col_name in enumerate(column_order):
            val = row[idx] if idx < len(row) else ""
            # ALL columns go into raw for debugging.
            item["raw"][col_name] = val
            if col_name.lower() in STANDARD_FIELDS:
                item[col_name.lower()] = val
            else:
                # Non-standard columns go into total (monetary).
                item["total"][col_name] = val
        items.append(item)
    return items


def postprocess_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw string totals to numeric values.

    Also logs conversion failures for post-migration debugging.
    Mutates items in place and returns the same list.
    """
    for item in items:
        if "total" in item and isinstance(item["total"], dict):
            converted: dict[str, Any] = {}
            for k, v in item["total"].items():
                result = convert_amount(str(v))
                if isinstance(result, str) and result:
                    logger.warning("convert_amount fallback", raw_value=v, reason="returned_as_string")
                converted[k] = result
            item["total"] = converted
    return items


def compute_date_confidence(date_format: str, items: list[dict[str, Any]]) -> str:
    """Determine whether the date format is ambiguous from the data itself.

    Parses the date_format to find which component is the year, then
    checks the other two (day-or-month) components across all items.
    If either component ever exceeds 12, the format is unambiguous ("high").
    If both are always <= 12, it's genuinely ambiguous ("low").

    Named-month formats (MMM, MMMM) are always unambiguous.
    """
    # Named months make the format unambiguous.
    if "MMM" in date_format:
        return "high"

    # Identify the separator from the format string (e.g. "/" in DD/MM/YYYY).
    sep = ""
    for ch in date_format:
        if ch not in "DMYdoy":
            sep = ch
            break

    if not sep:
        return "high"

    # Find which position is the year so we can check the other two.
    format_parts = date_format.split(sep)
    year_idx: int | None = None
    for i, part in enumerate(format_parts):
        if part.startswith("Y"):
            year_idx = i
            break

    if year_idx is None or len(format_parts) != 3:
        return "high"

    # Check the two non-year components across all date values.
    for item in items:
        date_str = item.get("date", "")
        if not date_str:
            continue

        parts = date_str.split(sep)
        if len(parts) != 3:
            continue

        for i, part in enumerate(parts):
            if i == year_idx:
                continue
            try:
                if int(part) > 12:
                    return "high"
            except ValueError:
                # Non-numeric component (e.g. month name) — unambiguous.
                return "high"

    return "low"


def build_header_mapping(detected_headers: list[str], column_order: list[str]) -> dict[str, str]:
    """Build header_mapping from detected_headers and column_order.

    Zips the two lists: where column_order[i] is a standard field name,
    maps detected_headers[i] -> column_order[i]. Otherwise maps to "total".

    Example::

        detected_headers=["Date", "Reference", "Debit", "Credit"]
        column_order=["date", "number", "Debit", "Credit"]
        -> {"Date": "date", "Reference": "number", "Debit": "total", "Credit": "total"}
    """
    mapping: dict[str, str] = {}
    for i in range(min(len(detected_headers), len(column_order))):
        header = detected_headers[i]
        col = column_order[i]
        if col.lower() in STANDARD_FIELDS:
            mapping[header] = col.lower()
        else:
            mapping[header] = "total"
    return mapping


def strip_overlap_prefix(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the leading overlap block from an incoming chunk's items.

    Chunks share a 1-page overlap, so the incoming chunk may start with
    rows already present at the tail of the existing list. This function
    finds the longest prefix of ``incoming`` that matches a suffix of
    ``existing`` and returns the non-overlapping remainder.

    Safe because it requires exact match across all fields — different
    rows are never dropped.
    """
    if not existing or not incoming:
        return incoming

    # Find where incoming[0] first appears in the tail of existing.
    # Only search the tail (up to len(incoming)) to avoid false matches
    # deep in the list.
    search_start = max(0, len(existing) - len(incoming))
    match_start: int | None = None

    for i in range(search_start, len(existing)):
        if _items_equal(existing[i], incoming[0]):
            match_start = i
            break

    if match_start is None:
        return incoming

    # Verify the full block matches.
    overlap_len = len(existing) - match_start
    if overlap_len > len(incoming):
        return incoming

    for j in range(overlap_len):
        if not _items_equal(existing[match_start + j], incoming[j]):
            # Partial match — not a real overlap block.
            return incoming

    logger.info("Chunk overlap stripped", overlap_items=overlap_len)
    return incoming[overlap_len:]


def _items_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Compare two items across all fields for exact equality."""
    keys = {"date", "number", "due_date", "reference", "total", "raw"}
    return all(a.get(k) == b.get(k) for k in keys)


# endregion

# region Bedrock API


def _get_bedrock_client() -> Any:
    """Return the shared Bedrock runtime client.

    Separated into a function so integration tests can mock it.
    """
    # Deferred: creating the boto3 client is expensive (HTTP setup, credential
    # resolution) so we avoid the cost on cold start until actually needed.
    from config import bedrock_runtime_client  # pylint: disable=import-outside-toplevel

    return bedrock_runtime_client


@functools.lru_cache(maxsize=1)
def _load_system_prompt() -> str:
    """Load the system prompt from the adjacent markdown file."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _call_bedrock(client: Any, system_prompt: str, pdf_bytes: bytes, user_text: str) -> BedrockResponse:
    """Call Bedrock Converse API with a PDF document and forced tool use.

    Returns:
        BedrockResponse with parsed tool input, token counts, and request ID.
    """
    response = client.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"document": {"name": "statement", "format": "pdf", "source": {"bytes": pdf_bytes}}}, {"text": user_text}]}],
        toolConfig={"tools": [{"toolSpec": EXTRACT_TOOL}], "toolChoice": {"tool": {"name": "extract_statement_rows"}}},
    )

    content_blocks = response.get("output", {}).get("message", {}).get("content", [])
    for block in content_blocks:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == "extract_statement_rows":
            usage = response.get("usage", {})
            request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
            return BedrockResponse(tool_input=tool_use["input"], input_tokens=usage.get("inputTokens", 0), output_tokens=usage.get("outputTokens", 0), request_id=request_id)

    raise ValueError("Bedrock response did not contain an extract_statement_rows tool use block")


def _call_bedrock_with_retry(client: Any, system_prompt: str, pdf_bytes: bytes, user_text: str) -> BedrockResponse:
    """Call Bedrock with retries for transient server/throttling errors.

    Retries up to MAX_RETRIES times with exponential backoff.
    Fails immediately on client/validation errors.
    """
    retryable_codes = {"InternalServerException", "ServiceUnavailableException"}
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            return _call_bedrock(client, system_prompt, pdf_bytes, user_text)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error_code = ""
            # ThrottlingException is a named exception on the client.
            if hasattr(client, "exceptions") and isinstance(exc, client.exceptions.ThrottlingException):
                error_code = "ThrottlingException"
            else:
                resp = getattr(exc, "response", None) or {}
                error_code = resp.get("Error", {}).get("Code", "")

            if error_code in retryable_codes or error_code == "ThrottlingException":
                last_error = exc
                if attempt < MAX_RETRIES:
                    delay = BASE_RETRY_DELAY * (2**attempt)
                    logger.warning("Bedrock transient error, retrying", attempt=attempt + 1, max_retries=MAX_RETRIES, delay_seconds=delay, error_code=error_code)
                    time.sleep(delay)
            else:
                raise

    raise last_error  # type: ignore[misc]


# endregion

# region Main entry point


def extract_statement(pdf_bytes: bytes, page_count: int, on_chunk_complete: Callable[[int, int], None] | None = None) -> "ExtractionResult":
    """Extract structured line items from a statement PDF.

    This is the sole entry point for statement extraction. Callers
    depend only on this function signature and ExtractionResult.

    The implementation chunks the PDF, calls Bedrock Haiku for each
    chunk (parallel for chunks 2+), reconstructs items, runs numeric
    post-processing, and deduplicates chunk boundaries.

    Args:
        pdf_bytes: Raw PDF bytes to extract from.
        page_count: Total page count (used in continuation prompts).
        on_chunk_complete: Optional progress callback. Called as
            ``on_chunk_complete(completed, total)`` after each chunk
            finishes. ``completed=0`` signals extraction start (all
            chunks known). Subsequent calls increment ``completed``
            up to ``total``.
    """
    client = _get_bedrock_client()
    system_prompt = _load_system_prompt()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    chunks = chunk_pdf(reader)
    chunk_count = len(chunks)
    request_ids: list[str] = []

    # Signal extraction start so the caller can transition to "extracting".
    if on_chunk_complete:
        on_chunk_complete(0, chunk_count)

    # -- Process chunk 1 (provides headers + metadata for rest) --

    first_chunk = chunks[0]
    logger.info("Processing chunk", chunk=1, total_chunks=chunk_count, pages=f"{first_chunk.start_page}-{first_chunk.end_page}")

    first_response = _call_bedrock_with_retry(client, system_prompt, first_chunk.pdf_bytes, "Extract all line items from this statement.")
    request_ids.append(first_response.request_id)

    detected_headers = first_response.tool_input.get("detected_headers", [])
    column_order = first_response.tool_input.get("column_order", [])
    date_format = first_response.tool_input.get("date_format", "")

    logger.info("Chunk 1 metadata", detected_headers=detected_headers, column_order=column_order, date_format=date_format)

    all_items = reconstruct_items(column_order, first_response.tool_input.get("items", []))
    logger.info("Chunk 1 items", count=len(all_items))

    if on_chunk_complete:
        on_chunk_complete(1, chunk_count)

    total_input_tokens = first_response.input_tokens
    total_output_tokens = first_response.output_tokens

    # -- Process remaining chunks in parallel --

    if chunk_count > 1:
        headers_str = ", ".join(detected_headers)
        col_order_str = json.dumps(column_order)

        def _process_continuation(chunk_idx: int) -> ChunkResult:
            """Process a single continuation chunk."""
            chunk = chunks[chunk_idx]
            logger.info("Processing chunk", chunk=chunk_idx + 1, total_chunks=chunk_count, pages=f"{chunk.start_page}-{chunk.end_page}")

            c_user_text = (
                f"This is a continuation of a multi-page statement "
                f"(pages {chunk.start_page}-{chunk.end_page} of {page_count}).\n"
                f"The table headers from page 1 are: "
                f"[{headers_str}]\n"
                f"Use this exact column_order: {col_order_str}\n"
                f"If headers are repeated on these pages, skip the "
                f"header rows.\n"
                f"If headers are NOT present on these pages, use "
                f"the headers above to identify columns.\n"
                f"Extract ALL data rows from every page in this chunk."
            )
            c_response = _call_bedrock_with_retry(client, system_prompt, chunk.pdf_bytes, c_user_text)

            # Log metadata disagreements with chunk 1 (the canonical source).
            warnings: dict[str, str] = {}
            canonical_meta = {"date_format": date_format}
            for field, canonical in canonical_meta.items():
                chunk_val = c_response.tool_input.get(field, "")
                if chunk_val and chunk_val != canonical:
                    warnings[field] = chunk_val
                    logger.warning("Chunk metadata disagreement", chunk=chunk_idx + 1, field=field, chunk_value=chunk_val, canonical_value=canonical)

            c_col_order = c_response.tool_input.get("column_order", column_order)
            c_items = reconstruct_items(c_col_order, c_response.tool_input.get("items", []))
            logger.info("Chunk items", chunk=chunk_idx + 1, count=len(c_items))

            return ChunkResult(chunk_index=chunk_idx, items=c_items, input_tokens=c_response.input_tokens, output_tokens=c_response.output_tokens, request_id=c_response.request_id, warnings=warnings)

        # Dispatch chunks 2+ in parallel, merge in page order.
        chunk_results: dict[int, ChunkResult] = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHUNKS) as pool:
            futures = {pool.submit(_process_continuation, idx): idx for idx in range(1, chunk_count)}
            for future in as_completed(futures):
                result = future.result()
                chunk_results[result.chunk_index] = result
                if on_chunk_complete:
                    # +1 because chunk 0 was already completed before parallel dispatch.
                    completed_so_far = len(chunk_results) + 1
                    on_chunk_complete(completed_so_far, chunk_count)

        # Merge in chunk order, stripping overlap prefixes.
        # Note: cr.warnings was already logged inside _process_continuation.
        for idx in range(1, chunk_count):
            cr = chunk_results[idx]
            deduped_items = strip_overlap_prefix(all_items, cr.items)
            all_items.extend(deduped_items)
            total_input_tokens += cr.input_tokens
            total_output_tokens += cr.output_tokens
            request_ids.append(cr.request_id)

    # -- Post-process --

    all_items = postprocess_items(all_items)
    logger.info("Post-merge item count", item_count=len(all_items))

    # Compute date_confidence from the actual date values rather than
    # relying on the LLM. If any date has a day-or-month component > 12,
    # the format is unambiguous. Otherwise it's genuinely ambiguous.
    date_confidence = compute_date_confidence(date_format, all_items)
    logger.info("date_confidence", confidence=date_confidence, date_format=date_format)

    # Build header_mapping from chunk 1 metadata.
    header_mapping = build_header_mapping(detected_headers, column_order)
    logger.info("header_mapping", mapping=header_mapping)

    # Convert raw dicts to StatementItem models.
    statement_items: list[StatementItem] = []
    for item in all_items:
        item["statement_item_id"] = ""  # Set by orchestrator later.
        statement_items.append(StatementItem.model_validate(item))

    return ExtractionResult(
        items=statement_items,
        detected_headers=detected_headers,
        header_mapping=header_mapping,
        date_format=date_format,
        date_confidence=date_confidence,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        request_ids=request_ids,
    )


# endregion
