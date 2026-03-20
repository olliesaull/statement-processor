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
        """Create a fresh Stripe Customer for a single checkout with that purchase's billing details.

        A new customer is created per checkout rather than reusing a persistent one,
        so each invoice is attached to a customer whose name, email, and address
        reflect exactly what the user entered — without overwriting any previous
        purchase's customer record.

        ``tenant_id`` is stored in metadata for traceability in the Stripe Dashboard.

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
        logger.info("Created per-checkout Stripe customer", tenant_id=tenant_id, stripe_customer_id=customer.id)
        return customer.id

    def create_checkout_session(self, *, customer_id: str, token_count: int, tenant_id: str, success_url: str, cancel_url: str) -> stripe.checkout.Session:
        """Create a Stripe Checkout Session for a one-time token purchase.

        Uses ``price_data`` (dynamic pricing) rather than fixed Price objects
        because token count is a free-form integer. The total ``unit_amount``
        is the full purchase price (``token_count x price_per_token``), with
        quantity set to 1 so the line item shows one purchase at the total
        price rather than N items at unit cost.

        Metadata carries ``tenant_id`` and ``token_count`` for the success
        route to verify ownership and determine how many tokens to credit.

        Args:
            customer_id: Stripe Customer ID to attach to the session.
            token_count: Number of tokens the user wants to purchase.
            tenant_id: Tenant making the purchase — stored in metadata for
                server-side verification on the success redirect.
            success_url: Stripe will redirect here after successful payment.
                Must include the ``{CHECKOUT_SESSION_ID}`` template literal.
            cancel_url: Stripe will redirect here if the user cancels.

        Returns:
            The created Stripe Checkout Session object (contains ``.url`` for
            the hosted payment page redirect).
        """
        unit_amount = token_count * STRIPE_PRICE_PER_TOKEN_PENCE
        return stripe.checkout.Session.create(
            customer=customer_id,
            mode="payment",
            invoice_creation={"enabled": True},
            billing_address_collection="auto",
            line_items=[{"price_data": {"currency": STRIPE_CURRENCY, "product": STRIPE_PRODUCT_ID, "unit_amount": unit_amount}, "quantity": 1}],
            metadata={"tenant_id": tenant_id, "token_count": str(token_count)},
            success_url=success_url,
            cancel_url=cancel_url,
        )

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
