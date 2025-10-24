window.addEventListener("load", () => {
  if (window.location.pathname === "/home") {
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
      headers: { "Accept": "application/json" },
      credentials: "same-origin",
    });

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

	if (response.status === 401) {
		window.location.href = `${baseUrl}/status`; // Force re-login
		return;
	}

	if (!response.ok) {
		throw new Error('Network response was not ok');
	}
	const data = await response.json();
	return data;
}
// #endregion
