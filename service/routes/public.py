"""Public page routes -- unauthenticated marketing and content pages.

Serves the landing page, about, instructions, FAQ, pricing, and legal
pages (privacy, terms, cookies).  None of these routes require
authentication.
"""

from flask import Blueprint, render_template

from logger import logger
from utils.auth import route_handler_logging
from utils.content import load_faqs, load_legal_page

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
@route_handler_logging
def index():
    """Render the landing page."""
    logger.info("Rendering index")
    return render_template("index.html")


@public_bp.route("/about")
@route_handler_logging
def about():
    """Render the about page."""
    return render_template("about.html")


@public_bp.route("/instructions")
@route_handler_logging
def instructions():
    """Render the user instructions page."""
    return render_template("instructions.html")


@public_bp.route("/faq")
@route_handler_logging
def faq():
    """Render the FAQ page with collapsible sections loaded from YAML+markdown."""
    faqs = load_faqs()
    return render_template("faq.html", faqs=faqs)


@public_bp.route("/pricing")
@route_handler_logging
def pricing():
    """Render the public-facing pricing explanation page (no login required).

    Intentionally has no ``@xero_token_required`` so prospective customers
    can see pricing before signing up.
    """
    return render_template("pricing.html")


@public_bp.route("/privacy")
@route_handler_logging
def privacy():
    """Render the privacy policy page from markdown content."""
    content = load_legal_page("privacy.md")
    return render_template("privacy.html", content=content)


@public_bp.route("/terms")
@route_handler_logging
def terms():
    """Render the terms and conditions page from markdown content."""
    content = load_legal_page("terms.md")
    return render_template("terms.html", content=content)


@public_bp.route("/cookies")
@route_handler_logging
def cookies():
    """Render the cookie policy and consent page."""
    return render_template("cookies.html")
