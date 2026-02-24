"""Xero OAuth login helpers for Playwright tests."""

import os
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from playwright_tests.helpers.logging import log_step

LOGIN_TIMEOUT_MS = 180 * 1000
XERO_EMAIL_ENV_NAMES = ("PLAYWRIGHT_XERO_EMAIL", "XERO_EMAIL")
XERO_PASSWORD_ENV_NAMES = ("PLAYWRIGHT_XERO_PASSWORD", "XERO_PASSWORD")
STORAGE_STATE_PATH = Path(__file__).resolve().parents[2] / "xero_storage_state.json"


@dataclass(frozen=True)
class XeroCredentials:
    """
    Represent credentials used for Xero OAuth login in tests.

    These credentials are used by Playwright flows to authenticate with Xero during end-to-end tests.

    Attributes:
        email: Xero account email address.
        password: Xero account password.
    """

    email: str
    password: str


def _persist_storage_state(page: Page) -> None:
    """Persist the browser storage state to disk.

    Args:
        page: Playwright page with the authenticated browser context.

    Returns:
        None.
    """
    STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(STORAGE_STATE_PATH))
    log_step("xero-login", f"Saved browser auth state to {STORAGE_STATE_PATH}.")


def _first_env_value(env_names: tuple[str, ...]) -> str:
    """Return the first non-empty environment value from the given names.

    Args:
        env_names: Ordered env var names to check.

    Returns:
        The first non-empty env value, or an empty string when none are set.
    """
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""


def load_xero_credentials() -> XeroCredentials:
    """Load Xero credentials from environment or prompt the user.

    Returns:
        XeroCredentials instance.
    """
    email = _first_env_value(XERO_EMAIL_ENV_NAMES)
    if not email:
        email = input("Xero email: ").strip()
    password = _first_env_value(XERO_PASSWORD_ENV_NAMES)
    if not password:
        password = getpass("Xero password: ")
    if not email or not password:
        pytest.skip("Xero credentials are required for login.")
    return XeroCredentials(email=email, password=password)


def _first_visible(page: Page, selectors: list[str]) -> Locator | None:
    """Return the first visible locator matching the selector list.

    Args:
        page: Playwright page to search.
        selectors: Ordered list of selectors to test.

    Returns:
        First visible locator or None if none are visible.
    """
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            return locator
    return None


def _submit_login_form(page: Page, credentials: XeroCredentials) -> bool:
    """Fill and submit the Xero login form if present.

    Args:
        page: Playwright page currently on the Xero login view.
        credentials: XeroCredentials containing email/password.

    Returns:
        True when a login form was submitted.
    """
    log_step("xero-login", "Submitting Xero login form.")
    email_locator = _first_visible(page, ["#xl-form-email", "input[name='email']", "input[type='email']"])
    if not email_locator:
        return False
    email_locator.fill(credentials.email)

    password_locator = _first_visible(page, ["#xl-form-password", "input[name='password']", "input[type='password']"])
    if not password_locator:
        continue_button = _first_visible(page, ["button:has-text('Next')", "button:has-text('Continue')", "#xl-form-continue"])
        if continue_button:
            continue_button.click()
            page.wait_for_selector("input[type='password']", timeout=LOGIN_TIMEOUT_MS)
            password_locator = _first_visible(page, ["#xl-form-password", "input[name='password']", "input[type='password']"])

    if not password_locator:
        raise AssertionError("Xero password input was not found after submitting email.")

    password_locator.fill(credentials.password)

    submit_button = _first_visible(page, ["#xl-form-submit", "button:has-text('Log in')", "button[type='submit']"])
    if submit_button:
        submit_button.click()
    else:
        password_locator.press("Enter")

    return True


def _accept_cookie_consent_if_needed(page: Page, *, base_url: str) -> None:
    """Accept essential cookies when the app redirects login flows to /cookies.

    Args:
        page: Playwright page currently on the app domain.
        base_url: Base URL for the app under test.

    Returns:
        None.
    """
    normalized_base_url = base_url.rstrip("/")
    if not page.url.startswith(normalized_base_url):
        return

    on_cookie_page = page.url.startswith(f"{normalized_base_url}/cookies")
    consent_button = _first_visible(page, ["[data-automation='cookie-accept-button']", "#cookie-accept-button", "a:has-text('Accept Essential Cookies')"])
    if not on_cookie_page and not consent_button:
        return
    if not consent_button:
        raise AssertionError("Cookie consent page is shown, but the accept button was not found.")

    log_step("xero-login", "Cookie consent required; accepting essential cookies.")
    consent_button.click()

    # The consent button navigates to /tenant_management and may then continue to /login or Xero.
    # Wait briefly to ensure we do not remain stuck on /cookies.
    for _ in range(20):
        if not page.url.startswith(f"{normalized_base_url}/cookies"):
            return
        page.wait_for_timeout(250)
    raise AssertionError("Cookie consent did not navigate away from /cookies after accepting.")


def _approve_connection(page: Page, *, base_url: str) -> None:
    """Approve the Xero connection if prompted.

    Args:
        page: Playwright page after login submission.
        base_url: Base URL for the app under test.

    Returns:
        None.
    """
    log_step("xero-login", "Checking for Xero connection approval prompt.")
    approve_button = page.locator("#approveButton")
    try:
        approve_button.wait_for(state="visible", timeout=LOGIN_TIMEOUT_MS)
        approve_button.click()
        return
    except PlaywrightTimeoutError:
        pass

    allow_button = _first_visible(page, ["button:has-text('Allow access')", "button:has-text('Connect')", "button:has-text('Approve')"])
    if allow_button:
        allow_button.click()
        return

    page.wait_for_url(f"{base_url.rstrip('/')}/**", timeout=LOGIN_TIMEOUT_MS)


def _ensure_active_tenant(page: Page, *, base_url: str, tenant_id: str, tenant_name: str | None) -> None:
    """Select the active tenant in the tenant management view.

    Args:
        page: Playwright page on the tenant management view.
        base_url: Base URL for the app under test.
        tenant_id: Tenant ID to activate.
        tenant_name: Optional tenant name fallback.

    Returns:
        None.
    """
    normalized_base_url = base_url.rstrip("/")
    log_step("xero-login", f"Ensuring tenant is active: {tenant_name or tenant_id}.")
    page.goto(f"{normalized_base_url}/tenant_management", wait_until="domcontentloaded")
    _accept_cookie_consent_if_needed(page, base_url=normalized_base_url)

    row = page.locator(f"#row-{tenant_id}")
    if row.count() == 0 and tenant_name:
        row = page.locator("tr").filter(has_text=tenant_name)
    if row.count() == 0:
        raise AssertionError(f"Tenant {tenant_id} was not found on the tenant management page.")

    if row.locator("text=Current Tenant").count() > 0:
        log_step("xero-login", "Tenant already active.")
        return

    switch_button = row.get_by_role("button", name="Switch to Tenant")
    if switch_button.count() == 0:
        raise AssertionError(f"Switch button not found for tenant {tenant_id}.")

    switch_button.first.click()
    page.wait_for_load_state("networkidle")
    row.locator("text=Current Tenant").wait_for(timeout=LOGIN_TIMEOUT_MS)


def ensure_xero_login(page: Page, *, base_url: str, tenant_id: str, tenant_name: str | None = None) -> None:
    """Run the full Xero OAuth login flow and select the tenant.

    Args:
        page: Playwright page fixture.
        base_url: Base URL for the app under test.
        tenant_id: Tenant ID to activate after login.
        tenant_name: Optional tenant name fallback when selecting the tenant.

    Returns:
        None.
    """
    normalized_base_url = base_url.rstrip("/")
    log_step("xero-login", "Starting Xero OAuth login flow.")
    page.goto(f"{normalized_base_url}/login", wait_until="domcontentloaded")
    _accept_cookie_consent_if_needed(page, base_url=normalized_base_url)

    try:
        page.wait_for_url("**xero.com/**", timeout=LOGIN_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        _accept_cookie_consent_if_needed(page, base_url=normalized_base_url)
        if page.url.startswith(normalized_base_url):
            if page.url.startswith(f"{normalized_base_url}/cookies"):
                raise AssertionError("Login is blocked on /cookies; consent was not accepted by the test flow.") from exc
            log_step("xero-login", "Already authenticated in app session; skipping login form.")
            _ensure_active_tenant(page, base_url=normalized_base_url, tenant_id=tenant_id, tenant_name=tenant_name)
            _persist_storage_state(page)
            return
        raise

    if _first_visible(page, ["#xl-form-email", "input[name='email']", "input[type='email']"]):
        log_step("xero-login", "Xero login form detected; collecting credentials.")
        credentials = load_xero_credentials()
        _submit_login_form(page, credentials)

    _approve_connection(page, base_url=normalized_base_url)
    log_step("xero-login", "Waiting for redirect back to the app.")
    page.wait_for_url(f"{normalized_base_url}/**", timeout=LOGIN_TIMEOUT_MS)
    _ensure_active_tenant(page, base_url=normalized_base_url, tenant_id=tenant_id, tenant_name=tenant_name)
    _persist_storage_state(page)
