# Testing Guide

This repository has three test layers:
- Service unit tests in `service/tests/`
- Textraction Lambda unit tests in `lambda_functions/textraction_lambda/tests/`
- End-to-end Playwright tests in `service/playwright_tests/`

Use this document when changing behavior, fixing bugs, or adding features.

## Test Commands

### Service unit tests
- `cd service && python3.13 -m pytest tests`

### Textraction Lambda unit tests
- `cd lambda_functions/textraction_lambda && python3.13 -m pytest tests`

### Playwright end-to-end tests
- `cd service && python3.13 -m pytest -vv -s --tb=long playwright_tests/tests/e2e/test_statement_flow.py --headed`

## About `make dev`

- Project policy still requires running `make dev` after Python changes.
- Current Make targets are non-blocking (`|| true`), so failures may not stop the command.
- Always review command output and run targeted pytest commands for the components you changed.

## When to Add or Update Tests

Add or update tests when you:
- Fix a bug (add a regression test).
- Change behavior (update expected outputs/flows).
- Add a feature (happy path and edge cases).

Skip new tests only for purely mechanical changes with no behavior impact.

## What to Assert

Prefer contract-level assertions:
- Input to output behavior
- Error behavior (status, exception type, message where relevant)
- Boundary handling (empty, missing, malformed, duplicate, unexpected)

Avoid coupling tests to incidental implementation details.

## Mocking Rules

Mock external boundaries, not internal helpers:
- Xero API calls
- AWS SDK calls (S3, DynamoDB, Textract, Step Functions)
- Filesystem/network/time when needed for determinism

Keep returned payloads realistic so tests validate production-shaped data.

## Determinism Requirements

- No real network calls in unit tests.
- Control clocks/time where logic depends on expiration or polling.
- Avoid randomness unless seeded and intentional.
