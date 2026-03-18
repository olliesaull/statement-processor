/**
 * Shared CSRF helpers for browser-side POST requests.
 *
 * CloudFront/App Runner can be awkward with custom headers, so the app sends
 * CSRF tokens in request bodies for JavaScript POSTs. Templates still render
 * hidden csrf_token inputs for normal HTML form submissions.
 */

export function getCsrfToken(fallbackRoot = document) {
  const meta = document.querySelector('meta[name="csrf-token"]');
  const metaToken = meta ? meta.getAttribute("content") || "" : "";
  if (metaToken) {
    return metaToken;
  }

  if (fallbackRoot instanceof Element || fallbackRoot instanceof Document) {
    const hiddenInput = fallbackRoot.querySelector('input[name="csrf_token"]');
    if (hiddenInput instanceof HTMLInputElement && hiddenInput.value) {
      return hiddenInput.value;
    }
  }

  return "";
}

export function appendCsrfTokenToFormData(formData, fallbackRoot = document) {
  formData.append("csrf_token", getCsrfToken(fallbackRoot));
  return formData;
}

export function buildCsrfUrlEncodedBody(fallbackRoot = document) {
  return new URLSearchParams({ csrf_token: getCsrfToken(fallbackRoot) });
}
