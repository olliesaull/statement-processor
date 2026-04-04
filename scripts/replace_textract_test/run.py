"""Sonnet extraction test script.

Reads PDFs from the pdfs/ directory, sends each to Sonnet 4.6 via
Bedrock Converse API with forced tool use, and writes structured
JSON output for manual accuracy comparison against Textract.

Large PDFs are chunked at ~10 pages per request with 1-page overlap
to stay within context window limits.
"""

import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from pypdf import PdfReader, PdfWriter

# -- Config ------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
INPUT_DIR = SCRIPT_DIR / "pdfs"
OUTPUT_DIR = SCRIPT_DIR / "output"
CHUNK_SIZE = 10
AWS_PROFILE = os.environ.get("AWS_PROFILE", "dotelastic-production")
AWS_REGION = "eu-west-1"
MODEL_ID = "eu.anthropic.claude-sonnet-4-6"
SYSTEM_PROMPT_PATH = SCRIPT_DIR / "system_prompt.md"
COST_PER_INPUT_TOKEN = 3.0 / 1_000_000  # $3/M input tokens
COST_PER_OUTPUT_TOKEN = 15.0 / 1_000_000  # $15/M output tokens

# Max retries for transient Bedrock errors.
MAX_RETRIES = 2
# Base delay in seconds for exponential backoff.
BASE_RETRY_DELAY = 2.0

# Bedrock document block size limit (~4.5 MB). Use 4 MB as safety margin.
MAX_CHUNK_BYTES = 4 * 1024 * 1024

# Concurrency limits — kept low to avoid Bedrock throttling.
MAX_PARALLEL_PDFS = 4
MAX_PARALLEL_CHUNKS = 3


# -- Tool schema -------------------------------------------------------------

# pylint: disable=line-too-long
# Compact schema: items are arrays-of-arrays instead of objects.
# Column order is defined once in `column_order`; each item is a flat
# array of values matching that order. This eliminates repeated key
# names and cuts output tokens by ~50%.
EXTRACT_TOOL: dict[str, Any] = {
    "name": "extract_statement_rows",
    "description": ("Extract structured line items from a supplier statement PDF. Use compact array-of-arrays format for items."),
    "inputSchema": {
        "json": {
            "type": "object",
            "properties": {
                "detected_headers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("The column headers detected in the main statement table."),
                },
                "date_format": {
                    "type": "string",
                    "description": ("Detected date format using SDF tokens (e.g. 'DD.MM.YYYY'). Scan all dates — if any day > 12, use that to disambiguate DD vs MM."),
                },
                "decimal_separator": {
                    "type": "string",
                    "enum": [".", ","],
                    "description": ("Character used as decimal separator in monetary amounts."),
                },
                "thousands_separator": {
                    "type": "string",
                    "enum": [",", ".", " ", "'", ""],
                    "description": ("Character used as thousands separator in monetary amounts. Empty string if none."),
                },
                "column_order": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Ordered list of column names for each item array. E.g. ['date', 'number', 'due_date', 'reference', 'Debit', 'Credit', 'Balance', 'Description']. Use 'date', 'number', 'due_date', 'reference' for the standard fields. Use the PDF column header name for monetary columns and any extra columns."),
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": ("One row as a flat array of values matching column_order. Use empty string for missing values."),
                    },
                    "description": ("All rows as arrays of strings, one per line item. Each array's values correspond positionally to column_order."),
                },
            },
            "required": [
                "detected_headers",
                "date_format",
                "decimal_separator",
                "thousands_separator",
                "column_order",
                "items",
            ],
        }
    },
}
# pylint: enable=line-too-long

# Standard fields that map to named keys in the output dict.
STANDARD_FIELDS = {"date", "number", "due_date", "reference"}


# -- PDF chunking ------------------------------------------------------------


def chunk_pdf(reader: PdfReader) -> list[tuple[bytes, int, int]]:
    """Split a PDF into overlapping page chunks.

    Each chunk is a self-contained PDF (as bytes) with 1-page overlap
    between consecutive chunks so rows spanning page boundaries are
    captured. If a chunk exceeds MAX_CHUNK_BYTES, it is recursively
    halved until each sub-chunk fits.

    Args:
        reader: PdfReader for the source PDF.

    Returns:
        List of (pdf_bytes, start_page_1indexed, end_page_1indexed).
    """
    total_pages = len(reader.pages)

    # Build page ranges with 1-page overlap.
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total_pages:
        end = min(start + CHUNK_SIZE, total_pages)
        ranges.append((start, end))
        # Next chunk starts at the last page of this chunk (overlap).
        start = end - 1 if end < total_pages else end

    chunks: list[tuple[bytes, int, int]] = []
    for page_start, page_end in ranges:
        sub_chunks = _build_chunk_bytes(reader, page_start, page_end)
        chunks.extend(sub_chunks)

    return chunks


def _build_chunk_bytes(
    reader: PdfReader,
    page_start: int,
    page_end: int,
) -> list[tuple[bytes, int, int]]:
    """Build PDF bytes for a page range, splitting if over size limit.

    Recursively halves the page range until each chunk is under
    MAX_CHUNK_BYTES (Bedrock document block limit).

    Args:
        reader: PdfReader for the source PDF.
        page_start: Start page index (0-based, inclusive).
        page_end: End page index (0-based, exclusive).

    Returns:
        List of (pdf_bytes, start_page_1indexed, end_page_1indexed).
    """
    writer = PdfWriter()
    for i in range(page_start, page_end):
        writer.add_page(reader.pages[i])

    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    if len(pdf_bytes) <= MAX_CHUNK_BYTES or (page_end - page_start) <= 1:
        # Fits, or can't split further (single page).
        return [(pdf_bytes, page_start + 1, page_end)]

    # Too large — split in half and recurse.
    mid = page_start + (page_end - page_start) // 2
    left = _build_chunk_bytes(reader, page_start, mid)
    right = _build_chunk_bytes(reader, mid, page_end)
    return left + right


# -- Bedrock API -------------------------------------------------------------


def call_bedrock(
    client: Any,
    system_prompt: str,
    pdf_bytes: bytes,
    user_text: str,
) -> tuple[dict[str, Any], int, int]:
    """Call Bedrock Converse API with a PDF document and forced tool use.

    Args:
        client: boto3 bedrock-runtime client.
        system_prompt: System prompt text.
        pdf_bytes: Raw PDF bytes for the document content block.
        user_text: User message text (chunk context / instructions).

    Returns:
        Tuple of (tool_input_dict, input_tokens, output_tokens).

    Raises:
        ValueError: If response contains no tool use block.
    """
    response = client.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "document": {
                            "name": "statement",
                            "format": "pdf",
                            "source": {"bytes": pdf_bytes},
                        }
                    },
                    {"text": user_text},
                ],
            }
        ],
        toolConfig={
            "tools": [{"toolSpec": EXTRACT_TOOL}],
            "toolChoice": {"tool": {"name": "extract_statement_rows"}},
        },
    )

    # Extract tool use result from response.
    content_blocks = response.get("output", {}).get("message", {}).get("content", [])
    for block in content_blocks:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == "extract_statement_rows":
            usage = response.get("usage", {})
            return (
                tool_use["input"],
                usage.get("inputTokens", 0),
                usage.get("outputTokens", 0),
            )

    raise ValueError("Bedrock response did not contain an extract_statement_rows tool use block")


def call_bedrock_with_retry(
    client: Any,
    system_prompt: str,
    pdf_bytes: bytes,
    user_text: str,
) -> tuple[dict[str, Any], int, int]:
    """Call Bedrock with retries for transient server errors.

    Retries up to MAX_RETRIES times with exponential backoff for
    InternalServerException and ServiceUnavailableException. Fails
    immediately on client/validation errors.

    Args:
        client: boto3 bedrock-runtime client.
        system_prompt: System prompt text.
        pdf_bytes: Raw PDF bytes.
        user_text: User message text.

    Returns:
        Tuple of (tool_input_dict, input_tokens, output_tokens).

    Raises:
        Exception: If all retries exhausted or non-retryable error.
    """
    # Transient error codes that warrant a retry.
    retryable = {
        "InternalServerException",
        "ServiceUnavailableException",
    }
    last_error: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            return call_bedrock(client, system_prompt, pdf_bytes, user_text)
        except client.exceptions.ThrottlingException as exc:
            # Throttling is transient — retry.
            last_error = exc
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # boto3 timeout exceptions have .response = None, so
            # guard against that to avoid masking the real error.
            resp = getattr(exc, "response", None) or {}
            error_code = resp.get("Error", {}).get("Code", "")
            if error_code in retryable:
                last_error = exc
            else:
                # Non-retryable — fail immediately.
                raise

        if attempt < MAX_RETRIES:
            delay = BASE_RETRY_DELAY * (2**attempt)
            print(f"  Retrying in {delay:.0f}s (attempt {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(delay)

    raise last_error  # type: ignore[misc]


# -- Post-processing ---------------------------------------------------------


def convert_amount(
    raw: str,
    decimal_sep: str,
    thousands_sep: str,
) -> float | str:
    """Convert a raw monetary string to a float.

    Handles trailing minus signs (126.50-), parenthetical negatives
    ((126.50)), thousands separators, and decimal separators.

    Args:
        raw: Raw string value from the LLM (e.g. "3,848.97", "126.50-").
        decimal_sep: Detected decimal separator ("." or ",").
        thousands_sep: Detected thousands separator.

    Returns:
        Float value, or the original string if conversion fails.
    """
    s = raw.strip()
    if not s:
        return s

    # Guard: if decimal and thousands separators are the same non-empty
    # value, we can't reliably parse — return raw string.
    if decimal_sep and thousands_sep and decimal_sep == thousands_sep:
        return raw

    # Detect negative: trailing minus or parentheses.
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

    # Strip thousands separator.
    if thousands_sep:
        s = s.replace(thousands_sep, "")

    # Normalise decimal separator to ".".
    if decimal_sep == ",":
        s = s.replace(",", ".")

    try:
        value = float(s)
        return -value if negative else value
    except ValueError:
        return raw


def reconstruct_items(
    column_order: list[str],
    raw_rows: list[list[str]],
) -> list[dict[str, Any]]:
    """Reconstruct item dicts from compact array-of-arrays format.

    Maps each flat value array back into a structured dict with
    standard fields (date, number, due_date, reference), monetary
    columns in `total`, and remaining columns in `raw`.

    Args:
        column_order: Column names matching the position of values.
        raw_rows: List of value arrays from the LLM response.

    Returns:
        List of item dicts in the same format as the original schema.
    """
    items: list[dict[str, Any]] = []
    for row in raw_rows:
        item: dict[str, Any] = {
            "date": "",
            "number": "",
            "due_date": "",
            "reference": "",
            "total": {},
            "raw": {},
        }
        for idx, col_name in enumerate(column_order):
            val = row[idx] if idx < len(row) else ""
            if col_name.lower() in STANDARD_FIELDS:
                item[col_name.lower()] = val
            else:
                # Non-standard columns go into total (monetary) or raw.
                # We can't distinguish here, so put all into total and
                # let the caller decide. Post-processing will attempt
                # numeric conversion; those that fail stay as strings.
                item["total"][col_name] = val
        items.append(item)
    return items


def postprocess_items(
    items: list[dict[str, Any]],
    decimal_sep: str,
    thousands_sep: str,
) -> list[dict[str, Any]]:
    """Convert raw string totals to numeric values.

    Mutates items in place and returns the same list for convenience.

    Args:
        items: List of extracted items with string total values.
        decimal_sep: Detected decimal separator.
        thousands_sep: Detected thousands separator.

    Returns:
        Items with total values converted to floats where possible.
    """
    for item in items:
        if "total" in item and isinstance(item["total"], dict):
            # fmt: off
            item["total"] = {
                k: convert_amount(v, decimal_sep, thousands_sep)
                for k, v in item["total"].items()
            }
            # fmt: on
    return items


# -- Single-PDF processing ---------------------------------------------------


def process_pdf(  # pylint: disable=too-many-locals
    pdf_path: Path,
    client: Any,
    system_prompt: str,
) -> dict[str, Any]:
    """Process a single PDF through Sonnet extraction.

    Chunks the PDF, calls Bedrock for each chunk with header
    propagation, merges results, post-processes numeric values,
    and returns the structured output dict.

    Args:
        pdf_path: Path to the input PDF.
        client: boto3 bedrock-runtime client.
        system_prompt: System prompt text.

    Returns:
        Output dict with items, metadata, and cost estimate.

    Raises:
        Exception: If any chunk fails after retries.
    """
    start_time = time.time()

    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    chunks = chunk_pdf(reader)
    chunk_count = len(chunks)

    is_single_chunk = chunk_count == 1

    # ── Process chunk 1 sequentially (provides headers for the rest) ──

    pdf_bytes_0, start_page_0, end_page_0 = chunks[0]
    print(f"  Processing chunk 1/{chunk_count} (pages {start_page_0}-{end_page_0})...")
    user_text_0 = "Extract all line items from this statement."

    result_0, in_tok_0, out_tok_0 = call_bedrock_with_retry(
        client, system_prompt, pdf_bytes_0, user_text_0,
    )
    detected_headers = result_0.get("detected_headers", [])
    column_order = result_0.get("column_order", [])
    date_format = result_0.get("date_format", "")
    decimal_separator = result_0.get("decimal_separator", ".")
    thousands_separator = result_0.get("thousands_separator", "")

    col_order_0 = result_0.get("column_order", column_order)
    all_items = reconstruct_items(col_order_0, result_0.get("items", []))
    total_input_tokens = in_tok_0
    total_output_tokens = out_tok_0

    # ── Process remaining chunks in parallel ────────────────────────

    if chunk_count > 1:
        headers_str = ", ".join(detected_headers)
        col_order_str = json.dumps(column_order)

        def _process_chunk(
            chunk_idx: int,
        ) -> tuple[int, list[dict[str, Any]], int, int, dict[str, str]]:
            """Process a single continuation chunk.

            Returns (chunk_idx, items, input_tokens, output_tokens,
            metadata_warnings).
            """
            c_bytes, c_start, c_end = chunks[chunk_idx]
            print(
                f"  Processing chunk {chunk_idx + 1}/{chunk_count} "
                f"(pages {c_start}-{c_end})..."
            )
            c_user_text = (
                f"This is a continuation of a multi-page statement "
                f"(pages {c_start}-{c_end} of {page_count}).\n"
                f"The first page of this chunk (page {c_start}) "
                f"was also the last page of the previous chunk.\n"
                f"Skip any rows from that page — they have already "
                f"been extracted.\n"
                f"The table headers from page 1 are: "
                f"[{headers_str}]\n"
                f"Use this exact column_order: {col_order_str}\n"
                f"If headers are repeated on these pages, skip the "
                f"header rows.\n"
                f"If headers are NOT present on these pages, use "
                f"the headers above to identify columns.\n"
                f"Extract the data rows only."
            )
            c_result, c_in, c_out = call_bedrock_with_retry(
                client, system_prompt, c_bytes, c_user_text,
            )
            # Check for metadata disagreements.
            warnings: dict[str, str] = {}
            chunk_1_meta = {
                "date_format": date_format,
                "decimal_separator": decimal_separator,
                "thousands_separator": thousands_separator,
            }
            for field, canonical in chunk_1_meta.items():
                chunk_val = c_result.get(field, "")
                if chunk_val and chunk_val != canonical:
                    warnings[field] = chunk_val

            c_col_order = c_result.get("column_order", column_order)
            c_items = reconstruct_items(
                c_col_order, c_result.get("items", [])
            )
            return (chunk_idx, c_items, c_in, c_out, warnings)

        # Dispatch chunks 2+ in parallel, merge in page order.
        chunk_results: dict[int, tuple[list[dict[str, Any]], int, int]] = {}
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHUNKS) as pool:
            futures = {
                pool.submit(_process_chunk, idx): idx
                for idx in range(1, chunk_count)
            }
            # Map field names to chunk 1's canonical values for warnings.
            canonical_meta = {
                "date_format": date_format,
                "decimal_separator": decimal_separator,
                "thousands_separator": thousands_separator,
            }
            for future in as_completed(futures):
                idx, c_items, c_in, c_out, warns = future.result()
                for field, val in warns.items():
                    print(
                        f"  WARNING: chunk {idx + 1} returned "
                        f"{field}='{val}' vs chunk 1's "
                        f"'{canonical_meta[field]}' — using chunk 1's value"
                    )
                chunk_results[idx] = (c_items, c_in, c_out)

        # Merge in chunk order to preserve row ordering.
        for idx in range(1, chunk_count):
            c_items, c_in, c_out = chunk_results[idx]
            all_items.extend(c_items)
            total_input_tokens += c_in
            total_output_tokens += c_out

    # Post-process: convert raw string totals to numeric values.
    all_items = postprocess_items(all_items, decimal_separator, thousands_separator)

    elapsed = time.time() - start_time
    cost = total_input_tokens * COST_PER_INPUT_TOKEN + total_output_tokens * COST_PER_OUTPUT_TOKEN

    return {
        "filename": pdf_path.name,
        "page_count": page_count,
        "chunk_count": chunk_count,
        "detected_headers": detected_headers,
        "date_format": date_format,
        "decimal_separator": decimal_separator,
        "thousands_separator": thousands_separator,
        "items": all_items,
        "item_count": len(all_items),
        "processing_time_seconds": round(elapsed, 1),
        "estimated_cost_usd": round(cost, 4),
    }


# -- Main --------------------------------------------------------------------


def main() -> None:  # pylint: disable=too-many-locals,too-many-statements
    """Run extraction on all PDFs in the input directory."""
    # Validate input directory exists and has PDFs.
    if not INPUT_DIR.exists():
        print(f"ERROR: Input directory not found: {INPUT_DIR}")
        print("Create it and add PDF files to test.")
        sys.exit(1)

    pdf_files = sorted(INPUT_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"ERROR: No PDF files found in {INPUT_DIR}")
        sys.exit(1)

    # Load system prompt.
    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")

    # Create output directory.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Init Bedrock client with the configured AWS profile.
    # Increase read timeout — large/dense PDFs can take minutes for
    # Sonnet to process, exceeding boto3's default 60s read timeout.
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    bedrock_config = BotoConfig(read_timeout=900)
    client = session.client("bedrock-runtime", config=bedrock_config)

    print(f"Found {len(pdf_files)} PDF(s) in {INPUT_DIR}")
    print(f"Model: {MODEL_ID}")
    print(f"Chunk size: {CHUNK_SIZE} pages")
    print()

    def _process_single_pdf(pdf_path: Path) -> dict[str, Any]:
        """Process one PDF and write its output JSON.

        Returns a summary dict for the run summary.
        """
        pdf_start = time.time()
        start_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"[{start_ts}] Processing: {pdf_path.name}")
        try:
            result = process_pdf(pdf_path, client, system_prompt)
            output_path = OUTPUT_DIR / f"{pdf_path.stem}.json"
            output_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            end_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            # fmt: off
            print(
                f"  [{end_ts}] Done: {result['item_count']} items, "
                f"{result['page_count']} pages, "
                f"{result['processing_time_seconds']}s, "
                f"${result['estimated_cost_usd']:.4f}"
            )
            # fmt: on
            return {
                "filename": result["filename"],
                "page_count": result["page_count"],
                "chunk_count": result["chunk_count"],
                "item_count": result["item_count"],
                "processing_time_seconds": result["processing_time_seconds"],
                "estimated_cost_usd": result["estimated_cost_usd"],
                "status": "success",
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            elapsed = round(time.time() - pdf_start, 1)
            end_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"  [{end_ts}] FAILED ({elapsed}s): {exc}")
            try:
                reader = PdfReader(str(pdf_path))
                fail_pages = len(reader.pages)
                fail_chunks = len(chunk_pdf(reader))
            except Exception:  # pylint: disable=broad-exception-caught
                fail_pages = 0
                fail_chunks = 0
            return {
                "filename": pdf_path.name,
                "page_count": fail_pages,
                "chunk_count": fail_chunks,
                "item_count": 0,
                "processing_time_seconds": elapsed,
                "estimated_cost_usd": 0,
                "status": "failed",
                "error": str(exc),
            }

    run_start = time.time()

    # Process all PDFs in parallel (limited concurrency to avoid
    # Bedrock throttling). Results are collected in submission order.
    pdf_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PDFS) as pool:
        futures = [
            pool.submit(_process_single_pdf, pdf_path)
            for pdf_path in pdf_files
        ]
        # Collect in submission order (preserves alphabetical sort).
        for future in futures:
            pdf_results.append(future.result())

    # Write run summary.
    total_time = round(time.time() - run_start, 1)
    total_items = sum(r["item_count"] for r in pdf_results)
    total_cost = sum(r["estimated_cost_usd"] for r in pdf_results)

    summary = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_pdfs": len(pdf_results),
        "total_items": total_items,
        "total_time_seconds": total_time,
        "total_estimated_cost_usd": round(total_cost, 4),
        "pdfs": pdf_results,
    }
    summary_path = OUTPUT_DIR / "run_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print()
    print("=" * 60)
    print(f"Total PDFs: {len(pdf_results)}")
    print(f"Total items: {total_items}")
    print(f"Total time: {total_time}s")
    print(f"Total estimated cost: ${total_cost:.4f}")
    print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
