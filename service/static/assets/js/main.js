import { buildCsrfUrlEncodedBody } from "./csrf.js";

const COOKIE_CONSENT_COOKIE_NAME = "cookie_consent";
const SESSION_IS_SET_COOKIE_NAME = "session_is_set";
const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;
const NAVBAR_SCROLLED_CLASS = "navbar-scrolled";

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

const setCookie = (cookieName, value, maxAgeSeconds) => {
  document.cookie = `${cookieName}=${value}; path=/; max-age=${maxAgeSeconds}; SameSite=Lax`;
};

const clearCookie = (cookieName) => {
  document.cookie = `${cookieName}=; path=/; max-age=0; SameSite=Lax`;
};

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

const redirectForUnauthorizedResponse = async (response, fallbackUrl) => {
  if (response.redirected && response.url.includes("/login")) {
    clearCookie(SESSION_IS_SET_COOKIE_NAME);
    window.location.href = response.url;
    return true;
  }

  if (response.status !== 401) {
    return false;
  }

  clearCookie(SESSION_IS_SET_COOKIE_NAME);
  try {
    const payload = await response.clone().json();
    if (payload && payload.error === "cookie_consent_required") {
      window.location.href = payload.redirect || `${window.location.origin}/cookies`;
      return true;
    }
  } catch (_error) {
    // Ignore JSON parse errors and fall through to the default auth redirect.
  }

  window.location.href = fallbackUrl;
  return true;
};

const updateNavbarScrollState = () => {
  const navbar = document.querySelector(".navbar");
  if (!navbar) return;
  navbar.classList.toggle(NAVBAR_SCROLLED_CLASS, window.scrollY > 8);
};

let stickyDockAbortController = null;

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
    setTimeout(handleResize, 120);
  });
};

const setupScrollProxy = () => {
  const wrapper = document.querySelector(".statement-table-wrapper");
  const proxy = document.getElementById("scroll-proxy");
  if (!wrapper || !proxy) return;

  const proxyInner = proxy.querySelector(".scroll-proxy-inner");
  if (!proxyInner) return;

  const dock = document.querySelector("[data-sticky-dock]");
  let syncing = false;

  /* Set the proxy's inner width to match the table's scroll width. */
  const syncWidths = () => {
    proxyInner.style.width = wrapper.scrollWidth + "px";
  };

  /* Determine whether the proxy should be visible:
     - table must have horizontal overflow
     - the native scrollbar (bottom of wrapper) must be off-screen */
  const syncVisibility = () => {
    const hasOverflow = wrapper.scrollWidth > wrapper.clientWidth;
    const nativeBarVisible = wrapper.getBoundingClientRect().bottom <= window.innerHeight;
    const shouldShow = hasOverflow && !nativeBarVisible;

    proxy.classList.toggle("is-visible", shouldShow);

    /* Align proxy horizontally with the table wrapper. */
    const wrapperRect = wrapper.getBoundingClientRect();
    proxy.style.left = wrapperRect.left + "px";
    proxy.style.width = wrapperRect.width + "px";

    /* Position above the sticky dock when it is visible. */
    if (dock && dock.classList.contains("is-visible")) {
      const dockHeight = dock.offsetHeight;
      proxy.style.bottom = "calc(" + (dockHeight + 8) + "px + 1rem + env(safe-area-inset-bottom))";
    } else {
      proxy.style.bottom = "";
    }
  };

  /* Bidirectional scroll sync with a guard flag. */
  proxy.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true;
    wrapper.scrollLeft = proxy.scrollLeft;
    syncing = false;
  });

  wrapper.addEventListener("scroll", () => {
    if (syncing) return;
    syncing = true;
    proxy.scrollLeft = wrapper.scrollLeft;
    syncing = false;
  });

  window.addEventListener("scroll", syncVisibility, { passive: true });
  window.addEventListener("resize", () => {
    syncWidths();
    syncVisibility();
  });

  syncWidths();
  syncVisibility();
};

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

  // Clean up DOM after hidden.
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
  // Strip notification params from URL.
  if (params.has("logged_out")) {
    params.delete("logged_out");
    const clean = params.toString();
    const newUrl = window.location.pathname + (clean ? `?${clean}` : "");
    window.history.replaceState({}, "", newUrl);
  }
};

const setupPaginationJump = () => {
  document.querySelectorAll("[data-pagination-jump]").forEach((container) => {
    const toggle = container.querySelector("[data-pagination-jump-toggle]");
    const popover = container.querySelector("[data-pagination-jump-popover]");
    if (!toggle || !popover) return;

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      const isOpen = popover.hasAttribute("data-open");
      closeAllPaginationPopovers();
      if (!isOpen) {
        popover.setAttribute("data-open", "");
        toggle.setAttribute("aria-expanded", "true");
      }
    });
  });

  document.addEventListener("click", closeAllPaginationPopovers);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAllPaginationPopovers();
  });
};

const closeAllPaginationPopovers = () => {
  document.querySelectorAll("[data-pagination-jump-popover][data-open]").forEach((p) => {
    p.removeAttribute("data-open");
  });
  document.querySelectorAll("[data-pagination-jump-toggle]").forEach((t) => {
    t.setAttribute("aria-expanded", "false");
  });
};

window.addEventListener("load", () => {
  updateNavbarScrollState();
  window.addEventListener("scroll", updateNavbarScrollState, { passive: true });
  setupCookieConsentButton();
  updateNavbarAuthLink();
  checkQueryParamToasts();
  setupStickyActionDocks();
  setupScrollProxy();
  setupPaginationJump();

  // Scroll-reveal animations
  document.querySelectorAll('.reveal, .reveal-subtle').forEach(el => {
    new IntersectionObserver((entries, obs) => {
      entries.forEach(e => {
        if (e.isIntersecting) {
          e.target.classList.add('visible');
          obs.unobserve(e.target);
        }
      });
    }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' }).observe(el);
  });

  if (window.location.pathname === "/tenant_management") {
      // Event listeners to check for inactivity 
			document.addEventListener("mousemove", throttle(resetActivityTimer, 1000)); // 1000ms delay between events
			document.addEventListener("keydown", throttle(resetActivityTimer, 1000));
			document.addEventListener("mousedown", throttle(resetActivityTimer, 1000));
			document.addEventListener("scroll", throttle(resetActivityTimer, 1000));

    handleCheckSync();
    setInterval(handleCheckSync, 30000);

    document.querySelectorAll(".sync-btn").forEach((btn) => {
      btn.addEventListener("click", () => handleSyncClick(btn));
    });
  }
});

// #region AFK Checker
let isPolling = true; // Track if long polling is active
let userActive = true; // Track user activity
let activityTimeout; // Timeout for inactivity
const INACTIVITY_TIME = 60000; // 60 seconds inactivity threshold

function handleCheckSync() {
	if (!isPolling) return;
	callGetTenantStatusesAPI()
	.then(data => {
		updateSyncStatuses(data);
	})
}

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

async function handleSyncClick(button) {
  const tenantId = button.dataset.tenantId;
  if (!tenantId) return;

  button.disabled = true;
  button.classList.add("disabled");
  toggleTenantSyncStatus(tenantId, true);

  try {
    const response = await fetch(`/api/tenants/${encodeURIComponent(tenantId)}/sync`, {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
      },
      body: buildCsrfUrlEncodedBody(),
      credentials: "same-origin",
    });

    if (await redirectForUnauthorizedResponse(response, `${window.location.origin}/login`)) {
      return;
    }

    if (!response.ok && response.status !== 202) {
      throw new Error(`HTTP ${response.status}`);
    }

  } catch (error) {
    button.disabled = false;
    button.classList.remove("disabled");
    toggleTenantSyncStatus(tenantId, false);
  }
}

function toggleTenantSyncStatus(tenantId, syncing) {
  const statusEl = document.querySelector(`.tenant-sync-status[data-tenant-id="${tenantId}"]`);
  if (statusEl) {
    statusEl.classList.toggle("d-none", !syncing);
    statusEl.setAttribute("data-syncing", syncing ? "true" : "false");
    const labelEl = statusEl.querySelector("span:last-child");
    if (labelEl && !labelEl.textContent.trim()) {
      labelEl.textContent = "Syncing";
    }
  }
}

// Throttle events to reduce loads of unnecessary resetActivityTimer function calls
const throttle = (func, limit) => {
    let inThrottle;
    return function() {
        if (!inThrottle) {
            func();
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
};

// Activity detection function
const resetActivityTimer = () => {
    clearTimeout(activityTimeout);

	// this block only runs if they were AFK and now they're active again
	if (!userActive) {
		userActive = true;
		if (!isPolling) isPolling = true; // Start making API calls
		handleCheckSync();
	}

    activityTimeout = setTimeout(() => {
        userActive = false;
        isPolling = false; // Stop making API calls
    }, INACTIVITY_TIME);
};
// #endregion

// #region APIS
async function callGetTenantStatusesAPI() {
	const baseUrl = window.location.origin;
    const response = await fetch(`${baseUrl}/api/tenant-statuses`, {
			method: 'GET', // Specify the request method
			headers: {
				'Content-Type': 'application/json', // Indicate that we're sending JSON data
			},
		});

		if (await redirectForUnauthorizedResponse(response, `${baseUrl}/login`)) {
			return;
		}

	if (!response.ok) {
		throw new Error('Network response was not ok');
	}
	const data = await response.json();
	return data;
}
// #endregion
