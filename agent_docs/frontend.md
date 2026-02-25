# Frontend Guidelines (HTML / CSS / JS)

This project uses a server-rendered Flask frontend with Jinja templates, Bootstrap 5, a single shared stylesheet, and small vanilla JS modules.

## Architecture Constraints

- Keep server-rendered pages (`service/templates/*.html`) as the source of truth.
- Keep static assets in `service/static/assets/` (no build pipeline).
- Do not introduce React/Vue/Svelte, bundlers, or Node-based asset tooling.
- Do not convert pages to SPA-style client routing.

## Existing Frontend Contracts

- Base layout and nav live in [`service/templates/base.html`](service/templates/base.html).
- Statement row status colors come from Python-injected CSS variables; do not hardcode duplicate color systems in templates/CSS.
- Tenant management sync UI depends on `/api/tenant-statuses` and `/api/tenants/<id>/sync` JSON contracts plus auth redirect behavior.
- Nav login/logout label toggling depends on helper cookie `session_is_set`; preserve this integration.

## HTML and Template Rules

- Use semantic structure and keep heading hierarchy sane.
- Keep business logic in Python; templates should mostly render prepared context.
- Reuse existing panel/table/button patterns before creating new variants.
- Preserve accessibility basics:
  - Valid button/input semantics
  - Label/input association
  - Useful `alt` text
  - Intentional ARIA usage only

## CSS Rules

- Prefer extending existing selectors in `service/static/assets/css/main.css`.
- Avoid page-unscoped global overrides that can regress other screens.
- Preserve the existing visual language unless redesign is explicitly requested.
- When changing statement row visuals, update the shared palette source in Python, not disconnected CSS constants.

## JavaScript Rules

- Keep JS progressive and focused on enhancement.
- Use small DOM-based scripts; avoid introducing heavy client frameworks.
- Maintain current auth/cookie consent handling semantics for API calls.
- Keep fetch flows resilient to 401 JSON/redirect responses as implemented in `main.js`.

## SEO Requirements

SEO is a core concern.

When creating or modifying pages:

- Include a meaningful `<title>`.
- Include a descriptive `<meta name="description">`.
- Maintain correct heading hierarchy (`h1 → h2 → h3`).
- Only one `<h1>` per page.
- Use descriptive link text.
- Add alt text to images that describes content meaningfully
- Ensure important content is server-rendered.
- Preserve canonical URLs and structured metadata if present.

Do not remove metadata or structured data unless explicitly instructed.

## Pre-Change Checklist

- [ ] Markup follows existing template/layout patterns.
- [ ] API-dependent JS behavior still matches backend response contracts.
- [ ] No unnecessary framework/tooling introduced.
- [ ] CSS changes are scoped and do not regress unrelated pages.
- [ ] Accessibility and keyboard usability remain intact.
