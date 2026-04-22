"""Visual + interaction E2E for the tenant-management cards.

Guards:
  1. Card markup renders for the current session tenant.
  2. Expandable detail opens on click and stays open across an HTMX poll swap
     (htmx:beforeSwap pre-apply path is not glitching).
  3. No CSP violations during a poll cycle.

Requires a running local container with ``STAGE=local``,
``PLAYWRIGHT_TENANT_ID``, and ``PLAYWRIGHT_TENANT_NAME`` set. Skips when
``PLAYWRIGHT_BASE_URL`` is unset so the suite stays green on machines without
the container (matches the pattern in ``test_csp_afk.py``).
"""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect

BASE_URL = os.environ.get("PLAYWRIGHT_BASE_URL") or os.environ.get("BASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="PLAYWRIGHT_BASE_URL unset — requires a running local container (see browser-testing.md).")


def _login(page: Page) -> None:
    assert BASE_URL is not None
    page.goto(f"{BASE_URL}/test-login", wait_until="networkidle")


def test_card_renders_for_current_tenant(page: Page) -> None:
    """Smoke: current-tenant card is visible with the expected state classes."""
    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)
    tenant_id = os.environ["PLAYWRIGHT_TENANT_ID"]

    card = page.locator(f"#card-{tenant_id}")
    expect(card).to_be_visible()
    expect(card).to_have_class(lambda value: "is-current" in value, timeout=5_000)
    expect(card.get_by_text("Current Tenant", exact=True)).to_be_visible()


def test_detail_stays_open_across_htmx_swap(page: Page) -> None:
    """Click Show detail, force a poll cycle, assert detail is still data-expanded=true."""
    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)
    tenant_id = os.environ["PLAYWRIGHT_TENANT_ID"]

    # Only syncing cards have a Show detail button — skip if the local cache is
    # Ready so the test isn't platform-dependent on sync state.
    toggle = page.locator(f"[data-toggle-target='detail-{tenant_id}']")
    if toggle.count() == 0:
        pytest.skip("Tenant is in Ready state; no detail toggle on the page.")

    toggle.click()
    detail = page.locator(f"#detail-{tenant_id}")
    expect(detail).to_have_attribute("data-expanded", "true")

    # Force a poll cycle via htmx.ajax and wait for the response to settle
    # before asserting. htmx.ajax is async, so we need an explicit wait to
    # avoid racing the subsequent attribute read.
    with page.expect_response("**/tenants/sync-progress") as resp_info:
        page.evaluate("htmx.ajax('GET', '/tenants/sync-progress', {target: '#sync-progress-panel', swap: 'outerHTML'})")
    assert resp_info.value.ok

    # htmx replaces the entire #sync-progress-panel, so wait for the new
    # detail node to attach before asserting on its attributes.
    page.wait_for_selector(f"#detail-{tenant_id}", state="attached")
    expect(page.locator(f"#detail-{tenant_id}")).to_have_attribute("data-expanded", "true")


def test_no_csp_eval_errors_during_poll(page: Page) -> None:
    """Polling + beforeSwap handler must not violate CSP (no eval / new Function)."""
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)
    page.wait_for_timeout(6_000)  # at least two natural 3s ticks

    csp_errors = [e for e in errors if "unsafe-eval" in e or "EvalError" in e or "Content Security Policy" in e]
    assert csp_errors == [], csp_errors
