"""End-to-end statement flow tests."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from playwright_tests.helpers.configs import count_config_contacts
from playwright_tests.helpers.excel import read_excel_table, require_expected_excel
from playwright_tests.helpers.logging import log_step
from playwright_tests.helpers.runs import StatementFlowRun, load_test_runs
from playwright_tests.helpers.statements import download_excel, mark_first_incomplete_item
from playwright_tests.helpers.tables import assert_table_equal, column_index, extract_statement_table, normalize_table_data
from playwright_tests.helpers.tenants import switch_to_tenant_row
from playwright_tests.helpers.xero_login import ensure_xero_login

TEST_RUNS = load_test_runs()


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
    expected_excel_path = require_expected_excel(test_run)
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


@pytest.mark.parametrize("test_run", TEST_RUNS, ids=lambda run: run.contact_name)
def test_tenant_switching_updates_contacts(page: Page, test_run: StatementFlowRun) -> None:
    """Validate tenant switching updates the active badge and contact list.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    log_step("playwright", "Starting tenant switching test.")
    ensure_xero_login(page, base_url=test_run.base_url, tenant_id=test_run.tenant_id, tenant_name=test_run.tenant_name)

    page.goto(f"{test_run.base_url}/tenant_management", wait_until="domcontentloaded")
    page.wait_for_selector("tr")

    active_row = page.locator("tr", has_text="Current Tenant").first
    active_row_id = (active_row.get_attribute("id") or "").strip()
    if not active_row_id:
        raise AssertionError("Active tenant row id not found.")

    switch_buttons = page.get_by_role("button", name="Switch to Tenant")
    if switch_buttons.count() == 0:
        log_step("playwright", "Only one tenant found; skipping tenant switching test.")
        pytest.skip("Only one tenant connected.")

    target_button = switch_buttons.first
    target_row = target_button.locator("xpath=ancestor::tr[1]")
    target_row_id = (target_row.get_attribute("id") or "").strip()
    if not target_row_id:
        raise AssertionError("Target tenant row id not found.")

    log_step("playwright", "Switching to another tenant.")
    target_button.click()
    page.wait_for_url("**/tenant_management**")
    page.locator(f"#{target_row_id}").locator("text=Current Tenant").wait_for()

    target_contact_count = count_config_contacts(page, test_run.base_url)

    log_step("playwright", "Switching back to original tenant.")
    page.goto(f"{test_run.base_url}/tenant_management", wait_until="domcontentloaded")
    page.wait_for_selector("tr")
    switch_to_tenant_row(page, row_id=active_row_id)
    original_contact_count = count_config_contacts(page, test_run.base_url)

    if target_contact_count == original_contact_count:
        raise AssertionError(f"Contact counts did not change after tenant switch (both {original_contact_count}).")
