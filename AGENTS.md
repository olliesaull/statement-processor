# Agent Guidelines

This file defines cross-project rules for AI coding agents.
Project-specific context lives in `agent_docs/`.

## Environment
- All development is done in Linux (Ubuntu).
  - Mac: Ubuntu via Parallels
  - Windows: Ubuntu via WSL
- Always use bash/Linux commands. Do not use PowerShell or Windows-specific commands.
**Important**: WSL may cause agents to incorrectly detect Windows. Always use bash/Linux commands of detected OS.

## Python Version
- Projects target **Python 3.13** unless the repo explicitly states otherwise.
- Always use explicit versioned commands (`python3.13 ...`).
- Never use bare `python` or `python3`.

## Required Workflow After Code Changes (important)

After modifying Python code, always run `make dev`.
- Run `make` commands from within `service/` or `lambda_functions/textraction_lambda/`, not from the repo root. The Makefile uses `find .` relative to the working directory, so running from root breaks pylint module resolution and produces spurious duplicate-code and import errors. The exception is `make run-app`, which is run from the repo root.
- This runs formatting and linting. Work is not complete unless it passes.
- Do not suppress lint errors without justification.
- If you cannot run commands, state what you would have run and why.
- Note: current Make targets are non-blocking (`|| true`), so review output carefully and run targeted tests for touched modules.

## Code Expectations

Write code that is:

- Readable, strongly typed, maintainable, testable, and secure
- Minimal and targeted (avoid large refactors unless requested)
- Well documented (important)
    - Add docstrings to all modules and functions
    - Add comments in simple language periodically to enhance explainability

Prefer structured types over untyped dictionaries.
Avoid magic strings for fixed values.
Do not introduce new production dependencies unless explicitly requested.

If behaviour changes, document it clearly.
Do not change behaviour solely for style/cleanup.

## Documentation (code MUST be documented)

### Docstrings
- Docstrings should explain intent and contracts (not narrate obvious code).
- Use consistent docstring style across the repo (see `agent_docs/documentation.md`).

### Comments
- Prefer “why” comments over “what” comments.
- Add comments for non-obvious business rules, edge cases, service quirks.

## Dependencies
- Production deps: `requirements.txt`
- Dev-only deps: `requirements-dev.txt`
- If dependencies change, run `make update-venv` if present.

## Logging (important)

- Prefer structured logs (key/value context).
- Log “why” and relevant identifiers (tenant_id, statement_id, request_id, etc).
- Avoid logging secrets, tokens, or full sensitive documents.
- Use aws_lambda_powertools style

## Behaviour preservation
- Never change behaviour just to improve documentation or style.
- If behaviour changes are required for correctness, clearly document the change.

## Plans

- Make the plan extremely concise. Sacrifice grammar for the sake of concision.
- At the end of each plan, give mne a list of unanswered questions to answer, if any.

## Deployment Configuration Checklist (important)

There is no nginx in the local dev environment, so changes that affect nginx or the Docker image will work locally but **break in production** if the config files are not updated. Always check these when making changes:

### Dockerfile (`service/Dockerfile`)
- **New directories under `service/`**: Add a `COPY <dir>/ ./<dir>/` line. The Dockerfile copies directories explicitly — new ones are silently excluded from the container image.
- **New config/data files**: If the app reads a new file at runtime, ensure it is copied into the image.

### Nginx query string allowlist (`service/nginx_route_querystring_allow_list.json`)
- **Adding or renaming query parameters on a route**: Add/update the entry in this JSON file. Public routes have query strings **stripped** by nginx unless explicitly allowed here. This is the most common production-only failure — the app works locally because there is no nginx, but 404s in production because the parameter is blocked.

### Nginx route regeneration (`service/nginx-routes.conf`)
- **Adding/removing Flask routes, changing auth decorators, or changing allowed query params**: Regenerate `nginx-routes.conf` by running the generator from `service/`:
  ```
  cd service && python3.13 nginx_route_config_generator.py
  ```
  Review the diff before committing.

### Nginx route overrides (`service/nginx_route_overrides.json`)
- **Routes needing non-default body size or timeout**: Add an entry here (e.g. `client_max_body_size`, `proxy_read_timeout`).

## Browser Testing with Playwright (important)

When using the Playwright MCP to test pages in the browser, you **must** authenticate first by navigating to `http://localhost:8080/test-login`. This seeds a fake session so all authenticated pages are accessible. Without this step, navigating to any protected page (e.g. `/statements`) will redirect to the Xero OAuth login, which cannot be completed in Playwright.

Full details (prerequisites, env vars, limitations) are in `agent_docs/browser_testing.md`.

## Progressive Documentation
Each repo may include `agent_docs/` with deeper context (read only when relevant), e.g.:
- `agent_docs/project.md` (purpose + architecture + repo-specific workflow)
- `agent_docs/testing.md`
- `agent_docs/security.md`
- `agent_docs/frontend.md`
- `agent_docs/documentation.md`
- `agent_docs/python_style.md`

**Important**:
 - Whenever you update the code, check README.md in the root to see if it needs updating with what you just added.
 - Make sure the updates you make are documented in detail, not just at a high level.
 - Make sure the README includes why certain decisions were made.
