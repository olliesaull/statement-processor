/**
 * tenant-card-local-time.js
 *
 * Rewrites <time data-last-sync-ms="..."> elements inside the tenant
 * management panel to the user's local time/locale, so the card stops
 * showing UTC when the browser is in a different timezone.
 *
 * Runs on two triggers:
 *   1. DOMContentLoaded — for the initial server-rendered page.
 *   2. htmx:beforeSwap — the 3s poll returns fresh UTC fallback text;
 *      we rewrite the detached HTML *before* htmx swaps it in, so no
 *      UTC flicker paints.
 *
 * Scoped to target.id === "sync-progress-panel" so unrelated swaps
 * (statements, banners, etc.) are ignored.
 *
 * Kept separate from tenant-card-detail.js, which also listens on
 * htmx:beforeSwap for a different concern (detail-open state).
 */

const FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

function rewriteIn(root) {
  if (!root) return;
  const nodes = root.querySelectorAll("time[data-last-sync-ms]");
  nodes.forEach((node) => {
    const raw = node.getAttribute("data-last-sync-ms");
    const epochMs = Number(raw);
    if (!Number.isFinite(epochMs) || epochMs <= 0) return;
    node.textContent = FORMATTER.format(new Date(epochMs));
  });
}

// Initial page render + any non-htmx navigations back to /tenant_management.
document.addEventListener("DOMContentLoaded", () => {
  rewriteIn(document.getElementById("sync-progress-panel"));
});

// HTMX poll swap — rewrite the detached HTML BEFORE it's injected so the
// UTC fallback text never paints. Mirrors tenant-card-detail.js's pattern.
document.addEventListener("htmx:beforeSwap", (evt) => {
  const target = evt.detail && evt.detail.target;
  if (!target || target.id !== "sync-progress-panel") return;
  const tmp = document.createElement("div");
  tmp.innerHTML = evt.detail.serverResponse;
  rewriteIn(tmp);
  evt.detail.serverResponse = tmp.innerHTML;
});
