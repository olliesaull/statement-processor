# Interactive Browser Testing for Claude Code

## Context

After implementing the auto-config suggestion feature, the main pain point was UI issues that couldn't be caught without manually browsing the app. Claude Code currently has no way to see or interact with the running UI — it can only write code and hope the templates are correct.

This plan adds interactive browser testing capabilities so Claude Code can navigate the app, take screenshots, click elements, and verify UI state during development. This is not an automated test suite — it's giving Claude eyes on the UI.

## Approach

1. **Playwright MCP server** — gives Claude browser tools (navigate, screenshot, click, fill, etc.)
2. **Local-only auth bypass** — `/test-login` route seeds a Flask session without Xero OAuth
3. **Agent docs** — so future sessions know how to use the browser

## Changes

### 1. Add `/test-login` route to `service/app.py`

At the bottom of `app.py` (before `if __name__`), add a route gated behind `STAGE == "local"`:

```python
if STAGE == "local":

    @app.route("/test-login")
    def test_login():
        """Seed the Flask session with fake auth for local browser testing.

        Only exists when STAGE=local. Bypasses Xero OAuth so Claude Code
        (or a developer) can browse authenticated pages without credentials.
        """
        session["xero_oauth2_token"] = {
            "access_token": "test-token-local",
            "token_type": "Bearer",
            "expires_in": 1800,
            "expires_at": time.time() + 1800,
        }
        session["xero_tenant_id"] = os.environ.get(
            "PLAYWRIGHT_TENANT_ID", "LOCAL_TEST_TENANT"
        )
        session["xero_tenant_name"] = os.environ.get(
            "PLAYWRIGHT_TENANT_NAME", "Local Test Tenant"
        )
        logger.info("Test login session seeded", tenant_id=session["xero_tenant_id"])

        response = redirect("/")
        response.set_cookie("cookie_consent", "true", max_age=86400, path="/")
        response.set_cookie("session_is_set", "true", max_age=86400, path="/")
        return response
```

**Why each session key:**
- `xero_oauth2_token` — checked by `xero_token_required` via `get_xero_oauth2_token()` in `service/utils/auth.py:65`
- `xero_tenant_id` — checked by `active_tenant_required` in `service/utils/auth.py:257`
- `cookie_consent` cookie — checked by `has_cookie_consent()` before auth decorators run
- `session_is_set` cookie — makes navbar show "Logout" link

**Safety:** The `if STAGE == "local":` block means the route is never registered in dev/prod. It doesn't exist in the Flask URL map.

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

### 3. Configure Playwright MCP server

**File:** `.claude/settings.json` (new file)

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@anthropic-ai/mcp-server-playwright"]
    }
  }
}
```

`npx` auto-fetches the package on first use. No `npm install` needed.

### 4. Create browser testing agent doc

**File:** `agent_docs/browser_testing.md` (new)

Document:
- Prerequisites (app running, Valkey running, STAGE=local)
- How to authenticate via `/test-login`
- `PLAYWRIGHT_TENANT_ID` / `PLAYWRIGHT_TENANT_NAME` env vars for targeting a real tenant
- Available MCP tools (browser_navigate, browser_screenshot, browser_click, browser_type, etc.)
- Typical workflow (navigate to /test-login → screenshot → navigate to page under test → verify)
- Limitations (no real Xero API calls, CSRF still enforced on POST forms)

### 5. Update existing testing doc

**File:** `agent_docs/testing.md`

Add a "Browser testing" section referencing `agent_docs/browser_testing.md` and the `/test-login` route.

## Files to modify

| File | Change |
|---|---|
| `service/app.py` | Add `/test-login` route (STAGE=local only); fix `SESSION_COOKIE_SECURE` for local |
| `.claude/settings.json` | Create with Playwright MCP server config |
| `agent_docs/browser_testing.md` | Create — browser testing workflow docs |
| `agent_docs/testing.md` | Add cross-reference to browser testing |

## Verification

1. `make dev` passes
2. Start app locally, navigate to `http://localhost:8080/test-login` — should redirect to `/` with authenticated session
3. Restart Claude Code session (to pick up new MCP server), verify `browser_navigate` tool is available
4. Use `browser_navigate` to hit `/test-login`, then `browser_screenshot` to verify the landing page renders
