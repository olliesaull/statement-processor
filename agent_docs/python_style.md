# Python Style Guide (Project Standard)

This document contains Python style and coding standards for this repository.
It is not injected into every agent session. Only consult it when making Python changes.

## General Principles

- Prefer clarity over cleverness.
- Prefer small, incremental changes over wide refactors.
- Keep functions small and single-purpose.
- Preserve behaviour unless a change is explicitly required.
- Make failure modes explicit (raise meaningful errors, log with context).
- Prefer targeted edits over broad refactors.

## Types & Interfaces

### Type hints
- Always add type hints to function signatures (args + return type).
- Prefer modern built-ins: `list`, `dict`, `set` rather than `typing.List`, etc.
- Avoid `Any` unless it's genuinely a boundary (e.g., external JSON payload).

### Structured data over raw dicts
Prefer one of:
- Pydantic models (external inputs, JSON, API payloads)
- Dataclasses (internal data containers)
- TypedDict (when dict shape is required, but you want typing)

In this repo, plain dict payloads are still common at boundaries (Flask request/session, Textract/Xero responses, DynamoDB items). Keep boundary handling explicit and typed where practical.

### Enums for fixed values
- For small fixed vocabularies, prefer `Enum` over string literals.

### Protocols for Interfaces
- Use `Protocol` for structural typing (duck typing with type safety):

## Repo-Specific Contracts to Preserve

- Do not silently change DynamoDB boolean-string contracts (`"true"` / `"false"`) without coordinated migration.
- Keep `statement_item_id` format stable (`<statement_id>#item-XXXX`) unless explicitly changing persistence contracts.
- Preserve API auth response shape for `/api/...` endpoints (`401` JSON semantics are relied on by frontend JS).

## "Don'ts" (Common Foot-Guns)

- Don't add production deps without explicit request.
- Don't disable lint rules unless there's a documented reason.
- Don't change behaviour for style-only reasons.
- Don't repeat yourself. Extract common logic and adhere to DRY principles.
