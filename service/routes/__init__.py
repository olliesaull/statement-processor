"""Flask Blueprint route modules for the statement processor service.

Each sub-module defines a Blueprint that groups related routes:

- **public** -- unauthenticated marketing/content pages (/, /about, /faq, ...).
- **seo** -- machine-readable endpoints (robots.txt, sitemap.xml, llms.txt, /healthz).
- **auth** -- Xero OAuth login/logout/callback.
- **tenants** -- tenant management, selection, and disconnection.
- **statements** -- statement list, detail, upload, and deletion.
- **billing** -- token purchase, billing details, and Stripe checkout pages.
- **api** -- JSON API endpoints (tenant sync, upload preflight, checkout, banners).

Blueprints are registered on the Flask app in ``app.py``.
"""
