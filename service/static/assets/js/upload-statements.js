import { appendCsrfTokenToFormData } from "./csrf.js";

const uploadForm = document.getElementById("upload-statements-form");
const rowsContainer = document.getElementById("rows");
const addRowButton = document.getElementById("add-row");
const uploadPageSummary = document.getElementById("upload-page-summary");
const uploadPreflightSummary = document.getElementById("upload-preflight-summary");
const submitButton = uploadForm ? uploadForm.querySelector('button[type="submit"]') : null;

const preflightState = {
  signature: "",
  canSubmit: false,
  pending: false,
};

let activePreflightController = null;
let preflightRequestSequence = 0;
let allowDirectSubmit = false;

function createPageCountCell() {
  return `
    <td class="text-center">
      <div class="upload-page-count" data-page-count-state="idle" data-automation="statement-upload-page-count">Select PDF</div>
    </td>
  `;
}

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

function buildPreflightSignature(entries) {
  return entries
    .map(({ file }, index) => `${index}:${file.name}:${file.size}:${file.lastModified}`)
    .join("|");
}

function setPageCountState(row, state, text) {
  const pageCountEl = row.querySelector(".upload-page-count");
  if (!pageCountEl) return;

  pageCountEl.dataset.pageCountState = state;
  pageCountEl.textContent = text;
}

function setPreflightSummary(state, text) {
  if (!uploadPreflightSummary) return;

  uploadPreflightSummary.dataset.preflightState = state;
  uploadPreflightSummary.textContent = text;
}

function updateSubmitState() {
  if (!submitButton) return;

  const hasSelectedFiles = getSelectedFileEntries().length > 0;
  submitButton.disabled = preflightState.pending || (hasSelectedFiles && !preflightState.canSubmit);
}

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
  const allServerConfirmed = selectedRows.every((row) => row.dataset.pageCountSource === "server" && Number.parseInt(row.dataset.pageCount || "", 10) > 0);

  uploadPageSummary.textContent = allServerConfirmed
    ? `${selectedRows.length} ${fileLabel} selected, server-confirmed total ${totalPages} ${pageLabel}.`
    : `${selectedRows.length} ${fileLabel} selected, estimated total ${totalPages} ${pageLabel}.`;
}

async function estimatePdfPageCount(file) {
  const buffer = await file.arrayBuffer();
  const pdfText = new TextDecoder("latin1").decode(buffer);
  const pageMatches = pdfText.match(/\/Type\s*\/Page\b/g);

  if (pageMatches && pageMatches.length > 0) {
    return pageMatches.length;
  }

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

function setEstimatedPageCount(row, pageCount) {
  row.dataset.pageCount = String(pageCount);
  row.dataset.pageCountSource = "estimate";
  const pageLabel = pageCount === 1 ? "page" : "pages";
  setPageCountState(row, "ready", `${pageCount} ${pageLabel}`);
}

function setServerPageCount(row, pageCount) {
  row.dataset.pageCount = String(pageCount);
  row.dataset.pageCountSource = "server";
  const pageLabel = pageCount === 1 ? "page" : "pages";
  setPageCountState(row, "ready", `${pageCount} ${pageLabel}`);
}

function resetRowPageCount(row) {
  row.dataset.pageCount = "";
  row.dataset.pageCountSource = "";
}

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

function setPreflightSummaryFromPayload(payload) {
  const totalPages = Number.isFinite(payload.total_pages) ? payload.total_pages : 0;
  const availableTokens = Number.isFinite(payload.available_tokens) ? payload.available_tokens : 0;
  const shortfall = Number.isFinite(payload.shortfall) ? payload.shortfall : 0;
  const pageLabel = totalPages === 1 ? "page" : "pages";
  const tokenLabel = availableTokens === 1 ? "token" : "tokens";

  if (payload.has_errors) {
    setPreflightSummary("error", "Server validation failed for one or more PDFs. Fix the files marked above before uploading.");
    return;
  }

  if (payload.can_submit) {
    setPreflightSummary("ready", `Server confirmed ${totalPages} ${pageLabel}. ${availableTokens} ${tokenLabel} available. Upload can proceed.`);
    return;
  }

  if (!payload.is_sufficient) {
    // Read the buy-tokens URL from the form's data attribute (set server-side via url_for),
    // falling back to the hardcoded path if the attribute is missing.
    const buyUrl = uploadForm ? uploadForm.dataset.buyTokensUrl || "/buy-tokens" : "/buy-tokens";
    // Values are server-supplied integers — no XSS risk using innerHTML here.
    uploadPreflightSummary.innerHTML =
      `Server confirmed ${totalPages} ${pageLabel}. ` +
      `${availableTokens} ${tokenLabel} available, short by ${shortfall}. ` +
      `<a href="${buyUrl}" class="btn btn-sm btn-outline-primary ms-2">Buy Tokens</a>`;
    uploadPreflightSummary.dataset.preflightState = "error";
    return;
  }

  setPreflightSummary("error", "Server validation is incomplete. Please review the selected files.");
}

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

function buildPreflightRequest(entries) {
  const formData = new FormData();
  appendCsrfTokenToFormData(formData, uploadForm || document);
  entries.forEach(({ file }) => {
    formData.append("statements", file);
  });
  return formData;
}

async function runPreflight() {
  const entries = getSelectedFileEntries();
  const signature = buildPreflightSignature(entries);

  if (!entries.length) {
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
  const requestSequence = ++preflightRequestSequence;
  preflightState.signature = signature;
  preflightState.canSubmit = false;
  preflightState.pending = true;
  setPreflightPending(entries);
  setPreflightSummary("checking", "Checking page counts and token availability...");
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
      const message = payload && typeof payload.error === "string" ? payload.error : "Unable to validate uploads on the server.";
      throw new Error(message);
    }

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
      return false;
    }

    if (requestSequence !== preflightRequestSequence) {
      return false;
    }

    preflightState.canSubmit = false;
    preflightState.pending = false;
    setPreflightSummary("error", error instanceof Error ? error.message : "Unable to validate uploads on the server.");
    updateSubmitState();
    return false;
  } finally {
    if (activePreflightController === controller) {
      activePreflightController = null;
    }
  }
}

async function handleFileChange(fileInput) {
  const row = fileInput.closest("tr");
  if (!row) return;

  const selectedFile = fileInput.files && fileInput.files[0];
  resetRowPageCount(row);
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

function handleTableClick(event) {
  const removeButton = event.target.closest(".remove-row");
  if (!removeButton) return;

  removeButton.closest("tr")?.remove();
  preflightState.signature = "";
  preflightState.canSubmit = false;
  updatePageSummary();
  void runPreflight();
}

function handleTableChange(event) {
  const fileInput = event.target.closest(".statement-file-input");
  if (!fileInput) return;
  void handleFileChange(fileInput);
}

async function handleFormSubmit(event) {
  if (allowDirectSubmit) {
    allowDirectSubmit = false;
    return;
  }

  const entries = getSelectedFileEntries();
  if (!entries.length) {
    return;
  }

  const currentSignature = buildPreflightSignature(entries);
  if (!preflightState.pending && preflightState.signature === currentSignature && preflightState.canSubmit) {
    return;
  }

  event.preventDefault();
  const canSubmit = await runPreflight();
  if (!canSubmit || !uploadForm) {
    return;
  }

  allowDirectSubmit = true;
  uploadForm.requestSubmit();
}

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
