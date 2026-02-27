# Documentation Standards

This document defines documentation expectations for this repository.
Consult it when adding or modifying code.

## Docstrings

Docstrings should explain intent and contracts (not narrate obvious code).
Functions and methods should include:

- A short, declarative summary.
- Argument descriptions (when non-trivial).
- Return description (when non-obvious).
- Raised exceptions (when meaningful).

Keep docstrings focused on intent and contracts.
Avoid writing essays.

## Class Docstrings

Class docstrings should explain:

- What the class represents.
- Why it exists in the system.
- Important attributes or invariants (when relevant).

Keep them practical and system-oriented.

## Comments

Comments should explain **why**, not what.

Add comments when:
- Handling edge cases.
- Encoding non-obvious business rules.
- Working around service constraints.
- Making trade-offs.

Avoid obvious comments.

Preferred comment style:

- The PDF can come from users, so filenames are messy (spaces, emojis, etc).
- We normalise to a stable, URL-safe format so links don't break later.

## Project-Specific Documentation Requirements

Update docs when behavior changes in:
- Route/auth/session behavior (`service/app.py`, `service/config.py`, `service/utils/auth.py`)
- Extraction contracts (`lambda_functions/textraction_lambda/core/*`)
- Persistence contracts (DynamoDB item shapes, S3 key layouts, Step Functions payloads)
- Operator workflows (sync, upload, reconciliation, export)

When these change:
- Update `agent_docs/project.md` with concrete flow/contract changes.
- Update `README.md` if user-facing or operator behavior changed.
- Explain **why** the design/behavior changed, not only what changed.

## When to Add Documentation

Add or update documentation when:

- Adding new public functions or classes.
- Changing behaviour.
- Introducing non-trivial logic.
- Modifying data models or configuration.

## Behaviour Preservation

Do not change behaviour solely for documentation or style.
If behaviour changes, make it explicit.
