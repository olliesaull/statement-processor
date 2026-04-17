/**
 * afk.js — Sets window.__userActive based on recent user activity.
 *
 * Replaces the ~250-line tenant-sync.js polling module. HTMX triggers in the
 * sync-progress partials gate on `window.__userActive && !document.hidden` so
 * polling stops when the user walks away, without any bespoke polling loop.
 *
 * Resets to "active" on any mouse/keyboard/scroll event; flips to "inactive"
 * after `INACTIVITY_MS` of silence. Scroll listener is passive to keep the
 * main thread free during long lists.
 */

const INACTIVITY_MS = 60_000;

window.__userActive = true;

let inactivityTimer = null;

const markActive = () => {
    window.__userActive = true;
    if (inactivityTimer !== null) {
        clearTimeout(inactivityTimer);
    }
    inactivityTimer = window.setTimeout(() => {
        window.__userActive = false;
    }, INACTIVITY_MS);
};

// Kick off the timer immediately so a fully-idle tab goes inactive after the window.
markActive();

// Use capture + passive for scroll/touch so listeners don't block main-thread work.
document.addEventListener("mousemove", markActive, { passive: true });
document.addEventListener("keydown", markActive);
document.addEventListener("scroll", markActive, { passive: true });
document.addEventListener("touchstart", markActive, { passive: true });
