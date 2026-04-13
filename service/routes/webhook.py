"""Stripe webhook route — receives events from Stripe for subscription billing.

This Blueprint is intentionally separate from api_bp so that CSRF protection
can be exempted at the Blueprint level without affecting other API routes.
Stripe webhook requests come directly from Stripe's servers with no session
cookie or CSRF token — authentication is via signature verification using
the webhook signing secret instead.

Decision logged in docs/decisions/log.md.
"""

import stripe
from flask import Blueprint, request

from config import STRIPE_WEBHOOK_SECRET
from logger import logger
from stripe_webhook_handler import StripeWebhookHandler

webhook_bp = Blueprint("webhook", __name__)

_webhook_handler = StripeWebhookHandler()


@webhook_bp.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Receive Stripe webhook events for subscription billing.

    Signature verification uses the raw request body (not parsed JSON)
    to prevent key reordering from invalidating the signature.
    Stripe retries failed deliveries for up to 3 days with exponential backoff.
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("Stripe webhook signature verification failed")
        return "Invalid signature", 400

    _webhook_handler.handle_event(event)
    return "", 200
