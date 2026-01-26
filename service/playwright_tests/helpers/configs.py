"""Configuration page helpers for Playwright tests."""

from playwright.sync_api import Page, expect

from playwright_tests.helpers.runs import StatementFlowRun


def open_configs_page(page: Page, base_url: str) -> None:
    """Navigate to the configs page.

    Args:
        page: Playwright page fixture.
        base_url: Base URL for the app under test.

    Returns:
        None.
    """
    page.goto(f"{base_url}/configs", wait_until="domcontentloaded")


def load_contact_config(page: Page, contact_name: str) -> None:
    """Load the config for a specific contact.

    Args:
        page: Playwright page currently on the configs page.
        contact_name: Contact name to load.

    Returns:
        None.
    """
    page.locator("[data-automation='config-contact-input']").fill(contact_name)
    page.locator("[data-automation='config-load-button']").click()
    page.wait_for_selector("[data-automation='config-save-form']")


def _fill_total_columns(page: Page, total_columns: list[str]) -> None:
    """Fill total column mappings, adding inputs as needed.

    Args:
        page: Playwright page on the config form.
        total_columns: Ordered list of total column labels.

    Returns:
        None.
    """
    add_button = page.locator("[data-automation='config-map-total-add']")
    container = page.locator("[data-automation='config-map-total-container']")
    inputs = container.locator("[data-automation='config-map-total']")
    for _ in range(max(len(total_columns) - inputs.count(), 0)):
        add_button.click()
    inputs = container.locator("[data-automation='config-map-total']")
    for idx, value in enumerate(total_columns):
        inputs.nth(idx).fill(value)


def update_contact_mapping(page: Page, test_run: StatementFlowRun) -> None:
    """Update mapping values for a contact config.

    Args:
        page: Playwright page on the config form.
        test_run: Current statement test run.

    Returns:
        None.
    """
    page.locator("[data-automation='config-map-number']").fill(test_run.number_column)
    page.locator("[data-automation='config-map-date']").fill(test_run.date_column)
    _fill_total_columns(page, test_run.total_column)
    page.locator("[data-automation='config-date-format']").fill(test_run.date_format)


def save_contact_config(page: Page) -> None:
    """Save the contact config and assert success.

    Args:
        page: Playwright page on the config form.

    Returns:
        None.
    """
    page.locator("[data-automation='config-save-button']").click()
    expect(page.get_by_role("alert")).to_contain_text("Config updated successfully.")


def configure_contact(page: Page, test_run: StatementFlowRun) -> None:
    """Load, update, and save a contact config.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    open_configs_page(page, test_run.base_url)
    load_contact_config(page, test_run.contact_name)
    update_contact_mapping(page, test_run)
    save_contact_config(page)
