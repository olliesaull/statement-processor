/**
 * afk.js — Gate HTMX polling on user activity and tab visibility.
 *
 * The prior implementation used an hx-trigger bracket filter
 * ("every 3s[(window.__userActive ?? true) && !document.hidden]") which htmx
 * compiles via `new Function()`. Our CSP forbids 'unsafe-eval', so every poll
 * tick raised a CSP EvalError and — worse — the gate silently fail-opened,
 * leaving polling running unconditionally.
 *
 * This module instead toggles the `hx-disable` attribute on the polling panels
 * whenever visibility or activity state changes. htmx checks `hx-disable` at
 * every trigger fire, so a disabled panel cleanly skips its 3s tick; removing
 * the attribute resumes polling on the next natural tick.
 */

const INACTIVITY_MS = 60_000;
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

// Initial sync + re-apply after every HTMX swap (outerHTML swaps wipe the
// attribute since the panel element is replaced wholesale).
document.addEventListener("DOMContentLoaded", updatePollingState);
document.addEventListener("htmx:afterSwap", updatePollingState);
