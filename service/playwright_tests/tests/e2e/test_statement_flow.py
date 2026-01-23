"""End-to-end statement flow tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from playwright_tests.helpers.auth import require_test_login_secret, seed_test_login
from playwright_tests.helpers.configs import configure_contact
from playwright_tests.helpers.excel import read_excel_table
from playwright_tests.helpers.runs import StatementFlowRun, load_test_runs
from playwright_tests.helpers.statements import (
    delete_statement_if_exists,
    download_excel,
    mark_first_incomplete_item,
    open_statement_from_list,
    open_statements_page,
    require_statement_file,
    set_payments_visibility,
    upload_statement,
    wait_for_statement_table,
)
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


def _prepare_statement(page: Page, test_run: StatementFlowRun) -> None:
    """Create a contact config, upload a statement, and open the detail view.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    login_secret = require_test_login_secret()
    require_statement_file(test_run)
    seed_test_login(page.context, test_run, login_secret)

    delete_statement_if_exists(page, test_run)
    configure_contact(page, test_run)
    upload_statement(page, test_run)

    open_statements_page(page, test_run.base_url)
    open_statement_from_list(page, test_run.contact_name)
    wait_for_statement_table(page)
    set_payments_visibility(page, show_payments=True)


@pytest.mark.parametrize("test_run", TEST_RUNS, ids=lambda run: run.contact_name)
def test_config_upload_ui_validation(page: Page, test_run: StatementFlowRun) -> None:
    """Validate the full statement table against a baseline Excel file.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    expected_excel_path = _require_expected_excel(test_run)
    _prepare_statement(page, test_run)

    expected_table = read_excel_table(expected_excel_path, sheet_name=test_run.expected_excel_sheet)
    actual_table = extract_statement_table(page)
    normalized_expected = normalize_table_data(expected_table)
    normalized_actual = normalize_table_data(actual_table)
    assert_table_equal(normalized_expected, normalized_actual)

    table = page.locator("#statement-table")
    expect(table).to_be_visible()
    for expected in test_run.expected_table_text:
        expect(table).to_contain_text(expected)


@pytest.mark.parametrize("test_run", TEST_RUNS, ids=lambda run: run.contact_name)
def test_ui_actions_excel_export_validation(page: Page, test_run: StatementFlowRun, tmp_path: Path) -> None:
    """Validate Excel exports reflect UI actions.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.
        tmp_path: Temporary path for downloads.

    Returns:
        None.
    """
    _prepare_statement(page, test_run)

    table_before = normalize_table_data(extract_statement_table(page))
    type_index = column_index(table_before.headers, "Type")
    has_payments = any(row[type_index] == "PMT" for row in table_before.rows if len(row) > type_index)
    if not has_payments:
        pytest.skip("No payment rows available to validate hide-payments export.")

    set_payments_visibility(page, show_payments=False)
    hidden_download = download_excel(page, tmp_path)
    hidden_table = normalize_table_data(read_excel_table(hidden_download))
    hidden_type_index = column_index(hidden_table.headers, "Type")
    assert all(row[hidden_type_index] != "PMT" for row in hidden_table.rows if len(row) > hidden_type_index)

    set_payments_visibility(page, show_payments=True)
    mark_first_incomplete_item(page)
    completed_download = download_excel(page, tmp_path)
    completed_table = normalize_table_data(read_excel_table(completed_download))
    status_index = column_index(completed_table.headers, "Status")
    assert any(row[status_index] == "Completed" for row in completed_table.rows if len(row) > status_index)
