"""Table extraction and comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page


@dataclass(frozen=True)
class TableData:
    """
    Represent a flattened statement table.

    Attributes:
        headers: Flattened column headers in display order.
        rows: Table rows aligned with headers.
    """

    headers: list[str]
    rows: list[list[str]]


def _collapse_whitespace(value: str) -> str:
    """Normalize whitespace for string comparison.

    Args:
        value: Raw string value.

    Returns:
        Normalized string with collapsed whitespace.
    """
    return " ".join(value.split())


def _normalize_cell(header: str, value: str) -> str:
    """Normalize a single cell value for comparison.

    Args:
        header: Column header label.
        value: Raw cell value.

    Returns:
        Normalized cell value.
    """
    cleaned = _collapse_whitespace(value)
    if header == "Xero Link":
        if cleaned in {"Open", "Link"}:
            return "Link"
        if cleaned in {"—", "-", "–"}:  # noqa: RUF001
            return ""
    if header == "Status":
        if "Mark complete" in cleaned:
            return "Incomplete"
        if "Mark incomplete" in cleaned:
            return "Completed"
        if "Unavailable" in cleaned:
            return ""
    return cleaned


def extract_statement_table(page: Page, *, table_selector: str = "#statement-table") -> TableData:
    """Extract the statement table headers and rows from the UI.

    Args:
        page: Playwright page on the statement detail view.
        table_selector: CSS selector for the statement table.

    Returns:
        Flattened table data.
    """
    table = page.locator(table_selector)
    header_rows = table.locator("thead tr")
    if header_rows.count() < 2:
        headers = [_collapse_whitespace(text) for text in table.locator("thead th").all_text_contents()]
    else:
        second_headers = [_collapse_whitespace(text) for text in header_rows.nth(1).locator("th").all_text_contents()]
        half = len(second_headers) // 2
        statement_headers = second_headers[:half]
        xero_headers = second_headers[half:]
        headers = ["Type", *[f"Statement {header}" for header in statement_headers], *[f"Xero {header}" for header in xero_headers], "Xero Link", "Status"]

    rows: list[list[str]] = []
    for row in table.locator("tbody tr").all():
        cells = [_collapse_whitespace(text) for text in row.locator("td").all_text_contents()]
        rows.append(cells)
    return TableData(headers=headers, rows=rows)


def normalize_table_data(table: TableData) -> TableData:
    """Normalize table data for comparison.

    Args:
        table: Table data to normalize.

    Returns:
        Normalized table data.
    """
    normalized_headers = [_collapse_whitespace(header) for header in table.headers]
    normalized_rows: list[list[str]] = []
    for row in table.rows:
        normalized_row = []
        for idx, value in enumerate(row):
            header = normalized_headers[idx] if idx < len(normalized_headers) else ""
            normalized_row.append(_normalize_cell(header, value))
        normalized_rows.append(normalized_row)
    return TableData(headers=normalized_headers, rows=normalized_rows)


def column_index(headers: list[str], label: str) -> int:
    """Find a column index by header label.

    Args:
        headers: Header row values.
        label: Column label to find.

    Returns:
        Index of the column.
    """
    try:
        return headers.index(label)
    except ValueError as exc:
        raise AssertionError(f"Header '{label}' not found in {headers}") from exc


def assert_table_equal(expected: TableData, actual: TableData) -> None:
    """Assert two tables are identical.

    Args:
        expected: Expected table data.
        actual: Actual table data.

    Returns:
        None.
    """
    if expected.headers != actual.headers:
        raise AssertionError(f"Header mismatch.\nExpected: {expected.headers}\nActual:   {actual.headers}")
    if len(expected.rows) != len(actual.rows):
        raise AssertionError(f"Row count mismatch. Expected {len(expected.rows)}, got {len(actual.rows)}.")
    for idx, (expected_row, actual_row) in enumerate(zip(expected.rows, actual.rows, strict=False)):
        if expected_row != actual_row:
            raise AssertionError(f"Row {idx + 1} mismatch.\nExpected: {expected_row}\nActual:   {actual_row}")
