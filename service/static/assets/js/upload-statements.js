/**
 * upload-statements.js — Client-side logic for the statement upload page.
 *
 * Responsibilities:
 *   - Add/remove file upload rows dynamically.
 *   - Estimate PDF page counts client-side by parsing raw bytes (fast, no upload needed).
 *   - Run a server-side preflight check that validates files and checks page availability.
 *   - Gate form submission behind a successful preflight so the server never receives
 *     files it cannot process (insufficient pages, corrupt PDFs, etc.).
 *
 * Dependencies:
 *   - csrf.js (provides appendCsrfTokenToFormData)
 */

import { appendCsrfTokenToFormData } from "./csrf.js";

// ---------- DOM references (grabbed once at module load) ----------

const uploadForm = document.getElementById("upload-statements-form");
const rowsContainer = document.getElementById("rows");
const addRowButton = document.getElementById("add-row");
const uploadPageSummary = document.getElementById("upload-page-summary");
const uploadPreflightSummary = document.getElementById("upload-preflight-summary");
const submitButton = uploadForm ? uploadForm.querySelector('button[type="submit"]') : null;

// ---------- Preflight state ----------

/**
 * Tracks the most recent preflight outcome so that form submission can
 * proceed without re-running the check if nothing has changed.
 */
const preflightState = {
    /** Fingerprint of the file set when the last check ran. */
    signature: "",
    /** Whether the server said the upload can proceed. */
    canSubmit: false,
    /** Whether a preflight request is currently in flight. */
    pending: false,
};

/** AbortController for the in-flight preflight request, if any. */
let activePreflightController = null;

/**
 * Monotonically incrementing counter. Each new request gets a higher number so
 * stale responses from aborted/superseded requests can be discarded.
 */
let preflightRequestSequence = 0;

/**
 * Flag that allows one unguarded form submission after a successful preflight.
 * After the server round-trip confirms canSubmit, main.js calls requestSubmit()
 * and sets this to true so the submit handler doesn't intercept its own request.
 */
let allowDirectSubmit = false;

// ---------- Row creation ----------

/**
 * Create the page-count cell HTML for a new upload row.
 * Initial state is "idle" — the cell updates as the user picks a file.
 *
 * @returns {string} HTML string for the <td> element.
 */
function createPageCountCell() {
    return `
    <td class="text-center">
      <div class="upload-page-count" data-page-count-state="idle" data-automation="statement-upload-page-count">Select PDF</div>
    </td>
  `;
}

/**
 * Build a complete upload row (<tr>) with a file input, contact name input,
 * page-count cell, and remove button.
 *
 * @returns {HTMLTableRowElement}
 */
function createRow() {
    const tr = document.createElement("tr");

    tr.innerHTML = `
      <td>
        <input type="file"
               name="statements"
               class="form-control statement-file-input"
               accept="application/pdf,.pdf"
               data-automation="statement-upload-file"
               required>
      </td>
      <td>
        <input type="text"
               name="contact_names"
               class="form-control contact-input"
               placeholder="Select contact"
               list="contacts-list"
               autocomplete="off"
               data-automation="statement-upload-contact"
               required>
      </td>
      ${createPageCountCell()}
      <td class="text-end">
        <button type="button" class="btn btn-outline-danger btn-sm remove-row" aria-label="Remove row">Remove</button>
      </td>
    `;
    return tr;
}

// ---------- Row helpers ----------

/**
 * Return all rows that have a file selected, each paired with its File object.
 * Rows without a selected file are excluded so page counting and preflight
 * only operate on actionable entries.
 *
 * @returns {{ row: HTMLTableRowElement, file: File }[]}
 */
function getSelectedFileEntries() {
    if (!rowsContainer) {
        return [];
    }

    return Array.from(rowsContainer.querySelectorAll("tr"))
        .map((row) => {
            const fileInput = row.querySelector(".statement-file-input");
            const selectedFile = fileInput && fileInput.files ? fileInput.files[0] : null;
            return selectedFile ? { row, file: selectedFile } : null;
        })
        .filter((entry) => entry !== null);
}

/**
 * Build a deterministic signature string for the current set of selected files.
 * Used to detect whether the file set has changed since the last preflight ran,
 * so we can skip redundant server round-trips on submit.
 *
 * @param {{ row: HTMLTableRowElement, file: File }[]} entries
 * @returns {string}
 */
function buildPreflightSignature(entries) {
    return entries
        .map(({ file }, index) => `${index}:${file.name}:${file.size}:${file.lastModified}`)
        .join("|");
}

// ---------- UI state setters ----------

/**
 * Update the data-page-count-state attribute and visible text of a row's
 * page-count pill. The CSS uses the state attribute to colour the pill.
 *
 * @param {HTMLTableRowElement} row
 * @param {"idle"|"counting"|"ready"|"error"} state
 * @param {string} text - Human-readable label to display.
 */
function setPageCountState(row, state, text) {
    const pageCountEl = row.querySelector(".upload-page-count");
    if (!pageCountEl) return;

    pageCountEl.dataset.pageCountState = state;
    pageCountEl.textContent = text;
}

/**
 * Update the preflight summary banner's state attribute and text.
 * The CSS uses the state attribute to colour the banner.
 *
 * @param {"idle"|"checking"|"ready"|"error"} state
 * @param {string} text
 */
function setPreflightSummary(state, text) {
    if (!uploadPreflightSummary) return;

    uploadPreflightSummary.dataset.preflightState = state;
    uploadPreflightSummary.textContent = text;
}

/**
 * Enable or disable the submit button based on current preflight state.
 * The button is disabled while a check is in flight, or when files are selected
 * but the most recent check did not approve submission.
 */
function updateSubmitState() {
    if (!submitButton) return;

    const hasSelectedFiles = getSelectedFileEntries().length > 0;
    // Disable while pending, or if files are selected but preflight rejected them.
    submitButton.disabled = preflightState.pending || (hasSelectedFiles && !preflightState.canSubmit);
}

/**
 * Refresh the page-count summary line above the table. Shows totals when
 * page counts are available; falls back to file count and status otherwise.
 */
function updatePageSummary() {
    if (!uploadPageSummary || !rowsContainer) return;

    const selectedRows = Array.from(rowsContainer.querySelectorAll("tr")).filter((row) => {
        const fileInput = row.querySelector(".statement-file-input");
        return Boolean(fileInput && fileInput.files && fileInput.files[0]);
    });

    if (!selectedRows.length) {
        uploadPageSummary.textContent = "No PDFs selected yet.";
        return;
    }

    // Collect page counts only from rows that have a confirmed number (positive integer).
    const pageCounts = selectedRows
        .map((row) => Number.parseInt(row.dataset.pageCount || "", 10))
        .filter((pageCount) => Number.isFinite(pageCount) && pageCount > 0);

    if (!pageCounts.length) {
        const fileLabel = selectedRows.length === 1 ? "file" : "files";
        const hasCountError = selectedRows.some((row) => {
            const pageCountEl = row.querySelector(".upload-page-count");
            return pageCountEl && pageCountEl.dataset.pageCountState === "error";
        });
        if (hasCountError) {
            uploadPageSummary.textContent = `${selectedRows.length} ${fileLabel} selected. One or more PDFs could not be counted.`;
            return;
        }
        uploadPageSummary.textContent = `${selectedRows.length} ${fileLabel} selected. Counting pages...`;
        return;
    }

    const totalPages = pageCounts.reduce((runningTotal, pageCount) => runningTotal + pageCount, 0);
    const fileLabel = selectedRows.length === 1 ? "file" : "files";
    const pageLabel = totalPages === 1 ? "page" : "pages";
    // Only mark as "server-confirmed" if every selected row has been verified server-side.
    const allServerConfirmed = selectedRows.every(
        (row) => row.dataset.pageCountSource === "server" && Number.parseInt(row.dataset.pageCount || "", 10) > 0,
    );

    uploadPageSummary.textContent = allServerConfirmed
        ? `${selectedRows.length} ${fileLabel} selected, server-confirmed total ${totalPages} ${pageLabel}.`
        : `${selectedRows.length} ${fileLabel} selected, estimated total ${totalPages} ${pageLabel}.`;
}

// ---------- PDF page count estimation ----------

/**
 * Estimate the page count of a PDF file by parsing its raw bytes in the browser.
 * This avoids an upload round-trip for the initial count, giving near-instant
 * feedback while the preflight request runs in parallel.
 *
 * Strategy:
 *   1. Count /Type /Page objects — the most reliable indicator for well-formed PDFs.
 *   2. Fall back to /Count N entries (used by some PDF generators) if (1) finds nothing.
 *
 * The estimate may be wrong for encrypted or non-standard PDFs; the server-side
 * preflight provides the authoritative count and overwrites this value.
 *
 * Uses latin1 decoding to treat the file as a byte string rather than UTF-8,
 * which avoids decoding errors on binary-encoded PDF streams.
 *
 * @param {File} file
 * @returns {Promise<number>} Estimated page count.
 * @throws {Error} If no page count can be determined.
 */
async function estimatePdfPageCount(file) {
    const buffer = await file.arrayBuffer();
    // latin1 decoding preserves raw bytes 1:1, making regex matching on ASCII
    // metadata safe even in the presence of binary (non-UTF-8) PDF streams.
    const pdfText = new TextDecoder("latin1").decode(buffer);

    // Strategy 1: count /Type /Page dictionary entries (one per page in most PDFs).
    const pageMatches = pdfText.match(/\/Type\s*\/Page\b/g);
    if (pageMatches && pageMatches.length > 0) {
        return pageMatches.length;
    }

    // Strategy 2: /Count N appears in page-tree nodes; the largest value is the total.
    const countMatches = Array.from(pdfText.matchAll(/\/Count\s+(\d+)/g));
    if (countMatches.length > 0) {
        const validCounts = countMatches
            .map((match) => Number.parseInt(match[1], 10))
            .filter((count) => Number.isFinite(count) && count > 0);
        if (validCounts.length > 0) {
            return Math.max(...validCounts);
        }
    }

    throw new Error("Unable to estimate PDF page count");
}

// ---------- Row page-count data setters ----------

/**
 * Store an estimated (client-side) page count on the row dataset and update the pill UI.
 *
 * @param {HTMLTableRowElement} row
 * @param {number} pageCount
 */
function setEstimatedPageCount(row, pageCount) {
    row.dataset.pageCount = String(pageCount);
    row.dataset.pageCountSource = "estimate";
    const pageLabel = pageCount === 1 ? "page" : "pages";
    setPageCountState(row, "ready", `${pageCount} ${pageLabel}`);
}

/**
 * Store a server-confirmed page count on the row dataset and update the pill UI.
 * Server counts overwrite estimate counts and are used in the "server-confirmed" summary.
 *
 * @param {HTMLTableRowElement} row
 * @param {number} pageCount
 */
function setServerPageCount(row, pageCount) {
    row.dataset.pageCount = String(pageCount);
    row.dataset.pageCountSource = "server";
    const pageLabel = pageCount === 1 ? "page" : "pages";
    setPageCountState(row, "ready", `${pageCount} ${pageLabel}`);
}

/**
 * Clear the page count and source from a row's dataset.
 * Called when a file is removed or changed so stale values don't persist.
 *
 * @param {HTMLTableRowElement} row
 */
function resetRowPageCount(row) {
    row.dataset.pageCount = "";
    row.dataset.pageCountSource = "";
}

// ---------- Preflight UI helpers ----------

/**
 * Put all selected rows into a "checking" visual state while the preflight request
 * is in flight. Shows "Checking..." for rows that already have a count (to signal
 * the server is re-validating) and "Counting..." for rows with no count yet.
 *
 * @param {{ row: HTMLTableRowElement, file: File }[]} entries
 */
function setPreflightPending(entries) {
    entries.forEach(({ row }) => {
        const existingPageCount = Number.parseInt(row.dataset.pageCount || "", 10);
        if (Number.isFinite(existingPageCount) && existingPageCount > 0) {
            setPageCountState(row, "counting", "Checking...");
        } else {
            setPageCountState(row, "counting", "Counting...");
        }
    });
}

/**
 * Apply the per-file results from a successful preflight response to the row pills.
 * Result objects arrive in the same order as the entries array.
 *
 * @param {{ row: HTMLTableRowElement, file: File }[]} entries
 * @param {{ files: Array<{ page_count?: number, error?: string }> }} payload
 */
function applyPreflightResults(entries, payload) {
    const resultFiles = Array.isArray(payload.files) ? payload.files : [];

    entries.forEach(({ row }, index) => {
        const result = resultFiles[index];
        if (!result || typeof result !== "object") {
            resetRowPageCount(row);
            setPageCountState(row, "error", "Unable to check");
            return;
        }

        if (typeof result.error === "string" && result.error) {
            resetRowPageCount(row);
            setPageCountState(row, "error", "Unable to count");
            return;
        }

        if (Number.isFinite(result.page_count) && result.page_count > 0) {
            setServerPageCount(row, result.page_count);
            return;
        }

        resetRowPageCount(row);
        setPageCountState(row, "error", "Unable to count");
    });
}

/**
 * Update the preflight summary banner from the server's preflight response payload.
 * The summary covers the aggregate result: total pages, available pages, and shortfall.
 *
 * When there is a shortfall, the banner uses innerHTML to embed a "Buy Pages" link.
 * Values come from server-supplied integers, so XSS via those fields is not a risk.
 *
 * @param {{ can_submit: boolean, has_errors: boolean, is_sufficient: boolean,
 *           total_pages: number, available_tokens: number, shortfall: number }} payload
 */
function setPreflightSummaryFromPayload(payload) {
    const totalPages = Number.isFinite(payload.total_pages) ? payload.total_pages : 0;
    const availableTokens = Number.isFinite(payload.available_tokens) ? payload.available_tokens : 0;
    const shortfall = Number.isFinite(payload.shortfall) ? payload.shortfall : 0;
    const pageLabel = totalPages === 1 ? "page" : "pages";
    // "tokens" is the backend term; the UI uses "pages" to match user-facing language.
    const tokenLabel = availableTokens === 1 ? "page" : "pages";

    if (payload.has_errors) {
        setPreflightSummary(
            "error",
            "Server validation failed for one or more PDFs. Fix the files marked above before uploading.",
        );
        return;
    }

    if (payload.can_submit) {
        setPreflightSummary(
            "ready",
            `Server confirmed ${totalPages} ${pageLabel}. ${availableTokens} ${tokenLabel} available. Upload can proceed.`,
        );
        return;
    }

    if (!payload.is_sufficient) {
        // Read the buy-tokens URL from the form's data attribute (set server-side via url_for),
        // falling back to the hardcoded path if the attribute is missing.
        const buyUrl = uploadForm ? uploadForm.dataset.buyTokensUrl || "/buy-pages" : "/buy-pages";
        // Values are server-supplied integers — no XSS risk using innerHTML here.
        uploadPreflightSummary.innerHTML =
            `Server confirmed ${totalPages} ${pageLabel}. ` +
            `${availableTokens} ${tokenLabel} available, short by ${shortfall}. ` +
            `<a href="${buyUrl}" class="btn btn-sm btn-outline-primary ms-2">Buy Pages</a>`;
        uploadPreflightSummary.dataset.preflightState = "error";
        return;
    }

    setPreflightSummary("error", "Server validation is incomplete. Please review the selected files.");
}

// ---------- Auth redirect ----------

/**
 * Check a preflight Response for auth failures and redirect when necessary.
 * A local version of the main.js helper, scoped to the upload page's login URL.
 *
 * @param {Response} response
 * @returns {Promise<boolean>} true if a redirect was triggered.
 */
async function redirectForUnauthorizedResponse(response) {
    const fallbackUrl = uploadForm ? uploadForm.dataset.loginUrl || "/login" : "/login";

    if (response.redirected && response.url.includes("/login")) {
        window.location.href = response.url;
        return true;
    }

    if (response.status !== 401) {
        return false;
    }

    try {
        const payload = await response.clone().json();
        if (payload && typeof payload.redirect === "string" && payload.redirect) {
            window.location.href = payload.redirect;
            return true;
        }
    } catch (_error) {
        // Ignore parse errors and fall back to the login page.
    }

    window.location.href = fallbackUrl;
    return true;
}

// ---------- Preflight request ----------

/**
 * Build the FormData body for the preflight POST, attaching all selected files
 * and the CSRF token.
 *
 * @param {{ row: HTMLTableRowElement, file: File }[]} entries
 * @returns {FormData}
 */
function buildPreflightRequest(entries) {
    const formData = new FormData();
    appendCsrfTokenToFormData(formData, uploadForm || document);
    entries.forEach(({ file }) => {
        formData.append("statements", file);
    });
    return formData;
}

/**
 * Run the preflight check against the server.
 *
 * Flow:
 *   1. Abort any in-flight preflight request.
 *   2. POST all selected files to the preflight endpoint.
 *   3. Apply per-file results to row pills.
 *   4. Update the summary banner and submit button.
 *
 * Uses a sequence counter to discard responses from superseded requests — the
 * user may change the file selection while a request is in flight.
 *
 * @returns {Promise<boolean>} true if the server approved the upload.
 */
async function runPreflight() {
    const entries = getSelectedFileEntries();
    const signature = buildPreflightSignature(entries);

    if (!entries.length) {
        // No files selected — clear all preflight state and reset the UI.
        if (activePreflightController) {
            activePreflightController.abort();
            activePreflightController = null;
        }
        preflightState.signature = "";
        preflightState.canSubmit = false;
        preflightState.pending = false;
        setPreflightSummary("idle", "");
        updatePageSummary();
        updateSubmitState();
        return false;
    }

    const preflightUrl = uploadForm ? uploadForm.dataset.preflightUrl : "";
    if (!preflightUrl) {
        // Misconfigured template — fail gracefully rather than silently.
        preflightState.signature = "";
        preflightState.canSubmit = false;
        preflightState.pending = false;
        setPreflightSummary("error", "Upload validation is not configured correctly.");
        updateSubmitState();
        return false;
    }

    if (activePreflightController) {
        activePreflightController.abort();
    }

    const controller = new AbortController();
    activePreflightController = controller;
    // Capture the sequence number so the response handler can ignore stale results.
    const requestSequence = ++preflightRequestSequence;
    preflightState.signature = signature;
    preflightState.canSubmit = false;
    preflightState.pending = true;
    setPreflightPending(entries);
    setPreflightSummary("checking", "Checking page counts and page availability...");
    updatePageSummary();
    updateSubmitState();

    try {
        const response = await fetch(preflightUrl, {
            method: "POST",
            headers: {
                Accept: "application/json",
            },
            body: buildPreflightRequest(entries),
            credentials: "same-origin",
            signal: controller.signal,
        });

        if (await redirectForUnauthorizedResponse(response)) {
            return false;
        }

        let payload = null;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = null;
        }

        if (!response.ok) {
            const message =
                payload && typeof payload.error === "string"
                    ? payload.error
                    : "Unable to validate uploads on the server.";
            throw new Error(message);
        }

        // Discard this response if a newer preflight request has already started.
        if (requestSequence !== preflightRequestSequence) {
            return false;
        }

        applyPreflightResults(entries, payload || {});
        setPreflightSummaryFromPayload(payload || {});
        preflightState.canSubmit = Boolean(payload && payload.can_submit);
        preflightState.pending = false;
        updatePageSummary();
        updateSubmitState();
        return preflightState.canSubmit;
    } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
            // Request was intentionally aborted (e.g. file selection changed) — not an error.
            return false;
        }

        // Discard stale error responses.
        if (requestSequence !== preflightRequestSequence) {
            return false;
        }

        preflightState.canSubmit = false;
        preflightState.pending = false;
        setPreflightSummary(
            "error",
            error instanceof Error ? error.message : "Unable to validate uploads on the server.",
        );
        updateSubmitState();
        return false;
    } finally {
        // Clear the active controller reference once this request completes or aborts.
        if (activePreflightController === controller) {
            activePreflightController = null;
        }
    }
}

// ---------- Event handlers ----------

/**
 * Handle a file input change for a specific row.
 *
 * When a file is selected:
 *   1. Immediately estimate the page count from the local PDF bytes.
 *   2. Kick off a preflight request to the server (which provides the authoritative count).
 *
 * A "selection token" (timestamp + random) is stamped on the row at the start of
 * each change. The async estimate result is discarded if the token has changed
 * (the user swapped files again) or if the server already provided a count.
 *
 * @param {HTMLInputElement} fileInput
 */
async function handleFileChange(fileInput) {
    const row = fileInput.closest("tr");
    if (!row) return;

    const selectedFile = fileInput.files && fileInput.files[0];
    resetRowPageCount(row);
    // Stamp a unique token so async callbacks can detect if this selection is still current.
    row.dataset.fileSelectionToken = `${Date.now()}-${Math.random()}`;
    const selectionToken = row.dataset.fileSelectionToken;

    if (!selectedFile) {
        setPageCountState(row, "idle", "Select PDF");
        preflightState.signature = "";
        preflightState.canSubmit = false;
        updatePageSummary();
        await runPreflight();
        return;
    }

    setPageCountState(row, "counting", "Counting...");
    updatePageSummary();

    try {
        const pageCount = await estimatePdfPageCount(selectedFile);
        // Bail out if the file changed again, or if the server already responded.
        if (row.dataset.fileSelectionToken !== selectionToken || row.dataset.pageCountSource === "server") {
            return;
        }
        setEstimatedPageCount(row, pageCount);
    } catch (_error) {
        if (row.dataset.fileSelectionToken !== selectionToken || row.dataset.pageCountSource === "server") {
            return;
        }
        resetRowPageCount(row);
        setPageCountState(row, "error", "Unable to count");
    } finally {
        updatePageSummary();
        await runPreflight();
    }
}

/**
 * Handle clicks within the rows table.
 * Delegates to the remove-row button if clicked.
 *
 * @param {MouseEvent} event
 */
function handleTableClick(event) {
    const removeButton = event.target.closest(".remove-row");
    if (!removeButton) return;

    removeButton.closest("tr")?.remove();
    preflightState.signature = "";
    preflightState.canSubmit = false;
    updatePageSummary();
    void runPreflight();
}

/**
 * Handle change events within the rows table.
 * Delegates to the file input if it triggered the event.
 *
 * @param {Event} event
 */
function handleTableChange(event) {
    const fileInput = event.target.closest(".statement-file-input");
    if (!fileInput) return;
    void handleFileChange(fileInput);
}

/**
 * Intercept form submission to ensure a successful preflight has run
 * for the current file set before the browser sends the multipart POST.
 *
 * If the preflight already approved the current files (signature match + canSubmit),
 * the submit is allowed through immediately. Otherwise the check is run first;
 * on approval, requestSubmit() is called programmatically with allowDirectSubmit=true
 * so this handler does not intercept the re-submitted event.
 *
 * @param {SubmitEvent} event
 */
async function handleFormSubmit(event) {
    // A submit we triggered ourselves after a successful check — pass through.
    if (allowDirectSubmit) {
        allowDirectSubmit = false;
        return;
    }

    const entries = getSelectedFileEntries();
    if (!entries.length) {
        return;
    }

    const currentSignature = buildPreflightSignature(entries);
    // Skip the preflight round-trip if the file set hasn't changed and the last check passed.
    if (!preflightState.pending && preflightState.signature === currentSignature && preflightState.canSubmit) {
        return;
    }

    event.preventDefault();
    const canSubmit = await runPreflight();
    if (!canSubmit || !uploadForm) {
        return;
    }

    // Gate lifted — submit once more without interception.
    allowDirectSubmit = true;
    uploadForm.requestSubmit();
}

// ---------- Init ----------

// Only wire up event listeners if all required elements are present.
// The script is included on all pages via base.html, so we must guard against
// pages that don't have the upload form.
if (uploadForm && addRowButton && rowsContainer) {
    addRowButton.addEventListener("click", () => rowsContainer.appendChild(createRow()));
    rowsContainer.addEventListener("click", handleTableClick);
    rowsContainer.addEventListener("change", handleTableChange);
    uploadForm.addEventListener("submit", (event) => {
        void handleFormSubmit(event);
    });
    updatePageSummary();
    updateSubmitState();
}
