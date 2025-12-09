from typing import Any, Dict, List, TypedDict, Optional

from config import logger, textract_client


class TableOnPage(TypedDict):
    page: int
    grid: List[List[str]]


def _sanitize_grid(grid: List[List[str]]) -> List[List[str]]:
    meaningful_rows = [row for row in grid if any(cell.strip() for cell in row)]
    if not meaningful_rows:
        return []

    keep_cols = [
        idx
        for idx in range(len(meaningful_rows[0]))
        if any(row[idx].strip() for row in meaningful_rows)
    ]
    if not keep_cols:
        return []

    cleaned = [[row[idx] for idx in keep_cols] for row in meaningful_rows]
    if not cleaned or not cleaned[0]:
        return cleaned

    seen_signatures = set()
    keep_dedup: List[int] = []
    for col_idx in range(len(cleaned[0])):
        header = (cleaned[0][col_idx] or "").strip().lower()
        column_values = tuple((row[col_idx] or "").strip() for row in cleaned[1:])
        signature = (header, column_values)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        keep_dedup.append(col_idx)

    if len(keep_dedup) == len(cleaned[0]):
        return cleaned

    return [[row[idx] for idx in keep_dedup] for row in cleaned]


def _extract_text_for_block(block_map: Dict[str, Dict[str, Any]], block: Dict[str, Any]) -> str:
    """Concatenate text for WORD and SELECTION blocks under a CELL."""
    texts: List[str] = []
    for rel in block.get("Relationships", []):
        if rel.get("Type") != "CHILD":
            continue
        for cid in rel.get("Ids", []):
            child = block_map.get(cid, {})
            if child.get("BlockType") == "WORD":
                txt = (child.get("Text") or "").strip()
                if txt:
                    texts.append(txt)
            elif child.get("BlockType") == "SELECTION_ELEMENT" and child.get("SelectionStatus") == "SELECTED":
                texts.append("X")
    return " ".join(texts)


def _extract_tables_from_blocks(blocks: List[Dict[str, Any]]) -> List[TableOnPage]:
    block_map = {b.get("Id"): b for b in blocks if isinstance(b, dict)}
    tables: List[TableOnPage] = []

    for block in blocks:
        if not isinstance(block, dict) or block.get("BlockType") != "TABLE":
            continue
        page = int(block.get("Page") or 1)
        cell_ids: List[str] = []
        for rel in block.get("Relationships", []):
            if rel.get("Type") == "CHILD":
                cell_ids.extend(rel.get("Ids", []))

        cells = [block_map.get(cid) for cid in cell_ids if cid in block_map]
        if not cells:
            continue

        max_row = max(int(c.get("RowIndex") or 0) for c in cells if isinstance(c, dict))
        max_col = max(int(c.get("ColumnIndex") or 0) for c in cells if isinstance(c, dict))
        grid = [["" for _ in range(max_col)] for _ in range(max_row)]

        for cell in cells:
            if not isinstance(cell, dict):
                continue
            r_idx = max(int(cell.get("RowIndex") or 1) - 1, 0)
            c_idx = max(int(cell.get("ColumnIndex") or 1) - 1, 0)
            if r_idx >= max_row or c_idx >= max_col:
                continue
            text = _extract_text_for_block(block_map, cell)
            grid[r_idx][c_idx] = text

        sanitized = _sanitize_grid(grid)
        if sanitized:
            tables.append({"page": page, "grid": sanitized})

    tables.sort(key=lambda t: t["page"])
    return tables


def analyze_tables_job(job_id: str) -> List[TableOnPage]:
    """Fetch completed Textract job results and convert tables to grids."""
    blocks: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"JobId": job_id}
        if next_token:
            params["NextToken"] = next_token
        resp = textract_client.get_document_analysis(**params)
        blocks.extend(resp.get("Blocks", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    return _extract_tables_from_blocks(blocks)


def get_tables_for_job(job_id: str) -> Dict[str, List[TableOnPage]]:
    result: Dict[str, List[TableOnPage]] = {}
    try:
        result[job_id] = analyze_tables_job(job_id)
    except Exception as exc:
        logger.exception("Textract result fetch failed", job_id=job_id, error=str(exc), exc_info=True)
    return result
