"""Checkout helpers for billing validation and Stripe session management.

Extracted from app.py to keep the route file focused on request handling.
These functions encapsulate billing field validation, Stripe customer
management, and token crediting logic used by the checkout routes.
"""

import secrets

import stripe

from billing_service import LAST_MUTATION_SOURCE_STRIPE_CHECKOUT, BillingService
from logger import logger
from pricing_config import PricingConfig
from stripe_repository import StripeRepository
from stripe_service import StripeService
from tenant_billing_repository import TenantBillingRepository


def validate_billing_fields(*, billing_name: str, billing_email: str, billing_line1: str, billing_postal_code: str, billing_country: str) -> list[str]:
    """Validate required billing fields and return names of missing ones.

    Args:
        billing_name: Customer name.
        billing_email: Customer email.
        billing_line1: Address line 1.
        billing_postal_code: Postal/zip code.
        billing_country: Country code.

    Returns:
        List of missing field names (empty if all present).
    """
    missing = []
    if not billing_name:
        missing.append("Name")
    if not billing_email:
        missing.append("Email")
    if not billing_line1:
        missing.append("Address line 1")
    if not billing_postal_code:
        missing.append("Postal code")
    if not billing_country:
        missing.append("Country")
    return missing


def resolve_or_create_stripe_customer(stripe_service: StripeService, *, tenant_id: str, billing_name: str, billing_email: str, address: dict[str, str]) -> str:
    """Look up or create a Stripe customer for the tenant.

    Checks DynamoDB for an existing Stripe customer ID.  If found, updates
    the customer's details; otherwise creates a new customer and persists
    the mapping.

    Args:
        stripe_service: Stripe service instance.
        tenant_id: Tenant making the purchase.
        billing_name: Customer name.
        billing_email: Customer email.
        address: Billing address dict with line1, line2, city, state,
            postal_code, and country keys.

    Returns:
        The Stripe customer ID.
    """
    existing_customer_id = TenantBillingRepository.get_stripe_customer_id(tenant_id)
    if existing_customer_id:
        stripe_service.update_customer(customer_id=existing_customer_id, name=billing_name, email=billing_email, address=address)
        return existing_customer_id

    customer_id = stripe_service.create_customer(name=billing_name, email=billing_email, address=address, tenant_id=tenant_id)
    TenantBillingRepository.set_stripe_customer_id(tenant_id, customer_id)
    return customer_id


def create_checkout_session(stripe_service: StripeService, *, customer_id: str, token_count: int, tenant_id: str, success_url: str, cancel_url: str) -> str | None:
    """Create a Stripe Checkout Session and return its URL.

    Calculates the graduated total price and delegates session creation
    to the Stripe service.

    Args:
        stripe_service: Stripe service instance.
        customer_id: Stripe customer ID.
        token_count: Number of tokens/pages being purchased.
        tenant_id: Tenant making the purchase.
        success_url: URL Stripe redirects to on success.
        cancel_url: URL Stripe redirects to on cancellation.

    Returns:
        The Stripe-hosted checkout URL, or None on Stripe error.
    """
    total_amount_pence = PricingConfig.calculate_total_pence(token_count)
    try:
        stripe_session = stripe_service.create_checkout_session(
            customer_id=customer_id, token_count=token_count, total_amount_pence=total_amount_pence, tenant_id=tenant_id, success_url=success_url, cancel_url=cancel_url
        )
        return stripe_session.url
    except stripe.StripeError:
        logger.exception("Failed to create Stripe checkout session", tenant_id=tenant_id)
        return None


def generate_checkout_failure_ref() -> str:
    """Generate a short hex reference for correlating checkout failure logs.

    Returns:
        A 16-character hex string.
    """
    return secrets.token_hex(8)


def credit_tokens_from_checkout(*, session_id: str, tenant_id: str, token_count: int) -> int:
    """Credit tokens to a tenant after a verified Stripe checkout.

    Records the effective rate for the audit trail and marks the Stripe
    session as processed so page refreshes don't re-credit.

    Args:
        session_id: Stripe checkout session ID.
        tenant_id: Tenant to credit.
        token_count: Number of tokens to add.

    Returns:
        The tenant's new token balance after crediting.
    """
    effective_rate = PricingConfig.effective_rate_pence(token_count)
    ledger_entry_id = f"purchase#{session_id}"
    BillingService.adjust_token_balance(tenant_id, token_count, source=LAST_MUTATION_SOURCE_STRIPE_CHECKOUT, ledger_entry_id=ledger_entry_id, price_per_token_pence=effective_rate)

    # Mark session as processed so page refreshes don't re-credit.
    StripeRepository.record_processed_session(session_id=session_id, tenant_id=tenant_id, tokens_credited=token_count, ledger_entry_id=ledger_entry_id)

    return TenantBillingRepository.get_tenant_token_balance(tenant_id)
