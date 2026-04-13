# Rules Index

| File | Description | Scoped to |
|------|-------------|-----------|
| [project.md](project.md) | Architecture, blueprints, data contracts, core flows, directory responsibilities | `**/*.py`, `**/*.html`, `**/*.js`, `**/*.css`, `cdk/**/*`, `service/**/*`, `lambda_functions/**/*` |
| [security.md](security.md) | Auth model, session security, upload boundaries, secrets/logging, IAM isolation | Auth/session/upload/config files, CDK, Lambda |
| [testing.md](testing.md) | Test layers, commands, mocking rules, determinism, when to add tests | `**/test_*.py`, `**/tests/**/*.py`, `conftest.py`, `**/playwright_tests/**/*` |
| [frontend.md](frontend.md) | HTML/CSS/JS guidelines, SEO, accessibility, architecture constraints | `**/*.html`, `**/*.css`, `**/*.js`, `service/templates/**/*`, `service/static/**/*` |
| [documentation.md](documentation.md) | Docstring/comment standards, when and what to document | `**/*.py`, `**/*.md`, `**/*.html` |
| [python-style.md](python-style.md) | Type hints, structured data, enums, repo-specific contracts, common foot-guns | `**/*.py` |
| [browser-testing.md](browser-testing.md) | Playwright MCP setup, authentication, local testing workflow, limitations | `service/playwright_tests/**/*`, `service/app.py`, `service/templates/**/*` |
| [deployment.md](deployment.md) | Dockerfile, nginx config, query string allowlist, route regeneration checklist | `service/routes/**/*.py`, `service/Dockerfile`, `service/nginx*`, `service/app.py`, `service/utils/auth.py` |
