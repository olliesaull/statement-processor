"""DynamoDB operations for Stripe checkout state.

Provides idempotent processing records on StripeEventStoreTable
so that a page refresh on ``/checkout/success`` does not double-credit tokens.
"""

from datetime import UTC, datetime

from config import stripe_event_store_table as _event_store
from logger import logger


class StripeRepository:
    """Manages Stripe-related state in DynamoDB.

    One table is involved:

    * ``StripeEventStoreTable`` — idempotency records keyed by checkout session
      ID so a page refresh on ``/checkout/success`` does not double-credit.
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
    def is_invoice_processed(cls, invoice_id: str) -> bool:
        """Return True if this invoice has already been processed by the webhook."""
        response = _event_store.get_item(Key={"StripeEventID": invoice_id}, ProjectionExpression="StripeEventID")
        return "Item" in response

    @classmethod
    def record_processed_invoice(cls, *, invoice_id: str, tenant_id: str, tier_id: str, tokens_credited: int, ledger_entry_id: str) -> bool:
        """Atomically record a processed subscription invoice to prevent double-crediting.

        Uses a conditional put to ensure only the first invocation succeeds.
        Concurrent duplicate deliveries from Stripe will fail the condition
        and return False.

        Returns:
            True if the record was written (first processor), False if it
            already existed (duplicate delivery).
        """
        from botocore.exceptions import ClientError

        processed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        try:
            _event_store.put_item(
                Item={
                    "StripeEventID": invoice_id,
                    "EventType": "invoice.paid",
                    "TenantID": tenant_id,
                    "TierID": tier_id,
                    "TokensCredited": tokens_credited,
                    "LedgerEntryID": ledger_entry_id,
                    "ProcessedAt": processed_at,
                },
                ConditionExpression="attribute_not_exists(StripeEventID)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.warning("Duplicate invoice processing attempted", invoice_id=invoice_id)
                return False
            raise
        logger.info("Recorded processed subscription invoice", invoice_id=invoice_id, tenant_id=tenant_id, tokens_credited=tokens_credited)
        return True
