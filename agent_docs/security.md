# Security Model & Guidelines

This document defines security-critical behavior for this repository.
Read it before changing auth, session handling, upload flows, API routes, or external integrations.

## Security Context

- The web app is deployed behind a Lambda Function URL configured with `auth_type=NONE`.
- This means perimeter auth is not provided by AWS; route-level controls in Flask are the main defense.
- Financial data and OAuth tokens are in scope, so fail-closed behavior is required.

## Required Route Protections

- Protected routes must use `@xero_token_required` from [`service/utils/auth.py`](service/utils/auth.py).
- Routes requiring an active tenant should also use `@active_tenant_required`.
- Routes that should not run during initial tenant load should use `@block_when_loading`.
- CSRF protection is enabled globally (`CSRFProtect(app)`), so form and JS POST flows must include valid CSRF tokens.

## Auth Behavior Contracts

- Missing/expired auth on UI routes redirects to `/login` (or `/cookies` when consent is missing).
- Missing/expired auth on `/api/...` routes returns `401` JSON, not redirect HTML.
- Cookie consent is mandatory for protected routes; API responses include a JSON redirect target for consent flow.

Do not change these semantics without updating frontend logic and docs.

## Session Security Contracts

- Session state is server-side in Valkey/Redis via Flask-Session (`SESSION_TYPE='redis'`, `SESSION_REDIS=redis.from_url(VALKEY_URL)`).
- Browser cookies only contain the signed session identifier; OAuth tokens and tenant payloads stay in Redis.
- `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`, and `SESSION_COOKIE_SAMESITE='Lax'` must remain enabled.
- `SESSION_TTL_SECONDS` sets `PERMANENT_SESSION_LIFETIME`; keep this aligned with expected idle session lifetime.
- `VALKEY_URL` must target a trusted Redis/Valkey endpoint (ElastiCache in AWS). Do not expose Redis publicly.

## Upload and Extraction Boundaries

- Uploads accept only PDFs (`application/pdf` + `.pdf`) and are size-limited via `MAX_CONTENT_LENGTH`.
- S3 keys are sanitized to avoid path traversal or separator injection.
- Textract and Xero responses are untrusted external data; validate/normalize before persistence.
- `table_to_json` requires configured `date_format`; this is a deliberate data-quality gate.

## Secrets and Logging

- Secrets/tokens must come from SSM/env, not source control.
- Never log:
  - OAuth access/refresh/id tokens
  - API secrets/credentials
  - Raw sensitive document contents unless explicitly redacted and justified
- Logs should include operational identifiers (tenant_id, statement_id, contact_id, job_id) for traceability.

## IAM and Data Isolation

- Keep least-privilege IAM access when changing CDK.
- Preserve tenant partitioning (`TenantID`) in DynamoDB access patterns.
- Do not introduce cross-tenant reads/writes in service or Lambda code paths.

## Security Tooling and Verification

- Run `make dev` and inspect output (targets are currently non-blocking).
- Run targeted tests for auth/session/upload changes:
  - `cd service && python3.13 -m pytest tests`
  - `cd lambda_functions/textraction_lambda && python3.13 -m pytest tests`
- Bandit findings must be fixed or explicitly justified.

## Change Review Checklist

- [ ] Protected routes still enforce auth/tenant/cookie consent correctly.
- [ ] API auth failures still return expected `401` JSON payloads.
- [ ] CSRF is preserved for all state-changing routes.
- [ ] No secrets or tokens are logged.
- [ ] Tenant isolation remains intact in all persistence calls.
- [ ] Any security-impacting behavior change is documented in README/agent docs.
