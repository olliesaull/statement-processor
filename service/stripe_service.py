"""Stripe API interactions for token purchases.

All stripe SDK calls are encapsulated here so they can be mocked
in tests without patching the module globally.
"""

import stripe

from config import STRIPE_API_KEY, get_envar
from logger import logger

# Set the API key once at module load time so all stripe calls use it.
stripe.api_key = STRIPE_API_KEY

STRIPE_PRODUCT_ID: str = get_envar("STRIPE_PRODUCT_ID")
STRIPE_PRICE_PER_TOKEN_PENCE: int = int(get_envar("STRIPE_PRICE_PER_TOKEN_PENCE"))
STRIPE_CURRENCY: str = get_envar("STRIPE_CURRENCY", "gbp")
STRIPE_MIN_TOKENS: int = int(get_envar("STRIPE_MIN_TOKENS", "10"))
STRIPE_MAX_TOKENS: int = int(get_envar("STRIPE_MAX_TOKENS", "10000"))


class StripeService:
    """Encapsulate Stripe API calls for customer management and checkout."""

    def create_customer(self, *, name: str, email: str, address: dict[str, str], tenant_id: str) -> str:
        """Create a Stripe Customer for this tenant.

        The customer is persisted in ``TenantBillingTable`` and reused across
        checkouts and subscriptions. Billing details can be updated later via
        ``update_customer`` (pay-as-you-go flow) or the Stripe Customer Portal
        (subscription flow).

        Args:
            name: Company or person name to appear on the invoice.
            email: Email address Stripe will send the finalised invoice PDF to.
            address: Billing address dict with Stripe field names:
                ``line1``, ``line2``, ``city``, ``state``, ``postal_code``, ``country``.
            tenant_id: Xero tenant (organisation) ID — stored in customer metadata.

        Returns:
            Stripe Customer ID (``cus_xxx``).
        """
        customer = stripe.Customer.create(name=name, email=email, address=address, metadata={"tenant_id": tenant_id})
        logger.info("Created Stripe customer", tenant_id=tenant_id, stripe_customer_id=customer.id)
        return customer.id

    def update_customer(self, *, customer_id: str, name: str, email: str, address: dict[str, str]) -> None:
        """Update an existing Stripe Customer's billing details.

        Called on repeat purchases to keep the customer record current.
        Invoices snapshot billing details at creation time, so updating
        here does not affect historical invoices (last-write-wins by design).

        Args:
            customer_id: Existing Stripe Customer ID (``cus_xxx``).
            name: Updated company or person name.
            email: Updated billing email.
            address: Updated billing address dict.
        """
        stripe.Customer.modify(customer_id, name=name, email=email, address=address)
        logger.info("Updated Stripe customer billing details", stripe_customer_id=customer_id)

    def create_checkout_session(self, *, customer_id: str, token_count: int, total_amount_pence: int, tenant_id: str, success_url: str, cancel_url: str) -> stripe.checkout.Session:
        """Create a Stripe Checkout Session for a one-time token purchase.

        Uses ``price_data`` (dynamic pricing) with the caller-provided total
        amount. The total is computed by PricingConfig using graduated tiers.
        Quantity is 1 so the line item shows one purchase at the total price.

        Args:
            customer_id: Stripe Customer ID to attach to the session.
            token_count: Number of tokens (stored in metadata for crediting).
            total_amount_pence: Total price in pence (computed by PricingConfig).
            tenant_id: Tenant making the purchase — stored in metadata.
            success_url: Redirect URL after payment (must include {CHECKOUT_SESSION_ID}).
            cancel_url: Redirect URL if user cancels.

        Returns:
            The created Stripe Checkout Session object.
        """
        return stripe.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            invoice_creation={"enabled": True},
            billing_address_collection="auto",
            line_items=[{"price_data": {"currency": STRIPE_CURRENCY, "product": STRIPE_PRODUCT_ID, "unit_amount": total_amount_pence}, "quantity": 1}],
            metadata={"tenant_id": tenant_id, "token_count": str(token_count)},
            success_url=success_url,
            cancel_url=cancel_url,
        )

    def create_subscription_checkout_session(
        self, *, customer_id: str, stripe_price_id: str, tenant_id: str, tier_id: str, token_count: int, success_url: str, cancel_url: str
    ) -> stripe.checkout.Session:
        """Create a Stripe Checkout Session for a subscription purchase.

        Uses mode='subscription' with a Stripe Price object (one per tier).
        Metadata carries tenant_id and tier_id for the webhook handler.
        """
        # NOTE: billing_address_collection is not set — Stripe Checkout shows address
        # fields by default with most payment methods but doesn't force them. The
        # customer can fill them in during checkout or later via the Customer Portal.
        # Add billing_address_collection="required" if tax compliance demands it. — reviewed 2026-04-13
        return stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": stripe_price_id, "quantity": 1}],
            subscription_data={"metadata": {"tenant_id": tenant_id, "tier_id": tier_id, "token_count": str(token_count)}},
            metadata={"tenant_id": tenant_id, "tier_id": tier_id},
            success_url=success_url,
            cancel_url=cancel_url,
        )

    def create_billing_portal_session(self, *, customer_id: str, return_url: str) -> stripe.billing_portal.Session:
        """Create a Stripe Customer Portal session for subscription management.

        Args:
            customer_id: Stripe Customer ID.
            return_url: URL to redirect to after the portal session.

        Returns:
            The Stripe Billing Portal Session object.
        """
        return stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)

    def retrieve_session(self, session_id: str) -> stripe.checkout.Session:
        """Retrieve a Stripe Checkout Session by ID.

        Called on the success redirect to verify ``payment_status == "paid"``
        and read ``metadata["token_count"]`` before crediting tokens.

        Args:
            session_id: Stripe checkout session ID (``cs_xxx``).

        Returns:
            The Stripe Checkout Session object.
        """
        return stripe.checkout.Session.retrieve(session_id)
