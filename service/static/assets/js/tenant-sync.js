/**
 * tenant-sync.js — Tenant sync polling and AFK detection for the tenant management page.
 *
 * Responsibilities:
 *   - Poll /api/tenant-statuses at a regular interval and update the UI.
 *   - Detect user inactivity (AFK) and pause polling to save server requests.
 *   - Handle sync button clicks (POST /api/tenants/:id/sync).
 *
 * Loaded as an ES module, imported by main.js.
 * initTenantSync() receives its shared dependencies as arguments to avoid circular imports.
 */

// ---------- AFK / polling state ----------

/** Whether the long-poll loop is currently running. */
let isPolling = true;

/** Whether the user has been active recently (resets on mouse/key/scroll events). */
let userActive = true;

/** Handle for the inactivity setTimeout so it can be cleared on activity. */
let activityTimeout;

/** Milliseconds of silence before the user is considered AFK. */
const INACTIVITY_TIME = 60000;

// ---------- Sync status helpers ----------

/**
 * Fetch the current sync status for all connected tenants from the server.
 * Returns the parsed JSON body, or undefined if the request redirected to login.
 *
 * @param {Function} redirectForUnauthorizedResponse - Shared auth-redirect helper.
 * @returns {Promise<object|undefined>}
 */
async function callGetTenantStatusesAPI(redirectForUnauthorizedResponse) {
    const baseUrl = window.location.origin;
    const response = await fetch(`${baseUrl}/api/tenant-statuses`, {
        method: "GET",
        headers: {
            "Content-Type": "application/json",
        },
    });

    if (await redirectForUnauthorizedResponse(response, `${baseUrl}/login`)) {
        return;
    }

    if (!response.ok) {
        throw new Error("Network response was not ok");
    }
    const data = await response.json();
    return data;
}

/**
 * Apply a status map (tenantId → statusString) to the tenant sync status elements
 * in the DOM, toggling loading/syncing indicators and enabling/disabling sync buttons.
 *
 * @param {object} data - Map of tenantId → status string (e.g. "LOADING", "SYNCING").
 */
function updateSyncStatuses(data) {
    if (!data || typeof data !== "object") {
        return;
    }

    const statusMap = data;
    document.querySelectorAll(".tenant-sync-status").forEach((statusEl) => {
        const tenantId = statusEl.dataset.tenantId;
        const rawStatus = tenantId ? statusMap[tenantId] : undefined;
        const normalizedStatus = typeof rawStatus === "string" ? rawStatus.toUpperCase() : "";
        const isLoading = normalizedStatus === "LOADING";
        const isSyncing = normalizedStatus === "SYNCING";
        // Defensive: ERASED and LOAD_INCOMPLETE are not expected in the UI (disconnected
        // tenants are removed from the session) but disable controls if they appear.
        const isInactive = normalizedStatus === "ERASED" || normalizedStatus === "LOAD_INCOMPLETE";
        const showStatus = isLoading || isSyncing;

        statusEl.classList.toggle("d-none", !showStatus);
        statusEl.setAttribute("data-syncing", isSyncing ? "true" : "false");
        statusEl.setAttribute("data-status", normalizedStatus || "");

        const labelEl = statusEl.querySelector("span:last-child");
        if (labelEl) {
            labelEl.textContent = isLoading ? "Loading" : isSyncing ? "Syncing" : "";
        }

        const row = tenantId ? document.getElementById(`row-${tenantId}`) : null;
        if (row) {
            const syncButton = row.querySelector(".sync-btn");
            if (syncButton) {
                syncButton.disabled = !!(showStatus || isInactive);
                syncButton.classList.toggle("disabled", !!(showStatus || isInactive));
            }
        }

        if (tenantId) {
            const row = document.getElementById(`row-${tenantId}`);
            if (row) {
                row.setAttribute("data-tenant-status", normalizedStatus || "");
            }
        }
    });
}

/**
 * Immediately update a single tenant's sync indicator in the DOM without waiting
 * for the next poll cycle. Used when the user clicks the sync button to give
 * instant visual feedback.
 *
 * @param {string} tenantId
 * @param {boolean} syncing - true to show the indicator, false to hide it.
 */
function toggleTenantSyncStatus(tenantId, syncing) {
    const statusEl = document.querySelector(`.tenant-sync-status[data-tenant-id="${tenantId}"]`);
    if (statusEl) {
        statusEl.classList.toggle("d-none", !syncing);
        statusEl.setAttribute("data-syncing", syncing ? "true" : "false");
        const labelEl = statusEl.querySelector("span:last-child");
        // Only overwrite the label if it is blank — avoids clobbering "Loading".
        if (labelEl && !labelEl.textContent.trim()) {
            labelEl.textContent = "Syncing";
        }
    }
}

/**
 * Handle a click on a sync button for a specific tenant.
 * Disables the button immediately, fires the POST, and re-enables on error.
 * On success the next poll cycle will reflect the updated status.
 *
 * @param {HTMLButtonElement} button
 * @param {Function} redirectForUnauthorizedResponse - Shared auth-redirect helper.
 * @param {Function} buildCsrfUrlEncodedBody - Returns a URLSearchParams with the CSRF token.
 */
async function handleSyncClick(button, redirectForUnauthorizedResponse, buildCsrfUrlEncodedBody) {
    const tenantId = button.dataset.tenantId;
    if (!tenantId) return;

    button.disabled = true;
    button.classList.add("disabled");
    toggleTenantSyncStatus(tenantId, true);

    try {
        const response = await fetch(`/api/tenants/${encodeURIComponent(tenantId)}/sync`, {
            method: "POST",
            headers: {
                Accept: "application/json",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
            body: buildCsrfUrlEncodedBody(),
            credentials: "same-origin",
        });

        if (await redirectForUnauthorizedResponse(response, `${window.location.origin}/login`)) {
            return;
        }

        // 202 Accepted is the expected success response for an async sync job.
        if (!response.ok && response.status !== 202) {
            throw new Error(`HTTP ${response.status}`);
        }
    } catch (_error) {
        // Restore controls so the user can retry.
        button.disabled = false;
        button.classList.remove("disabled");
        toggleTenantSyncStatus(tenantId, false);
    }
}

// ---------- AFK detection ----------

/**
 * Throttle a function so it fires at most once per `limit` milliseconds.
 * Used to avoid flooding resetActivityTimer on rapid mouse/scroll events.
 *
 * @param {Function} func
 * @param {number} limit - Minimum interval between calls in milliseconds.
 * @returns {Function}
 */
const throttle = (func, limit) => {
    let inThrottle;
    return function () {
        if (!inThrottle) {
            func();
            inThrottle = true;
            setTimeout(() => (inThrottle = false), limit);
        }
    };
};

// ---------- Public init ----------

/**
 * Wire up sync polling, AFK detection, and sync button listeners.
 * Called by main.js on window load, only when on the tenant management page.
 *
 * @param {Function} redirectForUnauthorizedResponse - Shared auth-redirect helper from main.js.
 * @param {Function} buildCsrfUrlEncodedBody - CSRF body builder from csrf.js.
 */
export function initTenantSync(redirectForUnauthorizedResponse, buildCsrfUrlEncodedBody) {
    /**
     * Fetch tenant statuses and push them to the DOM.
     * Skips the fetch entirely when polling has been paused due to AFK.
     */
    const handleCheckSync = () => {
        if (!isPolling) return;
        callGetTenantStatusesAPI(redirectForUnauthorizedResponse).then((data) => {
            updateSyncStatuses(data);
        });
    };

    /**
     * Reset the inactivity timer whenever the user makes an input gesture.
     * If they were previously AFK, resumes polling immediately.
     */
    const resetActivityTimer = () => {
        clearTimeout(activityTimeout);

        // Resume polling if the user was AFK and is now active again.
        if (!userActive) {
            userActive = true;
            if (!isPolling) isPolling = true;
            handleCheckSync();
        }

        activityTimeout = setTimeout(() => {
            userActive = false;
            isPolling = false; // Pause API calls while the user is idle.
        }, INACTIVITY_TIME);
    };

    // Activity events — throttled to 1 call per second each.
    document.addEventListener("mousemove", throttle(resetActivityTimer, 1000));
    document.addEventListener("keydown", throttle(resetActivityTimer, 1000));
    document.addEventListener("mousedown", throttle(resetActivityTimer, 1000));
    document.addEventListener("scroll", throttle(resetActivityTimer, 1000));

    // Initial status check, then repeat every 30 seconds.
    handleCheckSync();
    setInterval(handleCheckSync, 30000);

    // Attach sync button handlers, passing shared deps through the closure.
    document.querySelectorAll(".sync-btn").forEach((btn) => {
        btn.addEventListener("click", () =>
            handleSyncClick(btn, redirectForUnauthorizedResponse, buildCsrfUrlEncodedBody),
        );
    });
}
