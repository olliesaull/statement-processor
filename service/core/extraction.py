import time
from typing import Any, Dict, List, TypedDict

import boto3
from botocore.exceptions import ClientError

# Reuse clients across calls
textract = boto3.client("textract")


class TableOnPage(TypedDict):
    """Simple table representation extracted from Textract for a given page."""
    page: int
    grid: List[List[str]]

def _block_text(block: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> str:
    out: List[str] = []
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

def _collect_table_grids_with_pages(blocks: List[Dict[str, Any]]) -> List[TableOnPage]:
    by_id = {b["Id"]: b for b in blocks}
    out: List[TableOnPage] = []
    for tbl in [b for b in blocks if b.get("BlockType") == "TABLE"]:
        cells = []
        for rel in tbl.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    cb = by_id.get(cid)
                    if cb and cb.get("BlockType") == "CELL":
                        cells.append(cb)
        if not cells:
            continue
        max_r = max(c.get("RowIndex", 1) + c.get("RowSpan", 1) - 1 for c in cells)
        max_c = max(c.get("ColumnIndex", 1) + c.get("ColumnSpan", 1) - 1 for c in cells)
        grid = [["" for _ in range(max_c)] for _ in range(max_r)]
        for c in cells:
            r = c.get("RowIndex", 1) - 1
            col = c.get("ColumnIndex", 1) - 1
            grid[r][col] = _block_text(c, by_id).strip()
        grid = [row for row in grid if any(x.strip() for x in row)]
        if grid:
            keep_cols = [i for i in range(len(grid[0])) if any(row[i].strip() for row in grid)]
            grid = [[row[i] for i in keep_cols] for row in grid]
        if grid:
            out.append({"page": int(tbl.get("Page", 1)), "grid": grid})
    out.sort(key=lambda t: t["page"])
    return out

def analyze_tables_s3(bucket: str, key: str) -> List[TableOnPage]:
    """Run Textract TABLES on an S3 object and return tables per page."""
    k = key.lower()
    is_pdf_tiff = k.endswith((".pdf", ".tif", ".tiff"))
    if is_pdf_tiff:
        start = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        job_id = start["JobId"]
        delay, waited = 1.0, 0.0
        while True:
            status = textract.get_document_analysis(JobId=job_id, MaxResults=1000)
            js = status.get("JobStatus")
            if js in ("SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"):
                break
            time.sleep(delay)
            waited += delay
            delay = min(delay * 1.7, 5.0)
            if waited > 180:
                raise TimeoutError(f"Textract timed out for {key}")
        if status.get("JobStatus") != "SUCCEEDED":
            raise RuntimeError(f"Textract failed for {key}: {status.get('JobStatus')}")
        blocks = list(status.get("Blocks", []))
        nt = status.get("NextToken")
        while nt:
            page = textract.get_document_analysis(JobId=job_id, NextToken=nt, MaxResults=1000)
            blocks.extend(page.get("Blocks", []))
            nt = page.get("NextToken")
    else:
        resp = textract.analyze_document(
            Document={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        blocks = resp.get("Blocks", [])
    return _collect_table_grids_with_pages(blocks)

def get_tables(bucket: str, key: str) -> Dict[str, List[TableOnPage]]:
    """Convenience wrapper returning a mapping of key -> extracted tables."""
    result: Dict[str, List[TableOnPage]] = {}
    try:
        result[key] = analyze_tables_s3(bucket, key)
    except ClientError as ce:
        print(f"AWS error for {key}: {ce}")
    except Exception as e:
        print(f"Error processing {key}: {e}")
    return result
