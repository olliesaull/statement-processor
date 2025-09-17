from typing import Dict, List, TypedDict

from botocore.exceptions import ClientError
from textractor import Textractor
from textractor.data.constants import TextractFeatures
from textractor.entities.table import Table

from config import AWS_PROFILE, AWS_REGION, logger

class TableOnPage(TypedDict):
    """Simple table representation extracted from Textract for a given page."""
    page: int
    grid: List[List[str]]


def _sanitize_grid(grid: List[List[str]]) -> List[List[str]]:
    """Remove empty rows/columns so downstream logic matches legacy output."""

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

    return [[row[idx] for idx in keep_cols] for row in meaningful_rows]


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


def analyze_tables_s3(bucket: str, key: str) -> List[TableOnPage]:
    """Run Textract TABLES on an S3 object and return tables per page."""
    textractor = Textractor(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    s3_path = f"s3://{bucket}/{key}"

    if key.lower().endswith((".pdf", ".tif", ".tiff")):
        document = textractor.start_document_analysis(s3_path, features=[TextractFeatures.TABLES])
    else:
        document = textractor.analyze_document(s3_path, features=[TextractFeatures.TABLES])

    tables: List[TableOnPage] = []
    for index, page in enumerate(document.pages, start=1):
        page_number = page.page_num if isinstance(page.page_num, int) and page.page_num > 0 else index
        for table in page.tables:
            grid = _table_to_grid(table)
            if grid:
                tables.append({"page": int(page_number), "grid": grid})

    tables.sort(key=lambda t: t["page"])
    return tables


def get_tables(bucket: str, key: str) -> Dict[str, List[TableOnPage]]:
    """Convenience wrapper returning a mapping of key -> extracted tables."""
    result: Dict[str, List[TableOnPage]] = {}
    try:
        result[key] = analyze_tables_s3(bucket, key)
    except ClientError as ce:
        logger.info("AWS error", key=key, error=ce)
    except Exception as e:
        logger.info("Error processing file", key=key, error=e)
    return result
