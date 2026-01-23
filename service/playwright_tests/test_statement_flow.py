"""Playwright end-to-end flow for statements."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field, field_validator

TEST_LOGIN_SECRET = os.getenv("TEST_LOGIN_SECRET", "")
TEST_LOGIN_HEADER = os.getenv("PLAYWRIGHT_TEST_LOGIN_HEADER", "X-Test-Auth")
TEST_RUNS_PATH = Path(__file__).with_name("test_runs.json")

STATEMENT_WAIT_SECONDS = 5
STATEMENT_MAX_REFRESHES = 30


class StatementFlowRun(BaseModel):
    """
    Represent a statement flow test run.

    This model captures the inputs needed for a single end-to-end statement scenario.

    Attributes:
        base_url: Base URL for the app under test.
        tenant_id: Tenant ID to seed in the test login.
        tenant_name: Tenant name to seed in the test login.
        contact_name: Contact name used in config + upload.
        number_column: Statement column mapped to invoice number.
        date_column: Statement column mapped to transaction date.
        total_column: Statement columns mapped to totals.
        date_format: Date format string for statement parsing.
        statement_filename: PDF filename in the playwright_tests directory.
        expected_table_text: Optional substrings to assert in the statement table.
    """

    base_url: str = "http://localhost:8080"
    tenant_id: str
    tenant_name: str
    contact_name: str
    number_column: str
    date_column: str
    total_column: list[str]
    date_format: str = "DD/MM/YYYY"
    statement_filename: str
    expected_table_text: list[str] = Field(default_factory=list)

    @field_validator("statement_filename")
    @classmethod
    def _validate_statement_filename(cls, value: str) -> str:
        """Validate the statement PDF filename.

        Args:
            value: Raw filename.

        Returns:
            Normalized filename.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("statement_filename must be set")
        return stripped

    @field_validator("tenant_id", "tenant_name", "contact_name", "number_column", "date_column", "date_format")
    @classmethod
    def _require_non_empty(cls, value: str, info: Any) -> str:
        """Ensure required string fields are not empty.

        Args:
            value: Field value.
            info: Pydantic field context.

        Returns:
            Normalized non-empty string.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{info.field_name} must be set")
        return stripped

    @field_validator("total_column", mode="before")
    @classmethod
    def _validate_total_column(cls, value: object) -> list[str]:
        """Validate total columns are provided.

        Args:
            value: Raw total column value.

        Returns:
            List of non-empty total column labels.
        """
        cleaned = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        if not cleaned:
            raise ValueError("total_column must contain at least one entry")
        return cleaned

    @field_validator("expected_table_text", mode="before")
    @classmethod
    def _parse_expected_table_text(cls, value: object) -> list[str]:
        """Coerce expected table text into a list of strings.

        Args:
            value: Raw value from env or JSON.

        Returns:
            List of non-empty strings.
        """
        if value is None:
            return []
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",")]
            return [item for item in items if item]
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raise ValueError("expected_table_text must be a list or comma-separated string")

    def statement_path(self) -> Path:
        """Build the PDF path for this run.

        Args:
            None.

        Returns:
            Path to the statement PDF inside /statements.
        """
        return Path("/statements") / self.statement_filename


def _load_test_runs() -> list[StatementFlowRun]:
    """Load statement runs from the test_runs.json file.

    Args:
        None.

    Returns:
        List of StatementFlowRun instances.
    """
    if not TEST_RUNS_PATH.exists():
        pytest.skip(f"{TEST_RUNS_PATH.name} not found in playwright_tests.", allow_module_level=True)
    try:
        payload = json.loads(TEST_RUNS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{TEST_RUNS_PATH.name} must be valid JSON") from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{TEST_RUNS_PATH.name} must be a non-empty JSON list")
    return [StatementFlowRun.model_validate(item) for item in payload]


TEST_RUNS = _load_test_runs()


def _require_test_inputs(test_run: StatementFlowRun) -> None:
    """Skip the test when required inputs are missing.

    Args:
        test_run: Current statement test run.

    Returns:
        None.
    """
    if not TEST_LOGIN_SECRET:
        pytest.skip("TEST_LOGIN_SECRET is not set; cannot call /test-login.")
    if not test_run.statement_path().exists():
        pytest.skip(f"{test_run.statement_filename} not found in playwright_tests.")


def _seed_test_login(context: BrowserContext, test_run: StatementFlowRun) -> None:
    """Seed a test login session using the test-only endpoint.

    Args:
        context: Playwright browser context with shared cookies.
        test_run: Current statement test run.

    Returns:
        None.
    """
    response = context.request.post(f"{test_run.base_url}/test-login", headers={TEST_LOGIN_HEADER: TEST_LOGIN_SECRET}, json={"tenant_id": test_run.tenant_id, "tenant_name": test_run.tenant_name})
    if not response.ok:
        raise AssertionError(f"Test login failed: {response.status} {response.text()}")


def _wait_for_statement_table(page: Page) -> None:
    """Wait for the statement table to render, refreshing while processing.

    Args:
        page: Playwright page currently on the statement detail view.

    Returns:
        None.
    """
    for _ in range(STATEMENT_MAX_REFRESHES):
        try:
            page.wait_for_selector("#statement-table", timeout=3_000)
            return
        except PlaywrightTimeoutError:
            if page.locator("text=We're still preparing this statement.").count() > 0:
                page.wait_for_timeout(int(STATEMENT_WAIT_SECONDS * 1000))
                page.reload()
                continue
            raise
    raise AssertionError("Statement table did not render before the timeout limit.")


def _open_statement_from_list(page: Page, test_run: StatementFlowRun) -> None:
    """Open the statement detail page by clicking the list row.

    Args:
        page: Playwright page on the statements list.
        test_run: Current statement test run.

    Returns:
        None.
    """
    for _ in range(20):
        row = page.locator("tr[data-contact-name]").filter(has_text=test_run.contact_name)
        if row.count() > 0:
            row.get_by_role("link", name="Reconcile").first.click()
            return
        page.wait_for_timeout(2_000)
        page.reload()
    raise AssertionError("Statement row for the contact did not appear in time.")


def _fill_total_columns(page: Page, total_columns: list[str]) -> None:
    """Fill total column mappings, adding inputs as needed.

    Args:
        page: Playwright page on the config form.
        total_columns: Ordered list of total column labels.

    Returns:
        None.
    """
    total_row = page.locator("tr:has(input[name='map[total][]'])")
    add_button = total_row.get_by_role("button", name="Add another column")
    container = page.locator("#container-total")
    inputs = container.locator("input[name='map[total][]']")
    for _ in range(max(len(total_columns) - inputs.count(), 0)):
        add_button.click()
    inputs = container.locator("input[name='map[total][]']")
    for idx, value in enumerate(total_columns):
        inputs.nth(idx).fill(value)


@pytest.mark.parametrize("test_run", TEST_RUNS)
def test_statement_flow(page: Page, test_run: StatementFlowRun) -> None:
    """Create a config, upload a statement, and verify the statement table.

    Args:
        page: Playwright page fixture.
        test_run: Current statement test run.

    Returns:
        None.
    """
    _require_test_inputs(test_run)
    _seed_test_login(page.context, test_run)

    page.goto(f"{test_run.base_url}/configs", wait_until="domcontentloaded")
    page.fill("#contactInput", test_run.contact_name)
    page.get_by_role("button", name="Load Config").click()
    page.wait_for_selector("#config-save-form")

    page.fill('input[name="map[number]"]', test_run.number_column)
    page.fill('input[name="map[date]"]', test_run.date_column)
    _fill_total_columns(page, test_run.total_column)
    page.fill("#dateFormat", test_run.date_format)
    page.locator("#config-save-button").click()
    expect(page.get_by_role("alert")).to_contain_text("Config updated successfully.")

    page.goto(f"{test_run.base_url}/upload-statements", wait_until="domcontentloaded")
    page.set_input_files("input.statement-file-input", str(test_run.statement_path()))
    page.fill("input.contact-input", test_run.contact_name)
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_role("alert")).to_contain_text("Uploaded")

    page.goto(f"{test_run.base_url}/statements", wait_until="domcontentloaded")
    _open_statement_from_list(page, test_run)
    _wait_for_statement_table(page, test_run)

    table = page.locator("#statement-table")
    expect(table).to_be_visible()
    expect(table.locator("thead")).to_contain_text("Statement")
    expect(table.locator("thead")).to_contain_text("Xero")
    expect(table.locator("thead")).to_contain_text("Status")
    assert table.locator("tbody tr").count() > 0
    for expected in test_run.expected_table_text:
        expect(table).to_contain_text(expected)
