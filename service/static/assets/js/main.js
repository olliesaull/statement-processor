window.addEventListener("load", () => {
  if (window.location.pathname === "/") {
    handleCheckSync();
    setInterval(handleCheckSync, 30000);
  }
});

// #region AFK Checker
let isPolling = true; // Track if long polling is active
let userActive = true; // Track user activity
let activityTimeout; // Timeout for inactivity
const INACTIVITY_TIME = 60000; // 60 seconds inactivity threshold

function handleCheckSync() {
	if (!isPolling) return;
	callCheckSyncAPI()
	.then(data => {
		updateSyncStatuses(data);
	})
}

function updateSyncStatuses(data) {
	const syncingTenants = Array.isArray(data?.syncingTenants) ? data.syncingTenants : [];
	console.log("Syncing tenant IDs:", syncingTenants);

	syncingTenants.forEach((tenantId) => {
		if (!tenantId) return;
		const row = document.getElementById(`row-${tenantId}`);
		if (row) {
			console.log("Syncing row element:", row);
		}
	});
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
async function callCheckSyncAPI() {
	const baseUrl = window.location.origin;
    const response = await fetch(`${baseUrl}/api/tenants/sync-status`, {
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
