"""Pytest configuration for Playwright tests."""

from collections.abc import Callable

import pytest
from dotenv import load_dotenv
from playwright.sync_api import Page

from playwright_tests.helpers.configs import configure_contact
from playwright_tests.helpers.logging import log_step
from playwright_tests.helpers.runs import StatementFlowRun
from playwright_tests.helpers.statements import delete_statement_if_exists, open_statement_from_list, open_statements_page, require_statement_file, upload_statement, wait_for_statement_table
from playwright_tests.helpers.xero_login import STORAGE_STATE_PATH, ensure_xero_login

load_dotenv()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:  # pylint: disable=redefined-outer-name
    """Inject persisted Playwright auth state into the browser context.

    Args:
        browser_context_args: Default Playwright context args.

    Returns:
        Updated context args including storage state when available.
    """
    if STORAGE_STATE_PATH.exists():
        return {**browser_context_args, "storage_state": str(STORAGE_STATE_PATH)}
    return browser_context_args


@pytest.fixture(scope="session", name="uploaded_statement_cache")
def _uploaded_statement_cache() -> set[tuple[str, str, str, str]]:  # added _ to avoid disable=redefined-outer-name
    """Cache uploaded statements across the pytest session.

    Returns:
        Set of cache keys for uploaded statements.
    """
    return set()


@pytest.fixture
def prepare_statement(uploaded_statement_cache: set[tuple[str, str, str, str]]) -> Callable[[Page, StatementFlowRun], None]:
    """Build a statement flow and reuse uploads within the session.

    Args:
        uploaded_statement_cache: Cache of statement uploads for the session.

    Returns:
        Callable that prepares a statement for the given test run.
    """

    def _prepare(page: Page, test_run: StatementFlowRun) -> None:
        """Create a contact config, upload a statement once, and open the detail view.

        Args:
            page: Playwright page fixture.
            test_run: Current statement test run.

        Returns:
            None.
        """
        log_step("playwright", f"Preparing statement flow for {test_run.contact_name}.")
        require_statement_file(test_run)
        log_step("playwright", "Logging into Xero.")
        ensure_xero_login(page, base_url=test_run.base_url, tenant_id=test_run.tenant_id, tenant_name=test_run.tenant_name)

        cache_key = (test_run.base_url.rstrip("/"), test_run.tenant_id, test_run.contact_name, test_run.statement_filename)
        if cache_key not in uploaded_statement_cache:
            log_step("playwright", "Deleting existing statement if present.")
            delete_statement_if_exists(page, test_run)
            log_step("playwright", "Updating contact mapping.")
            configure_contact(page, test_run)
            log_step("playwright", "Uploading statement PDF.")
            upload_statement(page, test_run)
            uploaded_statement_cache.add(cache_key)
        else:
            log_step("playwright", "Statement already uploaded in this session; skipping upload.")

        log_step("playwright", "Opening statement detail view.")
        open_statements_page(page, test_run.base_url)
        open_statement_from_list(page, test_run.contact_name)
        wait_for_statement_table(page)

    return _prepare
