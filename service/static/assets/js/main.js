/**
 * main.js — Application entry point / glue module.
 *
 * Bootstraps all page-level behaviour on window load:
 *   - Navbar scroll state and auth link
 *   - Cookie consent button
 *   - Query-param-driven toast notifications
 *   - Sticky action docks
 *   - Scroll-reveal animations
 *   - Pagination jump popover
 *
 * Also registers global HTMX event handlers that run for every content swap,
 * including a CSRF-token injector so HTMX POSTs authenticate like form posts.
 *
 * Extracted sub-modules (imported as ES modules):
 *   - scroll-proxy.js  — sticky horizontal scrollbar proxy
 *   - afk.js           — sets window.__userActive for HTMX hx-trigger gating
 */

import { setupScrollProxy } from "./scroll-proxy.js";
// afk.js has no exports; importing it wires the document listeners that toggle
// window.__userActive, which sync-progress hx-trigger expressions gate on.
import "./afk.js";

// ---------- Constants ----------

const COOKIE_CONSENT_COOKIE_NAME = "cookie_consent";
const SESSION_IS_SET_COOKIE_NAME = "session_is_set";
const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;
const NAVBAR_SCROLLED_CLASS = "navbar-scrolled";

// ---------- Cookie utilities ----------

/**
 * Read a cookie value by name from document.cookie.
 * Returns an empty string if the cookie is absent.
 *
 * @param {string} cookieName
 * @returns {string}
 */
const getCookie = (cookieName) => {
    const cookies = document.cookie ? document.cookie.split(";") : [];
    for (const cookie of cookies) {
        const [rawKey, ...rawValue] = cookie.split("=");
        if ((rawKey || "").trim() === cookieName) {
            return rawValue.join("=");
        }
    }
    return "";
};

/**
 * Set a cookie with SameSite=Lax and an explicit max-age.
 *
 * @param {string} cookieName
 * @param {string} value
 * @param {number} maxAgeSeconds
 */
const setCookie = (cookieName, value, maxAgeSeconds) => {
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${cookieName}=${value}; path=/; max-age=${maxAgeSeconds}; SameSite=Lax${secure}`;
};

// ---------- Navbar ----------

/**
 * Toggle the login/logout label and href on the auth nav link based on whether
 * cookie consent has been given and the session cookie is present.
 *
 * The session cookie is client-readable and used only to drive UI — it does not
 * grant access. Server-side session validation is always authoritative.
 */
const updateNavbarAuthLink = () => {
    const authLink = document.getElementById("nav-auth-link");
    if (!authLink) return;

    const hasConsent = getCookie(COOKIE_CONSENT_COOKIE_NAME) === "true";
    const isSessionSet = hasConsent && getCookie(SESSION_IS_SET_COOKIE_NAME) === "true";
    const loginHref = authLink.dataset.loginHref || "/login";
    const logoutHref = authLink.dataset.logoutHref || "/logout";
    const loginLabel = authLink.dataset.loginLabel || "Login";
    const logoutLabel = authLink.dataset.logoutLabel || "Logout";

    authLink.href = isSessionSet ? logoutHref : loginHref;
    authLink.textContent = isSessionSet ? logoutLabel : loginLabel;
    const isActive = window.location.pathname === (isSessionSet ? logoutHref : loginHref);
    authLink.classList.toggle("active", isActive);
};

/** Add or remove the scrolled class on the navbar based on vertical scroll position. */
const updateNavbarScrollState = () => {
    const navbar = document.querySelector(".navbar");
    if (!navbar) return;
    navbar.classList.toggle(NAVBAR_SCROLLED_CLASS, window.scrollY > 8);
};

// ---------- Cookie consent page ----------

/**
 * On the /cookies page, intercept the consent button click to set the consent
 * cookie before following the redirect href.
 */
const setupCookieConsentButton = () => {
    if (window.location.pathname !== "/cookies") return;
    const consentButton = document.getElementById("cookie-accept-button");
    if (!consentButton) return;

    consentButton.addEventListener("click", (event) => {
        event.preventDefault();
        setCookie(COOKIE_CONSENT_COOKIE_NAME, "true", ONE_YEAR_SECONDS);
        window.location.href = consentButton.href;
    });
};


// ---------- Toast notifications ----------

/**
 * Show a transient toast notification that auto-dismisses after 3 seconds.
 * Uses the Bootstrap Toast component with accent styling matching banners.
 *
 * @param {string} message - Text to display.
 * @param {"success"|"danger"|"info"|"warning"} type - Alert colour variant.
 */
const showToast = (message, type = "info") => {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toastEl = document.createElement("div");
    toastEl.className = `toast align-items-center border-0 toast-${type}`;
    toastEl.setAttribute("role", "alert");
    toastEl.setAttribute("aria-live", "assertive");
    toastEl.setAttribute("aria-atomic", "true");
    toastEl.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">${message}</div>
      <button type="button" class="btn-close btn-close-dark me-2 m-auto" data-bs-dismiss="toast" aria-label="Close"></button>
    </div>
  `;

    container.appendChild(toastEl);
    const bsToast = new bootstrap.Toast(toastEl, { delay: 3000 });
    bsToast.show();

    // Clean up DOM node after Bootstrap hides the toast.
    toastEl.addEventListener("hidden.bs.toast", () => toastEl.remove());
};

/**
 * Check for query-param-driven notifications on page load and strip them
 * from the URL so refreshing doesn't re-show the toast.
 */
const checkQueryParamToasts = () => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("logged_out") === "1") {
        showToast("All tenants disconnected. You have been logged out.", "info");
    }
    // Strip notification params from URL without triggering a page reload.
    if (params.has("logged_out")) {
        params.delete("logged_out");
        const clean = params.toString();
        const newUrl = window.location.pathname + (clean ? `?${clean}` : "");
        window.history.replaceState({}, "", newUrl);
    }
};

// ---------- Sticky action docks ----------

/** AbortController for sticky dock scroll/resize listeners; replaced on each HTMX swap. */
let stickyDockAbortController = null;

/**
 * Attach IntersectionObserver (or scroll fallback) to each [data-sticky-dock] element
 * to show/hide it when its anchor target scrolls off-screen.
 *
 * Aborts previous listeners before re-registering so HTMX swaps don't accumulate
 * duplicate handlers.
 */
const setupStickyActionDocks = () => {
    if (stickyDockAbortController) {
        stickyDockAbortController.abort();
    }
    stickyDockAbortController = new AbortController();
    const signal = stickyDockAbortController.signal;

    const docks = document.querySelectorAll("[data-sticky-dock]");
    if (!docks.length) return;

    docks.forEach((dock) => {
        const targetSelector = dock.dataset.stickyTarget;
        if (!targetSelector) return;

        const anchor = document.querySelector(targetSelector);
        if (!anchor) return;

        // Only enable the dock if the anchor starts below the initial viewport
        // (i.e. the user must scroll to reach the actions).
        const isAnchorBelowInitialViewport = () => {
            const anchorDocumentTop = anchor.getBoundingClientRect().top + window.scrollY;
            return anchorDocumentTop >= window.innerHeight;
        };
        let shouldEnableDock = isAnchorBelowInitialViewport();

        const syncDockVisibility = () => {
            const anchorRect = anchor.getBoundingClientRect();
            const anchorBelowViewport = anchorRect.top >= window.innerHeight;
            const shouldShow = shouldEnableDock && anchorBelowViewport;
            dock.classList.toggle("is-visible", shouldShow);
            dock.setAttribute("aria-hidden", shouldShow ? "false" : "true");
        };

        if ("IntersectionObserver" in window) {
            const observer = new IntersectionObserver(
                () => {
                    syncDockVisibility();
                },
                { threshold: 0.01 },
            );
            observer.observe(anchor);
            signal.addEventListener("abort", () => observer.disconnect());
        } else {
            window.addEventListener("scroll", syncDockVisibility, { passive: true, signal });
            syncDockVisibility();
        }

        const handleResize = () => {
            shouldEnableDock = isAnchorBelowInitialViewport();
            syncDockVisibility();
        };
        window.addEventListener("resize", handleResize, { signal });

        syncDockVisibility();
        // Brief delay to let the browser settle layout before the initial measurement.
        setTimeout(handleResize, 120);
    });
};

// ---------- Pagination jump popover ----------

/** AbortController for pagination jump listeners; replaced on each HTMX swap. */
let paginationJumpAbortController = null;

/** Close all open pagination jump popovers and reset their toggles. */
const closeAllPaginationPopovers = () => {
    document.querySelectorAll("[data-pagination-jump-popover][data-open]").forEach((p) => {
        p.removeAttribute("data-open");
    });
    document.querySelectorAll("[data-pagination-jump-toggle]").forEach((t) => {
        t.setAttribute("aria-expanded", "false");
    });
};

/**
 * Attach click / keyboard listeners for the pagination jump popovers.
 * Aborts and re-registers on HTMX swaps.
 */
const setupPaginationJump = () => {
    if (paginationJumpAbortController) {
        paginationJumpAbortController.abort();
    }
    paginationJumpAbortController = new AbortController();
    const signal = paginationJumpAbortController.signal;

    document.querySelectorAll("[data-pagination-jump]").forEach((container) => {
        const toggle = container.querySelector("[data-pagination-jump-toggle]");
        const popover = container.querySelector("[data-pagination-jump-popover]");
        if (!toggle || !popover) return;

        toggle.addEventListener(
            "click",
            (e) => {
                e.stopPropagation();
                const isOpen = popover.hasAttribute("data-open");
                closeAllPaginationPopovers();
                if (!isOpen) {
                    popover.setAttribute("data-open", "");
                    toggle.setAttribute("aria-expanded", "true");
                }
            },
            { signal },
        );
    });

    document.addEventListener("click", closeAllPaginationPopovers, { signal });
    document.addEventListener(
        "keydown",
        (e) => {
            if (e.key === "Escape") closeAllPaginationPopovers();
        },
        { signal },
    );
};

// ---------- Window load ----------

window.addEventListener("load", () => {
    updateNavbarScrollState();
    window.addEventListener("scroll", updateNavbarScrollState, { passive: true });
    setupCookieConsentButton();
    updateNavbarAuthLink();
    checkQueryParamToasts();
    setupStickyActionDocks();
    setupScrollProxy();
    setupPaginationJump();

    // Scroll-reveal animations — observe each element once and unobserve after reveal.
    document.querySelectorAll(".reveal, .reveal-subtle").forEach((el) => {
        new IntersectionObserver(
            (entries, obs) => {
                entries.forEach((e) => {
                    if (e.isIntersecting) {
                        e.target.classList.add("visible");
                        obs.unobserve(e.target);
                    }
                });
            },
            { threshold: 0.1, rootMargin: "0px 0px -40px 0px" },
        ).observe(el);
    });

});

// ---------- HTMX event handlers ----------
// Registered outside the load handler so they are ready for any swap that fires
// before or after the initial page load event.

// Re-initialise UI components after HTMX swaps new content into the DOM.
document.addEventListener("htmx:afterSwap", () => {
    setupStickyActionDocks();
    setupScrollProxy();
    setupPaginationJump();
});

// Show a toast when an HTMX request fails.
document.addEventListener("htmx:responseError", () => {
    showToast("Something went wrong — please refresh the page.", "danger");
});

// Forward the server-issued CSRF token on every HTMX-originated request. The
// meta tag is emitted in base.html; swap targets behind @csrf_protect (sync +
// retry-sync) rely on this without per-template plumbing.
document.body.addEventListener("htmx:configRequest", (event) => {
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && event.detail && event.detail.headers) {
        event.detail.headers["X-CSRFToken"] = meta.content;
    }
});

// After a statement is deleted, refresh the count chips from the server.
document.addEventListener("listUpdated", () => {
    htmx.ajax("GET", "/statements/count" + window.location.search, { swap: "none" });
});
