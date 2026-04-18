# Agent Guidelines

Canonical instructions file for AI coding agents in this repo. `CLAUDE.md` symlinks to this file so Claude Code, Codex, Copilot CLI, and Kiro all read the same source.

Path-scoped rules live in `.claude/rules/` and auto-load based on which files you're editing. See [`.claude/rules/index.md`](.claude/rules/index.md) for the full map.

## Environment
- All development is done in Linux (Ubuntu).
  - Mac: Ubuntu via Parallels
  - Windows: Ubuntu via WSL
- Always use bash/Linux commands. Do not use PowerShell or Windows-specific commands.

**WSL note**: agents sometimes misdetect the host as Windows. Use bash/Linux commands regardless.

## Python Version
- Projects target **Python 3.13** unless the repo explicitly states otherwise.
- Use explicit versioned commands (`python3.13 ...`) — never bare `python` or `python3`.

## Required Workflow After Code Changes

After modifying Python code, run `make dev`. This runs formatting and linting.

**Run `make` from `service/` or `lambda_functions/extraction_lambda/`, not from the repo root.** The Makefile uses `find .` relative to the working directory, so running from root breaks pylint module resolution and produces spurious duplicate-code and import errors. The one exception is `make run-app`, which runs from the repo root.

**Important — the current Make targets are non-blocking (`|| true`).** A green exit code does not mean clean output. Read the output and treat any warnings/errors as failures. Also run targeted tests for touched modules.

- Do not suppress lint errors without justification.
- If you cannot run commands, state what you would have run and why.

## Dependencies
- Production: `requirements.txt` — do not add new prod deps without an explicit request.
- Dev-only: `requirements-dev.txt`.
- If deps change, run `make update-venv` if present.
- Shared code (`sp_common`) lives in `common/sp_common`, installed editable. Import from `sp_common` — do not duplicate into `service/` or `lambda_functions/`.

## Logging

- Structured logs — pass context as kwargs, not f-strings: `logger.info("fetching statements", tenant_id=tenant_id, statement_id=stmt_id)`.
- Include the "why" and relevant identifiers (`tenant_id`, `statement_id`, `request_id`, `job_id`, etc.).
- Never log secrets, tokens, or full sensitive documents.
- Use the `aws_lambda_powertools`-style logger.

## Plans

- Concise. Sacrifice grammar for brevity.
- End each plan with any unanswered questions.
- Plans live in `./plans/` — **gitignored**. Do not `git add` or commit plan files.

## Documentation

When behaviour changes, check whether `README.md` needs updating and document **why** the change was made, not only what. Docstring and comment standards live in [`.claude/rules/documentation.md`](.claude/rules/documentation.md).

## Decision Log

`docs/decisions/log.md` records architectural choices, design tradeoffs, security decisions, and convention choices.

- **Before flagging something as an issue during review**, check the decision log — it may have been a conscious decision.
- **When making a decision** (planning, execution, review, or standalone session), append an entry. This includes choosing between approaches, accepting a tradeoff, establishing a convention, or deciding NOT to do something.
- Do not log routine implementation details — only genuine choices or deliberate tradeoffs.

## Deployment — Hard Rule

**Agents MUST NOT deploy to ANY AWS environment.**

Deployments touch production data and customer tenants, and are hard to reverse. Deployment is a human-authored action — if the user asks you to deploy, refuse and ask them to run the command themselves.
