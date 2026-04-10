"""SEO and machine-readable routes.

Serves robots.txt, sitemap.xml, llms.txt, the /healthz liveness probe,
and the /favicon.ico placeholder.  All routes are unauthenticated.
"""

from flask import Blueprint, Response, current_app

from config import DOMAIN_NAME
from utils.content import load_llms_txt

seo_bp = Blueprint("seo", __name__)


def _get_authenticated_routes() -> list[str]:
    """Return sorted list of authenticated route paths for the robots.txt disallow block.

    Inspects each route's view function for the ``_requires_auth`` attribute
    set by ``xero_token_required`` and ``active_tenant_required``.
    """
    authenticated = set()
    for rule in current_app.url_map.iter_rules():
        view_func = current_app.view_functions.get(rule.endpoint)
        if view_func and getattr(view_func, "_requires_auth", False):
            # Use the base path (strip dynamic segments) so /statement/<id> becomes /statement/
            path = rule.rule.split("<")[0]
            authenticated.add(path)
    return sorted(authenticated)


# Routes excluded from the sitemap even though they are public (system/utility routes).
_SITEMAP_EXCLUDE = {"/healthz", "/login", "/logout", "/callback", "/robots.txt", "/sitemap.xml", "/llms.txt", "/favicon.ico", "/test-login"}


def _get_sitemap_routes() -> list[str]:
    """Return sorted list of public page paths suitable for the sitemap.

    A route is included when it is unauthenticated, accepts GET, has no
    dynamic segments, and is not in the system-route exclusion set.
    """
    pages = []
    for rule in current_app.url_map.iter_rules():
        if "<" in rule.rule:
            continue
        if rule.rule in _SITEMAP_EXCLUDE:
            continue
        if "GET" not in rule.methods:
            continue
        view_func = current_app.view_functions.get(rule.endpoint)
        if view_func and getattr(view_func, "_requires_auth", False):
            continue
        pages.append(rule.rule)
    return sorted(pages)


def _build_crawl_policy(header_comment: str) -> str:
    """Build a robots.txt body from the detected authenticated routes."""
    disallow_lines = "\n".join(f"Disallow: {path}" for path in _get_authenticated_routes())
    return f"""# {header_comment} for {DOMAIN_NAME}
# Public pages are allowed. Private/system routes are disallowed.

User-agent: *
Allow: /

{disallow_lines}

Sitemap: https://{DOMAIN_NAME}/sitemap.xml
"""


@seo_bp.route("/robots.txt")
def robots_txt():
    """Serve robots.txt with crawling policy for search engines."""
    return Response(_build_crawl_policy("Crawling policy"), mimetype="text/plain")


@seo_bp.route("/sitemap.xml")
def sitemap_xml():
    """Serve sitemap.xml listing all public pages."""
    lines = ["<?xml version='1.0' encoding='UTF-8'?>", "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"]
    for path in _get_sitemap_routes():
        lines.append(f"<url><loc>https://{DOMAIN_NAME}{path}</loc></url>")
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="text/xml")


@seo_bp.route("/llms.txt")
def llms_txt():
    """Serve llms.txt -- product overview for LLM consumption (llmstxt.org spec)."""
    return Response(load_llms_txt(), mimetype="text/plain")


@seo_bp.route("/healthz")
def healthz():
    """Return a minimal unauthenticated liveness response for App Runner."""
    return "", 200


@seo_bp.route("/favicon.ico")
def ignore_favicon():
    """Return empty 204 for favicon requests."""
    return "", 204
