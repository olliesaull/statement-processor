/**
 * tenant-card-detail.js
 *
 * Handles the expandable detail block on tenant cards on /tenant_management.
 *
 * Responsibilities:
 *   1. Toggle detail open/closed on button click (no Bootstrap collapse dep).
 *   2. Persist open detail IDs in sessionStorage.
 *   3. Pre-apply data-expanded="true" to the incoming HTML in htmx:beforeSwap
 *      so the 3s polling swap doesn't cause a close->reopen transition glitch.
 *
 * See docs/decisions/log.md entry dated 2026-04-22 ("Tenant card detail state
 * preservation across HTMX swaps") for why beforeSwap (not afterSwap) is used.
 */

const STORAGE_KEY = "tenantCardDetailsOpen";

function readOpenIds() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch (_err) {
    return new Set();
  }
}

function writeOpenIds(ids) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify([...ids]));
  } catch (_err) {
    /* sessionStorage unavailable - state lost on next swap; not fatal. */
  }
}

function applyToggleClick(event) {
  const btn = event.target.closest("[data-toggle-target]");
  if (!btn) return;
  const targetId = btn.getAttribute("data-toggle-target");
  const detail = document.getElementById(targetId);
  if (!detail) return;
  const expanded = btn.getAttribute("aria-expanded") === "true";
  const next = !expanded;
  btn.setAttribute("aria-expanded", String(next));
  detail.setAttribute("data-expanded", String(next));
  const open = readOpenIds();
  if (next) open.add(targetId);
  else open.delete(targetId);
  writeOpenIds(open);
}

function restoreOpenState(root = document) {
  const open = readOpenIds();
  open.forEach((id) => {
    const detail = root.querySelector(`#${CSS.escape(id)}`);
    const toggle = root.querySelector(`[data-toggle-target="${CSS.escape(id)}"]`);
    if (detail) detail.setAttribute("data-expanded", "true");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
  });
}

// Toggle click - delegated so it keeps working across HTMX swaps.
document.addEventListener("click", applyToggleClick);

// Initial render + full-page navigations back to /tenant_management.
document.addEventListener("DOMContentLoaded", () => restoreOpenState());

// Pre-apply open state to the incoming HTML BEFORE htmx swaps it in. Mutating
// the response string (not the live DOM after swap) means the new element's
// very first computed style is already max-height: 220px, so no 0 -> 220
// transition animation fires on each 3s poll.
document.addEventListener("htmx:beforeSwap", (evt) => {
  const target = evt.detail && evt.detail.target;
  if (!target || target.id !== "sync-progress-panel") return;
  const open = readOpenIds();
  if (open.size === 0) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = evt.detail.serverResponse;
  open.forEach((id) => {
    const detail = tmp.querySelector(`#${CSS.escape(id)}`);
    const toggle = tmp.querySelector(`[data-toggle-target="${CSS.escape(id)}"]`);
    if (detail) detail.setAttribute("data-expanded", "true");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
  });
  evt.detail.serverResponse = tmp.innerHTML;
});
