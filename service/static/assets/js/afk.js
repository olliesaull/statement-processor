/**
 * afk.js — Gate HTMX polling on user activity and tab visibility.
 *
 * Toggles the `hx-disable` attribute on the polling panels whenever visibility
 * or activity state changes. htmx checks `hx-disable` at every trigger fire,
 * so a disabled panel cleanly skips its 3s tick; removing the attribute
 * resumes polling on the next natural tick.
 *
 * History: the prior hx-trigger bracket filter relied on `new Function()`,
 * which our CSP forbids — see `docs/decisions/log.md` entry dated 2026-04-20
 * ("AFK / visibility gating via `hx-disable`...").
 */

const INACTIVITY_MS = 60_000;
// Add any new polling panel IDs here when introducing them — elements outside
// this selector stay ungated and will poll through visibility/AFK changes.
const POLL_PANEL_SELECTOR = "#sync-progress-panel, #statement-reconcile-not-ready";

window.__userActive = true;

let inactivityTimer = null;

function shouldPausePolling() {
    return document.hidden || window.__userActive === false;
}

function updatePollingState() {
    const paused = shouldPausePolling();
    document.querySelectorAll(POLL_PANEL_SELECTOR).forEach((el) => {
        if (paused) {
            el.setAttribute("hx-disable", "true");
        } else {
            el.removeAttribute("hx-disable");
        }
    });
}

function markActive() {
    const wasInactive = window.__userActive === false;
    window.__userActive = true;
    if (inactivityTimer !== null) {
        clearTimeout(inactivityTimer);
    }
    inactivityTimer = window.setTimeout(() => {
        window.__userActive = false;
        updatePollingState();
    }, INACTIVITY_MS);
    if (wasInactive) {
        // Leaving the inactive state — re-enable polling before the next 3s tick.
        updatePollingState();
    }
}

// Kick off the timer immediately so a fully-idle tab goes inactive after the window.
markActive();

document.addEventListener("mousemove", markActive, { passive: true });
document.addEventListener("keydown", markActive);
document.addEventListener("scroll", markActive, { passive: true });
document.addEventListener("touchstart", markActive, { passive: true });

document.addEventListener("visibilitychange", updatePollingState);

// Initial sync + re-apply after every HTMX swap of a polling panel (outerHTML
// swaps wipe the attribute since the panel element is replaced wholesale).
// Scoped to the polling panels so unrelated HTMX swaps elsewhere on the page
// don't trigger a full selector query.
document.addEventListener("DOMContentLoaded", updatePollingState);
document.addEventListener("htmx:afterSwap", (evt) => {
    const target = evt.detail && evt.detail.target;
    if (target && target.matches && target.matches(POLL_PANEL_SELECTOR)) {
        updatePollingState();
    }
});
