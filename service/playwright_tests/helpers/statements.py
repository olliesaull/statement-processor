"""Statement page helpers for Playwright tests."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from playwright_tests.helpers.runs import StatementFlowRun

STATEMENT_WAIT_SECONDS = float(5)
STATEMENT_MAX_REFRESHES = 30


def require_statement_file(test_run: StatementFlowRun) -> None:
    """Skip the test when the statement PDF is missing.

    Args:
        test_run: Current statement test run.

    Returns:
        None.
    """
    statement_path = test_run.statement_path()
    if not statement_path.exists():
        pytest.skip(f"Missing statement PDF: {statement_path}")


def open_statements_page(page: Page, base_url: str) -> None:
    """Navigate to the statements list page.

    Args:
        page: Playwright page fixture.
        base_url: Base URL for the app under test.

    Returns:
        None.
    """
    page.goto(f"{base_url}/statements", wait_until="domcontentloaded")


def open_statement_from_list(page: Page, contact_name: str) -> None:
    """Open the statement detail page by clicking the list row.

    Args:
        page: Playwright page on the statements list.
        contact_name: Contact name to match.

    Returns:
        None.
    """
    for _ in range(20):
        row = page.locator("tr[data-automation='statement-row']").filter(has_text=contact_name)
        if row.count() > 0:
            row.locator("[data-automation='statement-reconcile-link']").first.click()
            return
        page.wait_for_timeout(2_000)
        page.reload()
    raise AssertionError("Statement row for the contact did not appear in time.")


def wait_for_statement_table(page: Page) -> None:
    """Wait for the statement table to render, refreshing while processing.

    Args:
        page: Playwright page currently on the statement detail view.

    Returns:
        None.
    """
    for _ in range(STATEMENT_MAX_REFRESHES):
        try:
            page.wait_for_selector("[data-automation='statement-table']", timeout=3_000)
            return
        except PlaywrightTimeoutError:
            if page.locator("text=We're still preparing this statement.").count() > 0:
                page.wait_for_timeout(int(STATEMENT_WAIT_SECONDS * 1000))
                page.reload()
                continue
            raise
    raise AssertionError("Statement table did not render before the timeout limit.")


def upload_statement(page: Page, test_run: StatementFlowRun) -> None:
    """Upload a statement PDF for the configured contact.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    page.goto(f"{test_run.base_url}/upload-statements", wait_until="domcontentloaded")
    page.set_input_files("[data-automation='statement-upload-file']", str(test_run.statement_path()))
    page.locator("[data-automation='statement-upload-contact']").fill(test_run.contact_name)
    page.locator("[data-automation='statement-upload-submit']").click()
    expect(page.get_by_role("alert")).to_contain_text("Uploaded")


def delete_statement_if_exists(page: Page, test_run: StatementFlowRun) -> None:
    """Delete an existing statement for the contact to reset test data.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    # Deletions can take a moment to reflect back in the statements list (redirect + eventual consistency),
    # so we retry a few times to make the reset step more reliable.
    for _ in range(3):
        open_statements_page(page, test_run.base_url)
        row = page.locator("tr[data-automation='statement-row']").filter(has_text=test_run.contact_name)
        if row.count() == 0:
            return

        row.locator("[data-automation='statement-reconcile-link']").first.click()
        wait_for_statement_table(page)

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("[data-automation='statement-delete-button']").click()
        page.wait_for_url("**/statements")
        page.wait_for_selector("[data-automation='statements-table']")


def set_payments_visibility(page: Page, *, show_payments: bool) -> None:
    """Toggle the payments visibility on the statement page.

    Args:
        page: Playwright page on the statement detail view.
        show_payments: True to show payments, False to hide them.

    Returns:
        None.
    """
    toggle = page.locator("[data-automation='statement-payments-toggle']")
    if toggle.count() == 0:
        return
    label = (toggle.inner_text() or "").strip().lower()
    if show_payments and "show payments" in label:
        toggle.click()
        page.wait_for_load_state("networkidle")
        wait_for_statement_table(page)
    if not show_payments and "hide payments" in label:
        toggle.click()
        page.wait_for_load_state("networkidle")
        wait_for_statement_table(page)


def mark_first_incomplete_item(page: Page) -> None:
    """Mark the first incomplete row as complete.

    Args:
        page: Playwright page on the statement detail view.

    Returns:
        None.
    """
    button = page.locator("[data-automation='statement-row-complete']").first
    if button.count() == 0:
        pytest.skip("No incomplete statement rows available to mark complete.")
    button.click()
    page.wait_for_load_state("networkidle")
    wait_for_statement_table(page)


def download_excel(page: Page, download_dir: Path) -> Path:
    """Download the statement Excel export and save to disk.

    Args:
        page: Playwright page on the statement detail view.
        download_dir: Directory to save the downloaded file.

    Returns:
        Path to the saved Excel file.
    """
    download_dir.mkdir(parents=True, exist_ok=True)
    with page.expect_download() as download_info:
        page.locator("[data-automation='statement-download-excel']").click()
    download = download_info.value
    target_path = download_dir / download.suggested_filename
    download.save_as(target_path)
    return target_path
