# AGENTS.md
# Guidance for ChatGPT Codex agents working in this repository.
#
# Primary goal: produce production-quality code changes with excellent documentation,
# while keeping behaviour correct and stable.

## Documentation style (mandatory)

### Function / method docstrings: Structured (Google-ish)
Use this exact structure:

- One-line summary sentence.
- Blank line.
- Args: section
- Returns: section
- Raises: section (ONLY if applicable)

Example:

"""
Do one-line summary.

Args:
    foo: Description.
    bar: Description.

Returns:
    Description.
"""

Rules:
- Always use triple double-quotes.
- Always include Args/Returns for non-trivial functions.
- Keep the summary short and declarative ("Build ...", "Fetch ...", "Validate ...").
- If a function has side effects or important constraints, add a brief sentence after the summary.
- Prefer clarity over brevity, but avoid writing essays.
- Do NOT add long multi-paragraph docstrings unless the function is genuinely complex.
- If a function is public API, include at least one usage example if it would be helpful.

### Class docstrings: Structured + system intent
Use a structured docstring that explains:
1) what the class represents,
2) what subsystem it belongs to / why it exists,
3) important attributes.

Example:

"""
Represents an extracted supplier statement.

This model represents the canonical output of the Textraction pipeline
and is stored in DynamoDB.

Attributes:
    tenant_id: Partition key.
    statement_id: Statement identifier.
    supplier_name: Display name from statement metadata.
"""

Rules:
- Keep it practical and system-oriented.
- If this is a model persisted to DynamoDB/S3/etc, mention where/how.
- Use Attributes: for important fields (not necessarily every field).

## Comments (hashtags): Chatty "why" comments (preferred)
Use explanatory comments that document intent and rationale, especially where:
- the code handles messy real-world inputs,
- there are non-obvious business rules,
- there are important edge cases,
- behaviour is constrained by AWS service quirks or limits.

Preferred comment style:

# The PDF can come from users, so filenames are messy (spaces, emojis, etc).
# We normalise to a stable, URL-safe format so links don't break later.

Rules:
- Comments should explain WHY, not what the code obviously does.
- Avoid obvious comments (e.g., "# increment i").
- Use short multi-line comments rather than long single lines.
- Use NOTE: or IMPORTANT: only when necessary.

## When to add documentation (required)
Whenever you:
- add a new function/class/module,
- change behaviour,
- add a non-trivial condition/branch,
- add or change any data model fields,
- add a config option / env var,
- change any AWS integration behaviour.

## Type hints & clarity
- Always add type hints for function signatures.
- Prefer explicit types where helpful (e.g., `dict[str, Any]` over bare `dict`).
- If a return type is complex, add a short docstring note clarifying structure.

## Behaviour preservation
- Never change behaviour just to improve documentation or style.
- If behaviour changes are required for correctness, clearly document the change.

## Safety / scope rules
- Do not add new production dependencies unless explicitly requested.
- Do not perform large refactors unless asked.
- If intent is ambiguous, ask or leave a NOTE explaining the assumption.

## Quality bar before final output
Where possible, run / ensure compatibility with:
- `ruff check .`
- `ruff format .`

If tests cannot be run, state what you would have run and why.
