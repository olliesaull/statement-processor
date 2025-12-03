from typing import Dict, List, TypedDict

from textractor import Textractor
from textractor.entities.table import Table

from config import AWS_PROFILE, AWS_REGION, logger


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


def _table_to_grid(table: Table) -> List[List[str]]:
    row_count = table.row_count or 0
    col_count = table.column_count or 0

    if not row_count or not col_count:
        return []

    grid = [["" for _ in range(col_count)] for _ in range(row_count)]

    for cell in table.table_cells:
        row_idx = max(int(cell.row_index or 1) - 1, 0)
        col_idx = max(int(cell.col_index or 1) - 1, 0)
        if row_idx >= row_count or col_idx >= col_count:
            continue
        cell_text = (cell.text or "").strip()
        if cell_text:
            grid[row_idx][col_idx] = cell_text
        elif not grid[row_idx][col_idx]:
            grid[row_idx][col_idx] = ""

    return _sanitize_grid(grid)


def analyze_tables_job(job_id: str) -> List[TableOnPage]:
    textractor = Textractor(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    document = textractor.get_document_analysis(job_id)

    tables: List[TableOnPage] = []
    for index, page in enumerate(document.pages, start=1):
        page_number = page.page_num if isinstance(page.page_num, int) and page.page_num > 0 else index
        for table in page.tables:
            grid = _table_to_grid(table)
            if grid:
                tables.append({"page": int(page_number), "grid": grid})

    tables.sort(key=lambda t: t["page"])
    return tables


def get_tables_for_job(job_id: str) -> Dict[str, List[TableOnPage]]:
    result: Dict[str, List[TableOnPage]] = {}
    try:
        result[job_id] = analyze_tables_job(job_id)
    except Exception as exc:
        logger.exception("Textract result fetch failed", job_id=job_id, error=str(exc), exc_info=True)
    return result
