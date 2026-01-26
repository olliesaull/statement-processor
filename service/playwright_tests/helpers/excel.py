"""Excel parsing helpers for Playwright tests."""

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import Cell

from playwright_tests.helpers.tables import TableData


def _collapse_whitespace(value: str) -> str:
    """Normalize whitespace for string comparison.

    Args:
        value: Raw string value.

    Returns:
        Normalized string with collapsed whitespace.
    """
    return " ".join(value.split())


def _cell_to_text(cell: Cell) -> str:
    """Render a cell value as display text.

    Args:
        cell: OpenPyXL cell instance.

    Returns:
        Display string for the cell.
    """
    if cell.value is None:
        return ""
    return str(cell.value)


def read_excel_table(path: Path) -> TableData:
    """Read a worksheet into a TableData structure.

    Args:
        path: Path to the Excel file.

    Returns:
        TableData containing headers and rows.
    """
    workbook = load_workbook(path, data_only=True)

    rows: list[list[str]] = []
    for row in workbook.active.iter_rows():
        values = [_collapse_whitespace(_cell_to_text(cell)) for cell in row]
        if any(values):
            rows.append(values)

    if not rows:
        return TableData(headers=[], rows=[])

    headers = rows[0]
    data_rows = rows[1:]
    return TableData(headers=headers, rows=data_rows)
