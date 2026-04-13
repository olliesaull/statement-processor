---
paths:
  - "service/playwright_tests/**/*"
  - "service/app.py"
  - "service/templates/**/*"
---

# Browser Testing Guide

This project supports interactive browser testing via the Playwright MCP server. Use this when verifying UI changes, debugging page flows, or running exploratory end-to-end checks against a local app instance.

## Prerequisites

The following must be running before any browser test session:

1. **Valkey** (session store) on `localhost:6379`.
   - Verify with: `valkey-cli ping` — should return `PONG`.

2. **App server** started with gunicorn:
   ```
   python3.13 -m gunicorn --reload --bind 0.0.0.0:8080 app:app
   ```
   The `--reload` flag means code changes are picked up automatically without restarting.

3. **Environment variables** set before starting gunicorn:
   - `STAGE=local` — activates local config and disables production guards.
   - `PLAYWRIGHT_TENANT_ID` — the Xero tenant ID of a previously-synced tenant in the local cache.
   - `PLAYWRIGHT_TENANT_NAME` — the display name for that tenant.

   These variables are read by the `/test-login` route to create a fake session. The tenant must already exist in the local Valkey/cache from a prior real sync; the fake session does not pull live data.

## Authentication

There is no login form in local mode. Instead, navigate directly to the test-login route:

```
http://localhost:8080/test-login
```

This sets a full session cookie (using `PLAYWRIGHT_TENANT_ID` and `PLAYWRIGHT_TENANT_NAME`) and redirects to `/tenant_management`. From that point the browser is authenticated and all pages are accessible for the remainder of the session.

## What Works End-to-End

With a synced tenant in local cache, all of the following workflows function fully against local data:

- **Statement list** — browse, filter, and view parsed statements.
- **Upload** — upload new bank statement files for processing.
- **Configs** — view and edit tenant configuration settings.
- **Tenant picker** — switch between tenants in the nav.

Because all these pages read from the local cache (Valkey + local storage), they behave identically to production for UI verification purposes.

## Limitations

Do not click **Sync** or **Disconnect** on the tenant management page.

These buttons call the live Xero API. The fake session token is not a real OAuth token, so Xero will return a 401 and the app will surface an error. This is expected — the limitation is by design to keep local testing isolated from the live OAuth flow.

All other pages are safe to use without restriction.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/test-login` returns 500 | Valkey is not running | Run `valkey-cli ping`; start Valkey if it returns an error |
| Pages show stale data | Tenant not synced | Run a real sync once with valid Xero credentials, then use the fake session for subsequent testing |
| Gunicorn not picking up changes | `--reload` flag missing | Restart gunicorn with the command above |

## Iterative Workflow

The recommended loop for verifying UI changes:

1. Edit the Python or template code.
2. Gunicorn detects the change and reloads automatically (no restart needed).
3. Use `browser_navigate` to load the relevant page.
4. Use `browser_screenshot` to capture the current state and verify the change visually.

This cycle is fast — typically a few seconds between edit and visual confirmation.
