"""Authentication helpers for Playwright tests."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import BrowserContext

from playwright_tests.helpers.runs import StatementFlowRun

TEST_LOGIN_SECRET = os.getenv("TEST_LOGIN_SECRET", "")
TEST_LOGIN_HEADER = os.getenv("PLAYWRIGHT_TEST_LOGIN_HEADER", "X-Test-Auth")


def require_test_login_secret() -> str:
    """Return the test login secret or skip the module.

    Returns:
        The configured test login secret.
    """
    if not TEST_LOGIN_SECRET:
        pytest.skip("TEST_LOGIN_SECRET is not set; cannot call /test-login.")
    return TEST_LOGIN_SECRET


def seed_test_login(context: BrowserContext, test_run: StatementFlowRun, login_secret: str) -> None:
    """Seed a test login session using the test-only endpoint.

    Args:
        context: Playwright browser context with shared cookies.
        test_run: Current statement test run.
        login_secret: Secret value to authenticate the test-only endpoint.

    Returns:
        None.
    """
    response = context.request.post(f"{test_run.base_url}/test-login", headers={TEST_LOGIN_HEADER: login_secret}, json={"tenant_id": test_run.tenant_id, "tenant_name": test_run.tenant_name})
    if not response.ok:
        raise AssertionError(f"Test login failed: {response.status} {response.text()}")
