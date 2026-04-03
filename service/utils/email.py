"""Login notification email sender via AWS SES.

Sends a branded HTML email when a user logs in via Xero OAuth. The call
is fire-and-forget: failures are logged but never block the login flow.
Sending is skipped entirely outside production (STAGE != "prod").

This module reads STAGE from os.environ directly (not config.py) so it
can be imported in unit tests without triggering the SSM secrets fetch
that config.py performs at import time.
"""

import os
from pathlib import Path

import boto3
import jinja2

from logger import logger

_STAGE: str = os.environ.get("STAGE", "prod")

_ses_client = boto3.client("ses", region_name="eu-west-1")

_SENDER_EMAIL = "info@dotelastic.com"
_RECIPIENT_EMAIL = "ollie@dotelastic.com"

# Load email templates from the templates/email directory using a standalone
# Jinja2 environment so this module works without a Flask app context.
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
_jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=True)


def send_login_notification_email(tenant_name: str, user_name: str, user_email: str) -> None:
    """Send a login notification email via SES.

    Args:
        tenant_name: Name of the Xero tenant the user logged into.
        user_name: Full name of the authenticated user.
        user_email: Email address of the authenticated user.
    """
    if _STAGE != "prod":
        logger.debug("Skipping login notification email", stage=_STAGE)
        return

    try:
        template = _jinja_env.get_template("login_notification.html")
        html_body = template.render(tenant_name=tenant_name, user_name=user_name, user_email=user_email)
        _ses_client.send_email(
            Source=_SENDER_EMAIL,
            Destination={"ToAddresses": [_RECIPIENT_EMAIL]},
            Message={"Subject": {"Data": "Statement Processor Login", "Charset": "UTF-8"}, "Body": {"Html": {"Data": html_body, "Charset": "UTF-8"}}},
        )
        logger.info("Login notification email sent", user_email=user_email, tenant_name=tenant_name)
    except Exception:
        logger.exception("Failed to send login notification email", user_email=user_email, tenant_name=tenant_name)
