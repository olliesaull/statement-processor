import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import boto3
from botocore.exceptions import ClientError

from configuration.config import S3_BUCKET_NAME
from flag_outliers import apply_outlier_flags

s3 = boto3.client("s3")
textract = boto3.client("textract")


class TableOnPage(TypedDict):
    page: int
    grid: List[List[str]]

# ---------- Helpers ----------
def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()

def _clean_number_str(s: str) -> str:
    # remove ALL whitespace and commas
    return re.sub(r"\s+", "", (s or "")).replace(",", "")

def _to_number_if_possible(s: str):
    if s is None:
        return ""
    t = _clean_number_str(s)
    if t == "":
        return ""
    try:
        # prefer int if no decimal point
        if "." in t:
            return float(t)
        return int(t)
    except ValueError:
        return s.strip()

def _best_header_row(grid: List[List[str]], candidate_headers: List[str], lookahead: int = 5) -> Tuple[int, List[str]]:
    """
    Pick the header row index by scoring the first `lookahead` rows
    against the set of candidate header labels (case/space-insensitive).
    """
    cand = set(_norm(h) for h in candidate_headers if h)
    if not cand:
        # fallback: first non-empty row
        for idx, row in enumerate(grid):
            if any(c.strip() for c in row):
                return idx, row
        return 0, grid[0] if grid else []
    best_idx, best_score = 0, -1
    for i in range(min(lookahead, len(grid))):
        row = grid[i]
        score = 0
        for cell in row:
            cn = _norm(cell)
            if not cn:
                continue
            # exact or substring overlap helps catch split headers
            if cn in cand or any(c in cn or cn in c for c in cand):
                score += 1
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx, grid[best_idx]

def _list_s3_objects(bucket: str, prefix: str) -> List[str]:
    keys: List[str] = []
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    while True:
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []):
            k = obj["Key"]
            if not k.endswith("/"):
                keys.append(k)
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp.get("NextContinuationToken")
        else:
            break
    return keys

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

def _collect_table_grids_with_pages(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return [{'page': int, 'grid': List[List[str]]}, ...] across all pages."""
    by_id = {b["Id"]: b for b in blocks}
    out: List[Dict[str, Any]] = []

    for tbl in [b for b in blocks if b.get("BlockType") == "TABLE"]:

        # gather cells
        cells = []
        for rel in tbl.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    cb = by_id.get(cid)
                    if cb and cb.get("BlockType") == "CELL":
                        cells.append(cb)
        if not cells:
            continue

        # build grid
        max_r = max(c.get("RowIndex", 1) + c.get("RowSpan", 1) - 1 for c in cells)
        max_c = max(c.get("ColumnIndex", 1) + c.get("ColumnSpan", 1) - 1 for c in cells)
        grid = [["" for _ in range(max_c)] for _ in range(max_r)]

        for c in cells:
            r = c.get("RowIndex", 1) - 1
            col = c.get("ColumnIndex", 1) - 1
            grid[r][col] = _block_text(c, by_id).strip()

        # prune empty rows/cols
        grid = [row for row in grid if any(x.strip() for x in row)]
        if grid:
            keep_cols = [i for i in range(len(grid[0])) if any(row[i].strip() for row in grid)]
            grid = [[row[i] for i in keep_cols] for row in grid]

        if grid:
            out.append({"page": int(tbl.get("Page", 1)), "grid": grid})

    # sort by page order (stable)
    out.sort(key=lambda t: t["page"])
    return out

def _analyze_tables_s3(bucket: str, key: str) -> List[TableOnPage]:
    """
    Use async StartDocumentAnalysis for PDFs/TIFFs.
    Use analyze_document for single-page images.
    Always fetches all pages (no Pages=["1"]!).
    """
    k = key.lower()
    is_pdf_tiff = k.endswith(".pdf") or k.endswith(".tif") or k.endswith(".tiff")

    if is_pdf_tiff:
        start = textract.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["TABLES"],
        )
        job_id = start["JobId"]

        delay = 1.0
        while True:
            status = textract.get_document_analysis(JobId=job_id, MaxResults=1000)
            js = status.get("JobStatus")
            if js in ("SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"):
                break
            time.sleep(delay)
            delay = min(delay * 1.7, 5.0)

        if status.get("JobStatus") != "SUCCEEDED":
            raise RuntimeError(f"Textract analysis failed for {key}: {status.get('JobStatus')}")

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

# --- helpers for skipping opening/carried-forward rows ---
def _looks_money(s: str) -> bool:
    if s is None:
        return False
    t = re.sub(r"\s+", "", s).replace(",", "")
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t))

def _is_forward_label(text: str) -> bool:
    """
    Matches variations like:
      B/F, B/FWD, BFWD, BALANCE B/F, BROUGHT FORWARD, C/FWD, CARRIED FORWARD,
      OPENING BALANCE, PREVIOUS BALANCE, etc.
    """
    t = re.sub(r"[^a-z0-9 ]+", "", (_norm(text) or ""))  # strip punctuation (/, #, .) and lowercase
    if not t:
        return False
    keywords = (
        "brought forward",
        "carried forward",
        "opening balance",
        "opening bal",
        "previous balance",
        "balance forward",
        "balance bf",
        "balance b f",
        "bal bf",
        "bal b f",
    )
    short_forms = {"bf", "b f", "bfwd", "b fwd", "cf", "c f", "cfwd", "c fwd"}
    return t in short_forms or any(k in t for k in keywords)

def _row_is_opening_or_carried_forward(raw_row: List[str], mapped_item: Dict[str, Any]) -> bool:
    """
    Heuristics:
      - Contains a forward-like label in document_type / description_details / any raw cell
      - Very sparse row (<= 3 non-empty cells) AND only one money value present
        AND no useful identifiers (doc/customer/supplier refs)
    """
    # 1) Label-based detection
    if _is_forward_label(mapped_item.get("document_type", "")) or _is_forward_label(mapped_item.get("description_details", "")):
        return True
    if isinstance(mapped_item.get("raw"), dict):
        if any(_is_forward_label(v) for v in mapped_item["raw"].values() if v):
            return True

    # 2) Sparsity + money pattern
    non_empty = sum(1 for c in raw_row if (c or "").strip())
    money_count = sum(1 for c in raw_row if _looks_money(c))
    ids_empty = all(not (mapped_item.get(k) or "").strip() for k in ("supplier_reference", "customer_reference"))
    doc_like_empty = all(not (mapped_item.get(k) or "").strip() for k in ("document_type", "description_details"))
    if non_empty <= 3 and money_count <= 1 and ids_empty and doc_like_empty:
        return True

    return False

def select_relevant_tables_per_page(tables_with_pages: List[Dict[str, Any]], candidates: List[str]) -> List[Dict[str, Any]]:
    """
    From [{'page': p, 'grid': G}, ...], choose one best table per page.
    Returns a list of the same shape, sorted by page.
    """
    if not tables_with_pages:
        return []

    cand_set = {c.strip().lower() for c in candidates if c}
    date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")

    # group by page
    by_page: Dict[int, List[List[List[str]]]] = {}
    for t in tables_with_pages:
        by_page.setdefault(int(t["page"]), []).append(t["grid"])

    selected: List[Dict[str, Any]] = []

    for page, grids in sorted(by_page.items()):
        best_grid = None
        best_score = -1.0

        for grid in grids:
            if not grid:
                continue

            hdr_idx, header_row = _best_header_row(grid, list(cand_set))
            data_rows = grid[hdr_idx + 1 :]

            header_norm = [_norm(h) for h in header_row]
            header_hits = sum(1 for h in header_norm if h in cand_set or any(c in h or h in c for c in cand_set))

            date_hits = 0
            for r in data_rows[:10]:
                if r and date_re.match((r[0] or "").strip()):
                    date_hits += 1

            size_bonus = len(grid) * (len(grid[0]) if grid and grid[0] else 0)
            score = header_hits * 10 + date_hits * 2 + size_bonus * 0.001

            if score > best_score:
                best_score = score
                best_grid = grid

        if best_grid is None:
            # fallback: largest table on page
            best_grid = max(grids, key=lambda g: (len(g), len(g[0]) if g else 0))

        selected.append({"page": page, "grid": best_grid})

    return selected

# =========================================================
# 1) get_tables
# =========================================================
def get_tables(bucket: str = S3_BUCKET_NAME, prefix: str = "statements/", include_keys: Optional[List[str]] = None) -> Dict[str, List[TableOnPage]]:
    """
    Return {s3_key: [{"page": int, "grid": [[...], ...]}, ...]} for statements under 'prefix'.
    If include_keys is provided, only those keys are processed.
    Each table_grid is a 2D list of strings.
    """
    if include_keys:
        keys = [f"{prefix}{k}" for k in include_keys]
    else:
        keys = _list_s3_objects(bucket, prefix)

    result: Dict[str, List[TableOnPage]] = {}
    for key in keys:
        try:
            tables_wp = _analyze_tables_s3(bucket, key)
            result[key] = tables_wp
        except ClientError as ce:
            print(f"AWS error for {key}: {ce}")
        except Exception as e:
            print(f"Error processing {key}: {e}")
    return result

# =========================================================
# 2) table_to_json
# =========================================================
def table_to_json(key: str, tables: List[Any], config_dir: str = "./statement_configs") -> Dict[str, Any]:
    # --- load configs (unchanged) ---
    stem = Path(key).stem
    cfg_path = Path(config_dir) / f"{stem}.json"
    canon_path = Path(config_dir) / "canonical_schema.json"

    with open(cfg_path, "r", encoding="utf-8") as f:
        map_cfg: Dict[str, Any] = json.load(f)
    try:
        with open(canon_path, "r", encoding="utf-8") as f:
            _canonical_schema = json.load(f)
    except FileNotFoundError:
        _canonical_schema = None

    out: Dict[str, Any] = {
        "statement_meta": deepcopy(map_cfg.get("statement_meta", {})),
        "statement_items": [],
    }
    out["statement_meta"]["source_filename"] = Path(key).name

    tmpl_list = map_cfg.get("statement_items", [])
    if not tmpl_list:
        return out
    row_template: Dict[str, Any] = deepcopy(tmpl_list[0])

    # ----- build mapping config (unchanged) -----
    simple_map: Dict[str, str] = {}
    raw_map: Dict[str, str] = {}
    date_value_header = None
    date_format = None

    for field, value in row_template.items():
        if field == "transaction_date" and isinstance(value, dict):
            date_value_header = value.get("value")
            date_format = value.get("format")
        elif field == "raw" and isinstance(value, dict):
            raw_map = value
        elif isinstance(value, str) and value.strip():
            simple_map[field] = value

    candidates = list(simple_map.values())
    if date_value_header:
        candidates.append(date_value_header)
    candidates.extend(list(raw_map.keys()))
    candidates.extend([v for v in raw_map.values() if v])

    # ----- normalize input shape to [{'page','grid'}, ...] -----
    if tables and isinstance(tables[0], list):
        # came from the old _collect_table_grids; assume page=1
        tables_with_pages = [{"page": 1, "grid": g} for g in tables]
    else:
        tables_with_pages = tables or []

    # ----- select ONE table per page -----
    selected = select_relevant_tables_per_page(tables_with_pages, candidates=candidates)

    # ----- helpers -----
    def build_col_index(header_row: List[str]) -> Dict[str, int]:
        col_index: Dict[str, int] = {}
        for i, h in enumerate(header_row):
            hn = _norm(h)
            if hn and hn not in col_index:
                col_index[hn] = i
        return col_index

    def get_by_header(row: List[str], col_index: Dict[str, int], header_label: str) -> str:
        if not header_label:
            return ""
        idx = col_index.get(_norm(header_label))
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    # ----- parse selected tables (page order) -----
    for entry in selected:
        grid = entry["grid"]
        hdr_idx, header_row = _best_header_row(grid, candidates)
        data_rows = grid[hdr_idx + 1 :]

        col_index = build_col_index(header_row)

        for r in data_rows:
            if not any((c or "").strip() for c in r):
                continue

            item = deepcopy(row_template)

            # transaction_date
            if isinstance(item.get("transaction_date"), dict):
                dt_val = get_by_header(r, col_index, date_value_header or "")
                item["transaction_date"]["value"] = dt_val
                item["transaction_date"]["format"] = date_format or item["transaction_date"].get("format", "")

            # simple fields + numeric coercion
            for field, header_name in simple_map.items():
                cell = get_by_header(r, col_index, header_name)
                if field in {"debit", "credit", "invoice_balance", "balance"}:
                    item[field] = _to_number_if_possible(cell)
                else:
                    item[field] = cell

            # raw object (auto-fill when mapping empty)
            if isinstance(item.get("raw"), dict):
                raw_obj = {}
                for raw_key, raw_src_header in raw_map.items():
                    chosen_header = raw_src_header or raw_key
                    raw_obj[raw_key] = get_by_header(r, col_index, chosen_header)
                item["raw"] = raw_obj

            if _row_is_opening_or_carried_forward(r, item):
                continue

            # skip header-like rows / sparse carried-forward (your existing checks)
            dt_empty = not item.get("transaction_date", {}).get("value")
            mapped_values = [
                item.get("document_type", ""),
                item.get("description_details", ""),
                item.get("debit", ""),
                item.get("credit", ""),
                item.get("invoice_balance", ""),
                item.get("balance", ""),
                item.get("supplier_reference", ""),
                item.get("customer_reference", ""),
            ]
            if dt_empty and all(v in ("", None) for v in mapped_values):
                continue

            # Optional: if you kept the carried-forward skipper, call it here:
            # if _row_is_opening_or_carried_forward(r, item): continue

            out["statement_items"].append(item)
    return out

# ---------------- Example ----------------
if __name__ == "__main__":
    tables_by_key = get_tables(include_keys=["Bill Riley Z91.PDF", "ARSTMT11 (54).pdf"])

    for key, tables in tables_by_key.items():
        print(f"\n=== {key} ===")
        statement_json = table_to_json(key, tables)

        # Get just the filename without directories
        filename = Path(key).stem + ".json"
        out_path = f"./structured_statements/{filename}"

        # Ensure output dir exists
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(statement_json, f, ensure_ascii=False, indent=2)

        statement_json, summary = apply_outlier_flags(statement_json, remove=False, one_based_index=True)

        print(json.dumps(summary, indent=2))

        # Optionally save the annotated JSON as-is, or remove flagged rows:
        # statement_json, _ = apply_outlier_flags(statement_json, remove=True)
