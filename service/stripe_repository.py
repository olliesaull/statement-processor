"""DynamoDB operations for Stripe checkout state.

Provides idempotent processing records on StripeEventStoreTable
and caches the Stripe customer ID on TenantBillingTable.
"""

from datetime import UTC, datetime

from config import stripe_event_store_table as _event_store
from config import tenant_billing_table as _billing_table
from logger import logger


class StripeRepository:
    """Manages Stripe-related state in DynamoDB.

    Two tables are involved:

    * ``StripeEventStoreTable`` — idempotency records keyed by checkout session
      ID so a page refresh on ``/checkout/success`` does not double-credit.
    * ``TenantBillingTable`` — stores the cached ``StripeCustomerID`` to avoid
      a Stripe Customer search on every subsequent purchase.
    """

    @classmethod
    def is_session_processed(cls, session_id: str) -> bool:
        """Return True if this checkout session has already been credited.

        Performs a lightweight GetItem (projection only) to avoid reading
        the full record when we only need existence.

        Args:
            session_id: Stripe checkout session ID (``cs_xxx``).

        Returns:
            ``True`` if a processed record exists for this session.
        """
        response = _event_store.get_item(Key={"StripeEventID": session_id}, ProjectionExpression="StripeEventID")
        return "Item" in response

    @classmethod
    def record_processed_session(cls, *, session_id: str, tenant_id: str, tokens_credited: int, ledger_entry_id: str) -> None:
        """Record a completed checkout session to prevent double-crediting on refresh.

        Written **after** tokens have been credited so a partial failure
        (write succeeds but credit fails) is impossible — the credit always
        happens before the idempotency record is stored.

        Args:
            session_id: Stripe checkout session ID (``cs_xxx``) — used as PK.
            tenant_id: Tenant that made the purchase.
            tokens_credited: Number of tokens granted in this purchase.
            ledger_entry_id: The ledger entry ID (``purchase#<session_id>``)
                stored for audit cross-reference.
        """
        processed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        _event_store.put_item(
            Item={
                "StripeEventID": session_id,
                "EventType": "checkout.session.completed",
                "TenantID": tenant_id,
                "TokensCredited": tokens_credited,
                "LedgerEntryID": ledger_entry_id,
                "ProcessedAt": processed_at,
            }
        )
        logger.info("Recorded processed Stripe session", session_id=session_id, tenant_id=tenant_id, tokens_credited=tokens_credited)

    @classmethod
    def get_processed_session(cls, session_id: str) -> dict | None:
        """Retrieve a processed session record (to show success page on refresh).

        Used when ``is_session_processed`` returns ``True`` so the success
        page can display the original token count without re-crediting.

        Args:
            session_id: Stripe checkout session ID (``cs_xxx``).

        Returns:
            The stored DynamoDB item dict, or ``None`` if not found (tiny race
            window between ``is_session_processed`` and this call).
        """
        response = _event_store.get_item(Key={"StripeEventID": session_id})
        return response.get("Item")

    @classmethod
    def get_cached_customer_id(cls, tenant_id: str) -> str | None:
        """Read StripeCustomerID from TenantBillingTable.

        Returns ``None`` if the attribute is not set, meaning the tenant has
        never completed a Stripe checkout (or the cache was not written yet).

        Args:
            tenant_id: Xero tenant (organisation) ID.

        Returns:
            Stripe Customer ID (``cus_xxx``) or ``None``.
        """
        response = _billing_table.get_item(Key={"TenantID": tenant_id}, ProjectionExpression="StripeCustomerID")
        item = response.get("Item")
        if not item:
            return None
        return item.get("StripeCustomerID") or None

    @classmethod
    def cache_customer_id(cls, tenant_id: str, stripe_customer_id: str) -> None:
        """Write StripeCustomerID to TenantBillingTable for future checkout sessions.

        Called at checkout-creation time (before payment) so subsequent
        purchases reuse the same Stripe Customer without a search, even if
        the user abandons the current checkout. Also called as a fallback on
        the success redirect. UpdateItem with the same value is harmless.

        Args:
            tenant_id: Xero tenant (organisation) ID.
            stripe_customer_id: Stripe Customer ID to cache (``cus_xxx``).
        """
        _billing_table.update_item(Key={"TenantID": tenant_id}, UpdateExpression="SET StripeCustomerID = :cid", ExpressionAttributeValues={":cid": stripe_customer_id})
        logger.info("Cached Stripe customer ID", tenant_id=tenant_id, stripe_customer_id=stripe_customer_id)
