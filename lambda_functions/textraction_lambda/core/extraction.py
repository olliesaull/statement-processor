"""
This module converts the (fairly low-level) output of AWS Textract's
`GetDocumentAnalysis` API into a simple, table-shaped structure that the rest of
this Lambda can work with.

## What Textract returns (high-level)

Textract returns a JSON document containing a large `Blocks` array. Each "block"
is a node in a document graph and has a `BlockType` plus an `Id` used to link it
to other blocks via `Relationships`.

For table extraction, the important block types are:

- `TABLE`: represents a detected table on a page.
- `CELL`: represents a single table cell with `RowIndex` / `ColumnIndex`
  (1-based).
- `WORD`: a token of text, with a `Text` field.
- `SELECTION_ELEMENT`: checkboxes, with `SelectionStatus == "SELECTED"`.

The structure is a graph:

`TABLE` --(Relationships: CHILD ids)--> `CELL`
`CELL`  --(Relationships: CHILD ids)--> `WORD` / `SELECTION_ELEMENT`

`get_document_analysis` can be paginated; responses may include `NextToken`.
To retrieve all blocks for a job, you must repeatedly call the API until
`NextToken` is no longer returned.

## What we convert it into

Downstream code (see `core/transform.py`) wants tables in a very simple shape:

    TableOnPage = {"page": int, "grid": List[List[str]]}

Where `grid[row][col]` is the concatenated text for a single cell.

## Pipeline overview

- `analyze_tables_job`: paginate Textract results and collect all `Blocks`.
- `_extract_tables_from_blocks`: for each `TABLE`, rebuild a 2D cell grid and sanitize it.
- `_extract_text_for_block`: turn a `CELL` + its children into a single string.
- `_sanitize_grid`: remove empty rows/cols and duplicate columns.
- `get_tables_for_job`: wrapper that returns `{job_id: tables}` and logs failures.

The key idea: Textract gives us a graph of blocks; this module traverses that
graph and reconstructs a clean 2D grid per table so later stages can map headers
to fields and emit structured statement JSON.
"""

from typing import Any, TypedDict, cast

from config import textract_client
from logger import logger


class TableOnPage(TypedDict):
    """
    A single extracted table, annotated with the page number it was found on.

    The `grid` is a 2D list of strings where:
    - Each inner list is a row
    - Each string is the concatenated cell text for that row/column position
    - The grid has already been "sanitized" (empty rows/cols removed; duplicate columns removed)
    """

    page: int
    grid: list[list[str]]


def _sanitize_grid(grid: list[list[str]]) -> list[list[str]]:
    """
    Clean a raw rectangular table grid built from Textract `CELL` coordinates.

    Textract table reconstruction can produce:
    - Entirely empty rows (padding / detection noise)
    - Entirely empty columns
    - Duplicate columns (same header + identical values) due to detection quirks

    This function:
    1) Removes fully-empty rows
    2) Removes fully-empty columns
    3) Deduplicates identical columns by comparing (header, column_values) signatures

    Returns a smaller grid that is easier to map into structured fields later.
    """
    # 1) Remove fully-empty rows (e.g., padding/noise from table detection).
    meaningful_rows = [row for row in grid if any(cell.strip() for cell in row)]
    if not meaningful_rows:
        return []

    # 2) Remove fully-empty columns across the remaining rows.
    keep_cols = [idx for idx in range(len(meaningful_rows[0])) if any(row[idx].strip() for row in meaningful_rows)]
    if not keep_cols:
        return []

    cleaned = [[row[idx] for idx in keep_cols] for row in meaningful_rows]
    if not cleaned or not cleaned[0]:
        return cleaned

    # 3) Deduplicate columns by signature (header + all values below it).
    seen_signatures = set()
    keep_dedup: list[int] = []
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


def _extract_text_for_block(block_map: dict[str, dict[str, Any]], block: dict[str, Any]) -> str:
    """
    Convert a Textract `CELL` block into a single text string.

    A `CELL` does not directly contain human-readable text; instead it references CHILD blocks, typically:
    - `WORD` blocks (with `Text`)
    - `SELECTION_ELEMENT` blocks (checkboxes), represented here as "X" when selected

    This function walks the child ids and concatenates tokens into the cell's text.
    """
    texts: list[str] = []
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


def _extract_tables_from_blocks(blocks: list[dict[str, Any]]) -> list[TableOnPage]:  # pylint: disable=too-many-locals
    """
    Reconstruct table grids from the flat `Blocks` list returned by Textract.

    Textract encodes tables as a graph of blocks:
    - `TABLE` blocks reference their child `CELL` blocks via Relationships (Type="CHILD")
    - `CELL` blocks specify `RowIndex` / `ColumnIndex` (1-based) and reference their child `WORD`/`SELECTION_ELEMENT` blocks for text content

    This function:
    - Indexes blocks by id (`block_map`) so relationship ids can be resolved quickly
    - For each `TABLE`, collects all associated `CELL`s
    - Allocates a grid sized to the max RowIndex/ColumnIndex
    - Fills grid coordinates with cell text via `_extract_text_for_block`
    - Cleans the grid via `_sanitize_grid`

    Returns a list of `TableOnPage` entries sorted by page.
    """
    # Build an index for quick lookup
    block_map: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_id = block.get("Id")
        if isinstance(block_id, str):
            block_map[block_id] = block
    tables: list[TableOnPage] = []

    # We only care about `TABLE` blocks; everything else is reached via relationships.
    for block in blocks:
        if not isinstance(block, dict) or block.get("BlockType") != "TABLE":
            continue
        page = int(block.get("Page") or 1)
        cell_ids: list[str] = []
        for rel in block.get("Relationships", []):
            if rel.get("Type") == "CHILD":
                cell_ids.extend(rel.get("Ids", []))

        # Resolve the table's child ids into actual `CELL` blocks.
        cells = [block_map.get(cid) for cid in cell_ids if cid in block_map]
        if not cells:
            continue

        # Textract uses 1-based row/column indices; size the grid from the max indices seen.
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
            # Convert each cell's WORD/SELECTION children into a single string value.
            text = _extract_text_for_block(block_map, cell)
            grid[r_idx][c_idx] = text

        sanitized = _sanitize_grid(grid)
        if sanitized:
            tables.append({"page": page, "grid": sanitized})

    tables.sort(key=lambda t: t["page"])
    return tables


def analyze_tables_job(job_id: str) -> list[TableOnPage]:
    """
    Fetch and parse Textract table results for a completed job id.

    `get_document_analysis` is paginated. We call it repeatedly, following `NextToken`,
    until all blocks are retrieved, then extract tables by reconstructing grids from the block graph.
    """
    blocks: list[dict[str, Any]] = []
    next_token: str | None = None
    while True:
        params: dict[str, Any] = {"JobId": job_id}
        if next_token:
            params["NextToken"] = next_token
        # Textract paginates; keep calling until `NextToken` is absent.
        resp = textract_client.get_document_analysis(**params)
        raw_blocks = cast(list[dict[str, Any]], resp.get("Blocks", []))
        blocks.extend(raw_blocks)
        next_token = resp.get("NextToken")
        if not next_token:
            break

    # Convert the full block graph into a per-table grid representation.
    return _extract_tables_from_blocks(blocks)


def get_tables_for_job(job_id: str) -> dict[str, list[TableOnPage]]:
    """
    Convenience wrapper used by the main workflow (`run_textraction`).

    Returns a mapping of `{job_id: tables}` so callers can associate tables with
    the job that produced them. Any exceptions are logged and swallowed, because
    downstream steps may choose to proceed (or fail) based on missing table data.
    """
    result: dict[str, list[TableOnPage]] = {}
    try:
        result[job_id] = analyze_tables_job(job_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Failures are logged and swallowed so the caller can decide how to handle missing tables.
        logger.exception("Textract result fetch failed", job_id=job_id, error=str(exc), exc_info=True)
    return result
