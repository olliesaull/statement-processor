# Agent Guidelines

This file defines cross-project rules for AI coding agents.
Path-scoped project rules live in `.claude/rules/` and load automatically based on file context.

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
- Run `make` commands from within `service/` or `lambda_functions/extraction_lambda/`, not from the repo root. The Makefile uses `find .` relative to the working directory, so running from root breaks pylint module resolution and produces spurious duplicate-code and import errors. The exception is `make run-app`, which is run from the repo root.
- This runs formatting and linting. Work is not complete unless it passes.
- Do not suppress lint errors without justification.
- If you cannot run commands, state what you would have run and why.
- Note: current Make targets are non-blocking (`|| true`), so review output carefully and run targeted tests for touched modules.

## Code Expectations

- Readable, maintainable, testable, and secure.
- Minimal and targeted — avoid large refactors unless requested.
- Do not introduce new production dependencies unless explicitly requested.
- If behaviour changes, document it clearly. Do not change behaviour solely for style/cleanup.

## Documentation

Code must be documented — see `.claude/rules/documentation.md` for docstring and comment standards.

## Dependencies
- Production deps: `requirements.txt`
- Dev-only deps: `requirements-dev.txt`
- If dependencies change, run `make update-venv` if present.

## Logging (important)

- Prefer structured logs (key/value context).
- Log “why” and relevant identifiers (tenant_id, statement_id, request_id, etc).
- Avoid logging secrets, tokens, or full sensitive documents.
- Use aws_lambda_powertools style

## Plans

- Make the plan extremely concise. Sacrifice grammar for the sake of concision.
- At the end of each plan, give me a list of unanswered questions to answer, if any.
- Plans are saved to `./plans/` which is **gitignored**. Do not attempt to `git add` or commit plan files.

## Decision Log

A decision log is maintained at `docs/decisions/log.md`. This records significant decisions — architectural choices, design tradeoffs, security decisions, and convention choices.

- **Before flagging something as an issue during review**, check the decision log to see if it was a conscious decision.
- **When a decision is made** (during planning, execution, review, or any standalone session), append an entry to the log. This includes: choosing between technical approaches, accepting a tradeoff, establishing a new convention, or deciding NOT to do something.
- **Do not log routine implementation details** — only decisions where there was a genuine choice between alternatives or a deliberate tradeoff.

**Important**:
 - Whenever you update the code, check README.md in the root to see if it needs updating with what you just added.
 - Make sure the updates you make are documented in detail, not just at a high level.
 - Make sure the README includes why certain decisions were made.
