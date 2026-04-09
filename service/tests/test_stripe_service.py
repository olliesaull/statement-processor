"""Unit tests for StripeService — customer creation and checkout session creation.

All Stripe SDK calls are mocked so no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import stripe_service as stripe_service_module
from stripe_service import StripeService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_customer(customer_id: str = "cus_test123") -> MagicMock:
    """Build a minimal Stripe Customer mock."""
    customer = MagicMock()
    customer.id = customer_id
    return customer


def _make_session(session_id: str = "cs_test_abc", url: str = "https://checkout.stripe.com/pay/cs_test_abc") -> MagicMock:
    """Build a minimal Stripe Checkout Session mock."""
    session = MagicMock()
    session.id = session_id
    session.url = url
    return session


# ---------------------------------------------------------------------------
# create_customer
# ---------------------------------------------------------------------------


def test_create_customer_creates_with_billing_details() -> None:
    """create_customer must call stripe.Customer.create with name, email, address, and tenant_id metadata."""
    new_customer = _make_customer("cus_new_per_checkout")
    address = {"line1": "1 Test St", "line2": "", "city": "London", "state": "", "postal_code": "SW1A 1AA", "country": "GB"}

    with patch.object(stripe_service_module.stripe.Customer, "create", return_value=new_customer) as mock_create:
        service = StripeService()
        result = service.create_customer(name="Acme Ltd", email="billing@acme.com", address=address, tenant_id="tenant-1")

    assert result == "cus_new_per_checkout"
    mock_create.assert_called_once_with(name="Acme Ltd", email="billing@acme.com", address=address, metadata={"tenant_id": "tenant-1"})


def test_create_customer_includes_tenant_id_in_metadata() -> None:
    """tenant_id must be stored in metadata so purchases are traceable in the Stripe Dashboard."""
    new_customer = _make_customer("cus_meta_test")
    address = {"line1": "2 Example Rd", "line2": "", "city": "", "state": "", "postal_code": "EC1A 1BB", "country": "GB"}

    with patch.object(stripe_service_module.stripe.Customer, "create", return_value=new_customer) as mock_create:
        service = StripeService()
        service.create_customer(name="Test Co", email="test@testco.com", address=address, tenant_id="tenant-xyz")

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["metadata"]["tenant_id"] == "tenant-xyz"


# ---------------------------------------------------------------------------
# update_customer
# ---------------------------------------------------------------------------


def test_update_customer_updates_billing_details() -> None:
    """update_customer should call stripe.Customer.modify with the new details."""
    with patch.object(stripe_service_module.stripe.Customer, "modify") as mock_modify:
        service = StripeService()
        address = {"line1": "3 New St", "line2": "", "city": "London", "state": "", "postal_code": "SW1A 2AA", "country": "GB"}
        service.update_customer(customer_id="cus_existing", name="New Name", email="new@email.com", address=address)

    mock_modify.assert_called_once_with("cus_existing", name="New Name", email="new@email.com", address=address)


# ---------------------------------------------------------------------------
# create_checkout_session — price calculation
# ---------------------------------------------------------------------------


def test_create_checkout_session_computes_correct_unit_amount(monkeypatch) -> None:
    """unit_amount must equal token_count x STRIPE_PRICE_PER_TOKEN_PENCE."""
    mock_session = _make_session()
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRICE_PER_TOKEN_PENCE", 10)
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRODUCT_ID", "prod_test")
    monkeypatch.setattr(stripe_service_module, "STRIPE_CURRENCY", "gbp")

    with patch.object(stripe_service_module.stripe.checkout.Session, "create", return_value=mock_session) as mock_create:
        service = StripeService()
        service.create_checkout_session(
            customer_id="cus_test", token_count=50, tenant_id="tenant-1", success_url="https://example.com/success?session_id={CHECKOUT_SESSION_ID}", cancel_url="https://example.com/cancel"
        )

    call_kwargs = mock_create.call_args.kwargs
    line_items = call_kwargs["line_items"]
    assert len(line_items) == 1
    # 50 tokens x 10 pence = 500 pence (£5.00)
    assert line_items[0]["price_data"]["unit_amount"] == 500
    assert line_items[0]["quantity"] == 1


def test_create_checkout_session_passes_correct_metadata(monkeypatch) -> None:
    """tenant_id and token_count must be present in session metadata."""
    mock_session = _make_session()
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRICE_PER_TOKEN_PENCE", 10)
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRODUCT_ID", "prod_test")
    monkeypatch.setattr(stripe_service_module, "STRIPE_CURRENCY", "gbp")

    with patch.object(stripe_service_module.stripe.checkout.Session, "create", return_value=mock_session) as mock_create:
        service = StripeService()
        service.create_checkout_session(
            customer_id="cus_test", token_count=100, tenant_id="tenant-abc", success_url="https://example.com/success?session_id={CHECKOUT_SESSION_ID}", cancel_url="https://example.com/cancel"
        )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["metadata"]["tenant_id"] == "tenant-abc"
    assert call_kwargs["metadata"]["token_count"] == "100"


def test_create_checkout_session_uses_payment_mode(monkeypatch) -> None:
    """Checkout sessions must use mode='payment' (not subscription)."""
    mock_session = _make_session()
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRICE_PER_TOKEN_PENCE", 10)
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRODUCT_ID", "prod_test")
    monkeypatch.setattr(stripe_service_module, "STRIPE_CURRENCY", "gbp")

    with patch.object(stripe_service_module.stripe.checkout.Session, "create", return_value=mock_session) as mock_create:
        service = StripeService()
        service.create_checkout_session(customer_id="cus_test", token_count=10, tenant_id="tenant-1", success_url="https://example.com/s", cancel_url="https://example.com/c")

    assert mock_create.call_args.kwargs["mode"] == "payment"


def test_create_checkout_session_enables_invoice_creation(monkeypatch) -> None:
    """Invoice creation must be enabled so customers receive receipts."""
    mock_session = _make_session()
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRICE_PER_TOKEN_PENCE", 10)
    monkeypatch.setattr(stripe_service_module, "STRIPE_PRODUCT_ID", "prod_test")
    monkeypatch.setattr(stripe_service_module, "STRIPE_CURRENCY", "gbp")

    with patch.object(stripe_service_module.stripe.checkout.Session, "create", return_value=mock_session) as mock_create:
        service = StripeService()
        service.create_checkout_session(customer_id="cus_test", token_count=10, tenant_id="tenant-1", success_url="https://example.com/s", cancel_url="https://example.com/c")

    assert mock_create.call_args.kwargs["invoice_creation"] == {"enabled": True}


# ---------------------------------------------------------------------------
# retrieve_session
# ---------------------------------------------------------------------------


def test_retrieve_session_delegates_to_stripe_sdk() -> None:
    """retrieve_session should call stripe.checkout.Session.retrieve with the given ID."""
    mock_session = _make_session("cs_retrieve_test")

    with patch.object(stripe_service_module.stripe.checkout.Session, "retrieve", return_value=mock_session) as mock_retrieve:
        service = StripeService()
        result = service.retrieve_session("cs_retrieve_test")

    mock_retrieve.assert_called_once_with("cs_retrieve_test")
    assert result == mock_session
