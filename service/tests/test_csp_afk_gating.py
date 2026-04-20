"""Static guards for the CSP-safe AFK/visibility polling contract.

htmx compiles an hx-trigger bracket filter (e.g. ``every 3s[expr]``) into a
runtime function via ``new Function(expr)``. Our production CSP forbids
``'unsafe-eval'`` (finance app), so any bracket filter on a poll trigger
silently fail-opens AND spams the console with an ``EvalError`` per tick.

These tests assert the contract instead of running a browser:
- Neither polling partial uses a bracket filter on its ``hx-trigger``.
- ``afk.js`` wires the visibility / activity gating via ``hx-disable`` toggling
  on the poll panels (htmx evaluates this attribute dynamically per tick).

A full end-to-end verification that the CSP error no longer fires requires a
running local container with the real nginx CSP — covered by the smoke
section of the plan, not by unit tests.
"""

from __future__ import annotations

from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (SERVICE_ROOT / rel).read_text(encoding="utf-8")


def test_sync_progress_panel_has_no_bracket_filter_on_hx_trigger() -> None:
    content = _read("templates/partials/sync_progress_panel.html")
    assert 'hx-trigger="every 3s"' in content
    # The old bracket filter must not be present.
    assert "every 3s[" not in content
    assert "window.__userActive" not in content


def test_statement_wait_panel_has_no_bracket_filter_on_hx_trigger() -> None:
    content = _read("templates/partials/statement_wait_panel.html")
    assert 'hx-trigger="every 3s"' in content
    assert "every 3s[" not in content
    assert "window.__userActive" not in content


def test_afk_js_targets_both_poll_panels() -> None:
    content = _read("static/assets/js/afk.js")
    # Selector must cover both polling panels.
    assert "#sync-progress-panel" in content
    assert "#statement-reconcile-not-ready" in content
    # Gating must use hx-disable (evaluated at every tick) not a trigger filter.
    assert "hx-disable" in content


def test_afk_js_reapplies_on_visibility_and_after_swap() -> None:
    content = _read("static/assets/js/afk.js")
    # Visibility change must re-evaluate.
    assert "visibilitychange" in content
    # HTMX outerHTML swaps wipe the attribute; afk must re-apply on afterSwap.
    assert "htmx:afterSwap" in content
