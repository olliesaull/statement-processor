"""Tenant management helpers for Playwright tests."""

from playwright.sync_api import Page


def switch_to_tenant_row(page: Page, *, row_id: str) -> None:
    """Switch the active tenant by clicking the row's switch button.

    Args:
        page: Playwright page fixture.
        row_id: Tenant row DOM id (e.g., row-<tenantId>).

    Returns:
        None.
    """
    row = page.locator(f"#{row_id}")
    switch_button = row.get_by_role("button", name="Switch to Tenant")
    if switch_button.count() == 0:
        raise AssertionError(f"Switch button not found for tenant row {row_id}.")
    switch_button.first.click()
    page.wait_for_url("**/tenant_management**")
    row.locator("text=Current Tenant").wait_for()
