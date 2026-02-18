const getCsrfToken = () => {
  const meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute("content") : "";
};

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

const setupStickyActionDocks = () => {
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

    // Show the fixed dock only on pages where the real action bar starts below the first viewport.
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
    } else {
      const handleScrollFallback = () => syncDockVisibility();
      window.addEventListener("scroll", handleScrollFallback, { passive: true });
      handleScrollFallback();
    }

    const handleResize = () => {
      shouldEnableDock = isAnchorBelowInitialViewport();
      syncDockVisibility();
    };
    window.addEventListener("resize", handleResize);

    syncDockVisibility();
    setTimeout(handleResize, 120);
  });
};

window.addEventListener("load", () => {
  updateNavbarScrollState();
  window.addEventListener("scroll", updateNavbarScrollState, { passive: true });
  setupCookieConsentButton();
  updateNavbarAuthLink();
  setupStickyActionDocks();

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
        syncButton.disabled = !!showStatus;
        syncButton.classList.toggle("disabled", !!showStatus);
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
        "X-CSRFToken": getCsrfToken(),
      },
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
