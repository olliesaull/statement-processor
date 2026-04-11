"""OAuth client and URL helpers.

Extracted from ``app.py`` to break the circular import between ``app``
and ``routes/auth``.  The ``init_oauth`` function must be called once
during application startup (in ``app.py``) before any route handler
accesses ``oauth``.
"""

import os

from authlib.integrations.flask_client import OAuth
from flask import Flask

from config import CLIENT_ID, CLIENT_SECRET, DOMAIN_NAME, STAGE
from utils.auth import scope_str

# Module-level reference populated by init_oauth() during app startup.
oauth: OAuth | None = None

XERO_OIDC_METADATA_URL = os.getenv("XERO_OIDC_METADATA_URL", "https://identity.xero.com/.well-known/openid-configuration")


def init_oauth(app: Flask) -> OAuth:
    """Create and register the Xero OAuth client on the Flask app.

    Args:
        app: The Flask application instance.

    Returns:
        The configured OAuth registry.
    """
    global oauth  # noqa: PLW0603
    oauth = OAuth(app)
    oauth.register(
        name="xero",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        # Load endpoints + JWKS from OIDC metadata so Authlib can validate id_tokens.
        server_metadata_url=XERO_OIDC_METADATA_URL,
        # Reuse the existing scope string to keep requested permissions unchanged.
        client_kwargs={"scope": scope_str()},
    )
    return oauth


def absolute_app_url(path: str) -> str:
    """Build an absolute application URL from the configured public hostname.

    This mirrors Numerint's simpler Python-side host handling: local
    development uses ``http://localhost:<port>``, while non-local stages always
    generate ``https://<DOMAIN_NAME>`` URLs.

    Args:
        path: The URL path component (e.g. ``/callback``).

    Returns:
        Fully qualified URL string.
    """
    if STAGE == "local":
        local_port = os.getenv("PORT", "8080")
        return f"http://{DOMAIN_NAME}:{local_port}{path}"
    return f"https://{DOMAIN_NAME}{path}"
