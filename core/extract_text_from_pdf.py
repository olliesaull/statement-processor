import re
import time
from io import BytesIO
from typing import Any, Dict, List, Optional

import boto3
from mypy_boto3_textract import TextractClient

import pdfplumber
from configuration.config import AWS_PROFILE, AWS_REGION

session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
textract: TextractClient = session.client("textract")

def normalize_text(text: str) -> str:
    """
    Make line-based text consistent:
    - strip trailing/leading spaces per line
    - collapse multiple spaces to one
    - drop empty lines
    """
    lines = []
    for raw in text.splitlines():
        # collapse internal whitespace
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)

# ---------- Textract text ----------

def _collect_lines_from_blocks(blocks: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for b in blocks:
        if b.get("BlockType") == "LINE" and "Text" in b:
            lines.append(b["Text"])
    return "\n".join(lines)

def extract_text_from_textract_s3(bucket: str, key: str, pdf_pages: Optional[int]) -> str:
    """
    If pdf_pages is None, we don't know page count (could be image or not-a-PDF) -> use sync detect.
    If pdf_pages == 1 -> use sync detect (fast).
    If pdf_pages > 1 -> use async job (Textract requirement for multi-page PDFs).
    """
    doc_loc = {"S3Object": {"Bucket": bucket, "Name": key}}

    # Case 1: unknown or single-page -> synchronous detect
    if not pdf_pages or pdf_pages == 1:
        resp = textract.detect_document_text(Document=doc_loc)  # type: ignore
        return _collect_lines_from_blocks(resp.get("Blocks", []))

    # Case 2: multi-page PDF -> asynchronous text detection
    start = textract.start_document_text_detection(DocumentLocation=doc_loc)  # type: ignore
    job_id = start["JobId"]

    # Poll with simple backoff
    delay = 1.0
    while True:
        status = textract.get_document_text_detection(JobId=job_id, MaxResults=1000)  # type: ignore
        job_status = status["JobStatus"]
        if job_status in ("SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"):
            break
        time.sleep(delay)
        delay = min(delay * 1.7, 5.0)

    if job_status != "SUCCEEDED":
        raise RuntimeError(f"Textract async job did not succeed (status={job_status}) for {key}")

    # Gather all pages (pagination over NextToken)
    blocks: List[Dict[str, Any]] = []
    next_token = status.get("NextToken")
    blocks.extend(status.get("Blocks", []))
    while next_token:
        page = textract.get_document_text_detection(JobId=job_id, NextToken=next_token, MaxResults=1000)  # type: ignore
        blocks.extend(page.get("Blocks", []))
        next_token = page.get("NextToken")

    return _collect_lines_from_blocks(blocks)

# ---------- PDF text (pdfplumber) ----------

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """
    Extract text from PDF bytes using pdfplumber.
    Returns a single big string (pages joined by newlines).
    """
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)

def count_pdf_pages(pdf_bytes: bytes) -> Optional[int]:
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None  # not a PDF or corrupted
