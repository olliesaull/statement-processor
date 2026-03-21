# Interactive Browser Testing for Claude Code

## Context

After implementing the auto-config suggestion feature, the main pain point was UI issues that couldn't be caught without manually browsing the app. Claude Code currently has no way to see or interact with the running UI — it can only write code and hope the templates are correct.

This plan adds interactive browser testing capabilities so Claude Code can navigate the app, take screenshots, click elements, and verify UI state during development. This is not an automated test suite — it's giving Claude eyes on the UI.

## Approach

1. **Local-only auth bypass** — `/test-login` route seeds a Flask session without Xero OAuth
2. **Agent docs** — so future sessions know how to use the browser

Playwright MCP server is already configured.

## Changes

### 1. Add `/test-login` route to `service/app.py`

At the **end of the file** (no `if __name__` block exists), add a route gated behind `STAGE == "local"`:

```python
if STAGE == "local":

    @app.route("/test-login")
    def test_login():
        """Seed the Flask session with fake auth for local browser testing.

        Only exists when STAGE=local. Bypasses Xero OAuth so Claude Code
        (or a developer) can browse authenticated pages without credentials.

        Requires PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars
        pointing to a previously-synced tenant.
        """
        tenant_id = os.environ.get("PLAYWRIGHT_TENANT_ID")
        tenant_name = os.environ.get("PLAYWRIGHT_TENANT_NAME")

        if not tenant_id or not tenant_name:
            return "Set PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars", 400

        session["xero_oauth2_token"] = {
            "access_token": "test-token-local",
            "token_type": "Bearer",
            "expires_in": 86400,
            "expires_at": time.time() + 86400,
        }
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_name
        session["xero_tenants"] = [{"tenantId": tenant_id, "tenantName": tenant_name}]
        session["xero_user_email"] = "claude@local-test.dev"
        logger.info("Test login session seeded", tenant_id=tenant_id)

        response = redirect(url_for("tenant_management"))
        response.set_cookie("cookie_consent", "true", max_age=86400, path="/")
        response.set_cookie("session_is_set", "true", max_age=86400, path="/")
        return response
```

**Requires:** `import time` at top of file (`os`, `redirect`, `url_for`, `session`, `logger` already imported).

**Why each session key:**
- `xero_oauth2_token` — checked by `xero_token_required` via `get_xero_oauth2_token()` in `service/utils/auth.py:56`. 24h expiry to avoid session timeout during long dev sessions.
- `xero_tenant_id` — checked by `active_tenant_required` in `service/utils/auth.py:289`
- `xero_tenant_name` — display name used in UI
- `xero_tenants` — list for tenant picker page (`/tenant_management` reads `session.get("xero_tenants")`)
- `xero_user_email` — pre-fills email on buy-tokens/checkout pages (`app.py:1642`)
- `cookie_consent` cookie — passes `has_cookie_consent()` in `service/utils/auth.py:124`
- `session_is_set` cookie — navbar shows "Logout" link (cosmetic only)

**Safety:** The `if STAGE == "local":` block means the route is never registered in dev/prod. It doesn't exist in the Flask URL map. Sessions are server-side (Redis/Valkey) so users cannot tamper with tenant IDs in production.

### 2. Fix `SESSION_COOKIE_SECURE` for local dev

**File:** `service/app.py:81`

Change:
```python
SESSION_COOKIE_SECURE=True,
```
To:
```python
SESSION_COOKIE_SECURE=STAGE != "local",
```

Without this, the browser won't send the session cookie over plain HTTP on localhost and every page will redirect to login.

### 3. Create browser testing agent doc

**File:** `agent_docs/browser_testing.md` (new)

Document:
- **Prerequisites**: Valkey running on localhost:6379, app running with `python3.13 -m gunicorn --reload --bind 0.0.0.0:8080 app:app`, `STAGE=local` in env, `PLAYWRIGHT_TENANT_ID` and `PLAYWRIGHT_TENANT_NAME` env vars set to a previously-synced tenant
- **Auth**: Navigate to `http://localhost:8080/test-login` → redirects to `/tenant_management` with full session
- **Full end-to-end**: With a synced tenant, all workflows work — upload statements, configure configs, view statements, tenant picker, etc.
- **Limitations**:
  - Do not click **Sync** or **Disconnect** on the tenant management page — these call the live Xero API and the fake token will return 401
  - All other pages (statements, upload, configs) read from local cache and work fully with the fake session
- **Troubleshooting**: If `/test-login` returns 500, check Valkey is running (`valkey-cli ping` should return PONG)
- **Iterative workflow**: Edit code → Gunicorn auto-reloads → `browser_navigate` → `browser_screenshot` to verify

### 4. Update existing testing doc

**File:** `agent_docs/testing.md`

Add a "Browser testing" section with cross-reference: "For interactive browser testing with Playwright MCP, see `agent_docs/browser_testing.md`."

### 5. Update README.md

Add documentation for the `/test-login` local dev route and the `SESSION_COOKIE_SECURE` conditional in the auth/session section.

### 6. Add tests for `/test-login` route

**File:** `service/tests/test_app_test_login.py` (new)

One test:
- **STAGE=local + env vars set**: `GET /test-login` returns 302 redirect to `/tenant_management`, session contains `xero_oauth2_token`, `xero_tenant_id`, `xero_tenant_name`, `xero_tenants`, `xero_user_email`

## Files to modify

| File | Change |
|---|---|
| `service/app.py` | Add `import time`; add `/test-login` route at end; change `SESSION_COOKIE_SECURE` |
| `agent_docs/browser_testing.md` | Create — browser testing workflow docs |
| `agent_docs/testing.md` | Add cross-reference to browser testing |
| `README.md` | Document `/test-login` route and `SESSION_COOKIE_SECURE` conditional |
| `service/tests/test_app_test_login.py` | Happy-path test for `/test-login` session seeding |

## Verification

1. `make dev` passes (includes new tests)
2. Start app locally, navigate to `http://localhost:8080/test-login` — should redirect to `/tenant_management` with authenticated session
3. Use `browser_navigate` to hit `/test-login`, then `browser_screenshot` to verify tenant management page renders with "Logout" in navbar
