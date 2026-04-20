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

    Listens to ``pageerror`` AND ``console`` because browsers typically surface
    CSP ``EvalError`` as a console-level error rather than an uncaught
    exception — pageerror alone would stay green while the regression is
    present.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.on("console", lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type == "error" else None)

    _login(page)
    page.wait_for_url("**/tenant_management", timeout=10_000)
    page.wait_for_timeout(6_000)  # observe at least two 3s ticks

    csp_errors = [e for e in errors if "EvalError" in e or "unsafe-eval" in e or "Content Security Policy" in e]
    assert csp_errors == [], f"Expected zero CSP eval violations; got: {csp_errors}"


def test_polling_pauses_when_tab_becomes_hidden(page: Page) -> None:
    """Tab visibility must gate polling. Observe at least two poll cycles of silence.

    Waits ~7 s after the visibility change (more than two 3 s poll cycles) and
    asserts zero new poll requests. The prior ``<= 1`` threshold over a 4 s
    window could pass trivially because an unpaused poller could happen to fire
    exactly once in that span — the stricter window cannot.
    """
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

    # Mark the boundary: any request in `requests` after this index was fired
    # after the visibility change. Use the current length rather than elapsed
    # time so we don't race the network event queue.
    hidden_boundary = len(requests)

    # Wait beyond two 3s poll cycles so any unpaused poll would fire at least
    # twice — a single stray in-flight request can no longer mask a regression.
    paused_start = time.monotonic()
    page.wait_for_timeout(7_000)

    extra_requests = requests[hidden_boundary:]
    assert extra_requests == [], f"Polling did not pause after visibility change; got {len(extra_requests)} requests in {time.monotonic() - paused_start:.1f}s: {extra_requests}"
