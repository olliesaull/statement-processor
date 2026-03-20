"""Unit tests for StripeService — customer management and checkout session creation.

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


def _make_search_result(customers: list[MagicMock]) -> MagicMock:
    """Build a Stripe Customer.search result mock."""
    result = MagicMock()
    result.data = customers
    return result


def _make_session(session_id: str = "cs_test_abc", url: str = "https://checkout.stripe.com/pay/cs_test_abc") -> MagicMock:
    """Build a minimal Stripe Checkout Session mock."""
    session = MagicMock()
    session.id = session_id
    session.url = url
    return session


# ---------------------------------------------------------------------------
# get_or_create_customer
# ---------------------------------------------------------------------------


def test_get_or_create_customer_returns_existing_customer(monkeypatch) -> None:
    """When Stripe already has a customer for this tenant, return its ID without creating a new one."""
    existing_customer = _make_customer("cus_existing")
    search_result = _make_search_result([existing_customer])

    with patch.object(stripe_service_module.stripe.Customer, "search", return_value=search_result) as mock_search, patch.object(stripe_service_module.stripe.Customer, "create") as mock_create:
        service = StripeService()
        result = service.get_or_create_customer(tenant_id="tenant-1", name="Acme Ltd", email="user@acme.com")

    assert result == "cus_existing"
    mock_search.assert_called_once()
    mock_create.assert_not_called()


def test_get_or_create_customer_creates_new_customer_when_none_found(monkeypatch) -> None:
    """When no customer exists for this tenant, create one with correct attributes."""
    new_customer = _make_customer("cus_new123")
    search_result = _make_search_result([])  # empty — no existing customer

    with (
        patch.object(stripe_service_module.stripe.Customer, "search", return_value=search_result),
        patch.object(stripe_service_module.stripe.Customer, "create", return_value=new_customer) as mock_create,
    ):
        service = StripeService()
        result = service.get_or_create_customer(tenant_id="tenant-2", name="Beta Ltd", email="user@beta.com")

    assert result == "cus_new123"
    mock_create.assert_called_once_with(name="Beta Ltd", email="user@beta.com", metadata={"tenant_id": "tenant-2"})


def test_get_or_create_customer_search_uses_tenant_id_query() -> None:
    """The customer search query must key on tenant_id metadata, not email."""
    search_result = _make_search_result([_make_customer("cus_found")])

    with patch.object(stripe_service_module.stripe.Customer, "search", return_value=search_result) as mock_search:
        service = StripeService()
        service.get_or_create_customer(tenant_id="tenant-xyz", name="XYZ Corp", email="")

    call_kwargs = mock_search.call_args
    query: str = call_kwargs.kwargs.get("query") or (call_kwargs.args[0] if call_kwargs.args else "")
    assert "tenant-xyz" in query


def test_get_or_create_customer_empty_email_is_accepted() -> None:
    """An empty email (Xero session did not expose one) should not raise."""
    new_customer = _make_customer("cus_noemail")
    search_result = _make_search_result([])

    with (
        patch.object(stripe_service_module.stripe.Customer, "search", return_value=search_result),
        patch.object(stripe_service_module.stripe.Customer, "create", return_value=new_customer) as mock_create,
    ):
        service = StripeService()
        result = service.get_or_create_customer(tenant_id="tenant-3", name="No Email Ltd", email="")

    assert result == "cus_noemail"
    mock_create.assert_called_once_with(name="No Email Ltd", email="", metadata={"tenant_id": "tenant-3"})


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
