import json
import time
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from configuration.config import S3_BUCKET_NAME

s3 = boto3.client("s3")
textract = boto3.client("textract")

OUTPUT_DIR = Path("./statement_configs")

ALL_HEADINGS = set()

# ----------------- Helpers -----------------
def block_text(block: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> str:
    """Collect visible text from a block (WORDS, LINES, checkboxes)."""
    out = []
    for rel in block.get("Relationships", []):
        if rel["Type"] == "CHILD":
            for cid in rel["Ids"]:
                ch = by_id.get(cid)
                if not ch:
                    continue
                bt = ch.get("BlockType")
                if bt in ("WORD", "LINE"):
                    out.append(ch.get("Text", ""))
                elif bt == "SELECTION_ELEMENT":
                    out.append("[x]" if ch.get("SelectionStatus") == "SELECTED" else "[ ]")
    return " ".join(t for t in out if t).strip()

def _collect_tables_first_row(blocks: List[Dict[str, Any]]) -> List[List[str]]:
    """
    From a Textract 'Blocks' list, return a list of headers for each table on page 1.
    Each entry is the list of header cell texts (RowIndex == 1), ordered by ColumnIndex.
    """
    by_id = {b["Id"]: b for b in blocks}
    by_type = defaultdict(list)
    for b in blocks:
        by_type[b.get("BlockType", "")].append(b)

    headers_per_table: List[List[str]] = []

    for table in by_type.get("TABLE", []):
        # Only consider page 1 (Textract includes Page on blocks)
        if str(table.get("Page", "1")) != "1":
            continue

        # Gather CELL blocks
        cells = []
        for rel in table.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    cb = by_id.get(cid)
                    if cb and cb.get("BlockType") == "CELL":
                        cells.append(cb)

        # Pick header row: RowIndex == 1
        header_cells = [c for c in cells if c.get("RowIndex", 0) == 1]
        header_cells.sort(key=lambda c: c.get("ColumnIndex", 0))

        headers = [block_text(c, by_id) for c in header_cells]
        if any(h.strip() for h in headers):
            headers_per_table.append(headers)

    return headers_per_table

def _normalize_heading(h: str) -> str:
    # Trim and collapse whitespace
    return " ".join((h.lower() or "").split())

# ----------------- Textract drivers -----------------
def is_pdf_or_tiff(key: str) -> bool:
    k = key.lower()
    return k.endswith(".pdf") or k.endswith(".tif") or k.endswith(".tiff")

def analyze_first_page_tables_s3(bucket: str, key: str) -> List[List[str]]:
    """
    Returns a list of table header rows (list of strings) found on page 1 of the document.
    Uses async StartDocumentAnalysis with Pages=['1'] for PDF/TIFF.
    Falls back to sync analyze_document for image files (e.g., PNG/JPG).
    """
    if is_pdf_or_tiff(key):
        # ---- Async for PDFs/TIFFs; limit to page 1 for speed/cost ----
        start = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        job_id = start["JobId"]

        delay = 1.0
        status = None
        while True:
            status = textract.get_document_analysis(JobId=job_id, MaxResults=1000)
            js = status.get("JobStatus")
            if js in ("SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"):
                break
            time.sleep(delay)
            delay = min(delay * 1.7, 5.0)

        if status.get("JobStatus") != "SUCCEEDED":
            raise RuntimeError(f"Textract analysis failed for {key}: {status.get('JobStatus')}")

        # Gather all pages (pagination)
        blocks = list(status.get("Blocks", []))
        next_token = status.get("NextToken")
        while next_token:
            page = textract.get_document_analysis(JobId=job_id, NextToken=next_token, MaxResults=1000)
            blocks.extend(page.get("Blocks", []))
            next_token = page.get("NextToken")

        return _collect_tables_first_row(blocks)

    else:
        # ---- Sync for images (JPG/PNG, etc.) ----
        resp = textract.analyze_document(
            Document={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        blocks = resp.get("Blocks", [])
        return _collect_tables_first_row(blocks)

# ----------------- S3 iteration + JSON writing -----------------
def list_s3_objects(bucket: str, prefix: Optional[str] = None) -> List[str]:
    keys = []
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    while True:
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp.get("NextContinuationToken")
        else:
            break
    return keys

def save_headings_json_for_key(key: str, headers_list: List[List[str]]) -> Optional[Path]:
    """
    Merge/dedupe all headings from page 1 tables and save as JSON where each heading is both key and value.
    File path: ./statement_configs/<basename>.pdf (as requested).
    Returns the path if written, else None.
    """
    # Merge/dedupe headings across tables
    unique_headings = []
    seen = set()
    for headers in headers_list:
        for h in headers:
            n = _normalize_heading(h)
            if n and n not in seen:
                seen.add(n)
                unique_headings.append(n)
                ALL_HEADINGS.add(n)

    if not unique_headings:
        return None

    # Build mapping {heading: heading}
    mapping = {h: h for h in unique_headings}

    # Ensure output dir
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    base_name = Path(key).stem  # file name without extension
    out_name = f"{base_name}.json"
    out_path = OUTPUT_DIR / out_name

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)

    return out_path

def process_bucket_and_write_json(bucket: str, prefix: Optional[str] = None) -> None:
    keys = list_s3_objects(bucket, prefix)
    if not keys:
        print(f"No objects found in s3://{bucket}/{prefix or ''}")
        return

    for key in keys:
        # Skip obvious non-docs or folders
        if key.endswith("/") or key.lower().endswith((".json", ".txt", ".csv")):
            continue

        print(f"\n=== File: s3://{bucket}/{key} ===")
        try:
            headers_list = analyze_first_page_tables_s3(bucket, key)
            if not headers_list:
                print("No tables found on page 1. (No JSON written)")
                continue

            out_path = save_headings_json_for_key(key, headers_list)
            if out_path:
                print(f"Wrote headings JSON to: {out_path}")
            else:
                print("No non-empty headings found. (No JSON written)")

        except ClientError as ce:
            print(f"AWS error for {key}: {ce}")
        except Exception as e:
            print(f"Error processing {key}: {e}")

# ----------------- Example usage -----------------
if __name__ == "__main__":
    PREFIX = "statements/"
    process_bucket_and_write_json(S3_BUCKET_NAME, PREFIX)
    with open(f"{OUTPUT_DIR}/all_headings.txt", "w", encoding="utf-8") as f:
        f.write(",".join(str(item) for item in ALL_HEADINGS))
