"""End-to-end statement flow tests."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from playwright_tests.helpers.excel import read_excel_table
from playwright_tests.helpers.logging import log_step
from playwright_tests.helpers.runs import StatementFlowRun, load_test_runs
from playwright_tests.helpers.statements import download_excel, mark_first_incomplete_item
from playwright_tests.helpers.tables import assert_table_equal, column_index, extract_statement_table, normalize_table_data

TEST_RUNS = load_test_runs()


def _require_expected_excel(test_run: StatementFlowRun) -> Path:
    """Return the expected Excel path or skip the test.

    Args:
        test_run: Current statement test run.

    Returns:
        Path to the expected Excel baseline.
    """
    expected_path = test_run.expected_excel_path()
    if expected_path is None:
        pytest.skip("expected_excel_filename is not set for this run.")
    if not expected_path.exists():
        pytest.skip(f"Expected Excel baseline not found: {expected_path}.")
    return expected_path


@pytest.mark.parametrize("test_run", TEST_RUNS, ids=lambda run: run.contact_name)
def test_config_upload_ui_validation(page: Page, test_run: StatementFlowRun, prepare_statement: Callable[[Page, StatementFlowRun], None]) -> None:
    """Validate the full statement table against a baseline Excel file.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.
        prepare_statement: Fixture that prepares the statement flow.

    Returns:
        None.
    """
    log_step("playwright", "Starting UI validation test.")
    expected_excel_path = _require_expected_excel(test_run)
    prepare_statement(page, test_run)

    log_step("playwright", f"Comparing UI table to Excel baseline: {expected_excel_path.name}.")
    expected_table = read_excel_table(expected_excel_path)
    actual_table = extract_statement_table(page)
    normalized_expected = normalize_table_data(expected_table)
    normalized_actual = normalize_table_data(actual_table)
    assert_table_equal(normalized_expected, normalized_actual)

    table = page.locator("#statement-table")
    expect(table).to_be_visible()


@pytest.mark.parametrize("test_run", TEST_RUNS, ids=lambda run: run.contact_name)
def test_ui_actions_excel_export_validation(page: Page, test_run: StatementFlowRun, tmp_path: Path, prepare_statement: Callable[[Page, StatementFlowRun], None]) -> None:
    """Validate Excel exports reflect UI actions.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.
        tmp_path: Temporary path for downloads.
        prepare_statement: Fixture that prepares the statement flow.

    Returns:
        None.
    """
    log_step("playwright", "Starting Excel export validation test.")
    prepare_statement(page, test_run)

    log_step("playwright", "Showing payments, marking complete, and exporting Excel.")
    mark_first_incomplete_item(page)
    completed_download = download_excel(page, tmp_path)
    completed_table = normalize_table_data(read_excel_table(completed_download))
    status_index = column_index(completed_table.headers, "Status")
    assert any(row[status_index] == "Completed" for row in completed_table.rows if len(row) > status_index)
