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
