/**
 * scroll-proxy.js — Sticky horizontal scrollbar proxy for wide tables.
 *
 * The statement detail page contains a wide comparison table that can overflow
 * horizontally. On desktop the native scrollbar appears at the bottom of the
 * table wrapper, which is often below the fold when the table is long. This
 * module mirrors the horizontal scroll position into a fixed-position proxy
 * scrollbar that stays visible at the bottom of the viewport.
 *
 * Visibility rules:
 *   - The proxy is only shown when the table has horizontal overflow AND the
 *     native scrollbar (the bottom edge of the table wrapper) is off-screen.
 *   - When the sticky action dock is visible, the proxy is pushed above it.
 *
 * Expected DOM:
 *   .statement-table-wrapper  — the scrollable table container
 *   #scroll-proxy             — the fixed proxy element
 *   .scroll-proxy-inner       — child of #scroll-proxy; width is set to match
 *                               the table's scrollWidth so the proxy is scrollable
 *   [data-sticky-dock]        — optional sticky dock whose height the proxy avoids
 *
 * Dependencies: none.
 */

/** AbortController for the current proxy setup; replaced on each HTMX swap. */
let scrollProxyAbortController = null;

/**
 * Initialise (or re-initialise) the scroll proxy.
 *
 * Aborts any previously registered listeners before setting up new ones so
 * HTMX page swaps don't accumulate duplicate handlers.
 */
export function setupScrollProxy() {
    if (scrollProxyAbortController) {
        scrollProxyAbortController.abort();
    }
    scrollProxyAbortController = new AbortController();
    const signal = scrollProxyAbortController.signal;

    const wrapper = document.querySelector(".statement-table-wrapper");
    const proxy = document.getElementById("scroll-proxy");
    if (!wrapper || !proxy) return;

    const proxyInner = proxy.querySelector(".scroll-proxy-inner");
    if (!proxyInner) return;

    const dock = document.querySelector("[data-sticky-dock]");
    // Guard flag so a scroll event on one element doesn't re-trigger the other.
    let syncing = false;

    /** Set the proxy's inner width to match the table's full scroll width. */
    const syncWidths = () => {
        proxyInner.style.width = wrapper.scrollWidth + "px";
    };

    /**
     * Determine whether the proxy should be visible:
     *   - table must have horizontal overflow
     *   - the native scrollbar (bottom of wrapper) must be off-screen
     * Also aligns the proxy horizontally and positions it above the sticky dock.
     */
    const syncVisibility = () => {
        const hasOverflow = wrapper.scrollWidth > wrapper.clientWidth;
        const nativeBarVisible = wrapper.getBoundingClientRect().bottom <= window.innerHeight;
        const shouldShow = hasOverflow && !nativeBarVisible;

        proxy.classList.toggle("is-visible", shouldShow);

        // Align proxy horizontally with the table wrapper.
        const wrapperRect = wrapper.getBoundingClientRect();
        proxy.style.left = wrapperRect.left + "px";
        proxy.style.width = wrapperRect.width + "px";

        // Position above the sticky dock when it is visible, accounting for
        // safe-area insets (notched devices).
        if (dock && dock.classList.contains("is-visible")) {
            const dockHeight = dock.offsetHeight;
            proxy.style.bottom = "calc(" + (dockHeight + 8) + "px + 1rem + env(safe-area-inset-bottom))";
        } else {
            proxy.style.bottom = "";
        }
    };

    // Bidirectional scroll sync — each handler skips re-entrancy via the syncing flag.
    proxy.addEventListener(
        "scroll",
        () => {
            if (syncing) return;
            syncing = true;
            wrapper.scrollLeft = proxy.scrollLeft;
            syncing = false;
        },
        { signal },
    );

    wrapper.addEventListener(
        "scroll",
        () => {
            if (syncing) return;
            syncing = true;
            proxy.scrollLeft = wrapper.scrollLeft;
            syncing = false;
        },
        { signal },
    );

    window.addEventListener("scroll", syncVisibility, { passive: true, signal });
    window.addEventListener(
        "resize",
        () => {
            syncWidths();
            syncVisibility();
        },
        { signal },
    );

    syncWidths();
    syncVisibility();
}
