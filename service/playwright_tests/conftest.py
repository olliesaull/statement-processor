"""Pytest configuration for Playwright tests."""

import pytest
from dotenv import load_dotenv

from playwright_tests.helpers.xero_login import STORAGE_STATE_PATH

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
