"""CSP + visibility gating smoke for sync-progress polling.

Verifies the Stage 3 fix (plan step 5): the polling panels no longer trigger
the CSP ``EvalError`` on every tick, and polling pauses when the tab becomes
hidden.

Requires a running local container with the real nginx CSP and valid
``PLAYWRIGHT_TENANT_ID`` / ``PLAYWRIGHT_TENANT_NAME`` env vars. See
``.claude/rules/browser-testing.md``. Skips when ``PLAYWRIGHT_BASE_URL`` is
unset so the suite stays green on machines without the container.
"""

from __future__ import annotations

import os
import time

import pytest
from playwright.sync_api import Page

BASE_URL = os.environ.get("PLAYWRIGHT_BASE_URL") or os.environ.get("BASE_URL")

pytestmark = pytest.mark.skipif(not BASE_URL, reason="PLAYWRIGHT_BASE_URL unset — requires a running local container (see browser-testing.md).")


def _login(page: Page) -> None:
    assert BASE_URL is not None
    page.goto(f"{BASE_URL}/test-login", wait_until="networkidle")


def test_tenant_management_polling_emits_no_csp_eval_errors(page: Page) -> None:
    """Over a 6-second observation window, polling must not raise any EvalError.

    Before fix: htmx compiled the ``every 3s[expr]`` bracket filter via
    ``new Function()``, and every 3s tick raised a CSP ``EvalError`` under the
    finance-app CSP (no ``'unsafe-eval'``).
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))

    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)
    page.wait_for_timeout(6_000)  # observe at least two 3s ticks

    eval_errors = [e for e in errors if "EvalError" in e or "unsafe-eval" in e]
    assert eval_errors == [], f"Expected zero CSP EvalErrors; got: {eval_errors}"


def test_polling_pauses_when_tab_becomes_hidden(page: Page) -> None:
    """Tab visibility must gate polling within one 3s cycle (4s grace)."""
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url) if "/tenants/sync-progress" in req.url else None)

    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)

    # Let polling run briefly so we see at least one request.
    page.wait_for_timeout(4_000)
    baseline = len(requests)
    assert baseline >= 1, "Expected at least one poll tick before emulating visibility change."

    # Emulate tab hidden.
    page.evaluate(
        "() => { Object.defineProperty(document, 'visibilityState', { get: () => 'hidden', configurable: true }); "
        "Object.defineProperty(document, 'hidden', { get: () => true, configurable: true }); "
        "document.dispatchEvent(new Event('visibilitychange')); }"
    )

    # Wait beyond one poll cycle + grace. Polling should stop.
    paused_start = time.monotonic()
    page.wait_for_timeout(4_000)
    hidden_requests = len(requests) - baseline
    assert hidden_requests <= 1, f"Expected polling to pause within one 3s cycle after visibility change; observed {hidden_requests} extra requests in {time.monotonic() - paused_start:.1f}s."
