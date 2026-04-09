"""Billing settlement helpers for the async extraction workflow.

The web service reserves tokens before upload processing starts. This module is
responsible for settling those reservations once the Step Functions workflow
knows whether processing succeeded or failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from config import TENANT_BILLING_TABLE_NAME, TENANT_STATEMENTS_TABLE_NAME, TENANT_TOKEN_LEDGER_TABLE_NAME, ddb_client, tenant_statements_table
from logger import logger

ENTRY_TYPE_CONSUME = "CONSUME"
ENTRY_TYPE_RELEASE = "RELEASE"
SOURCE_EXTRACTION_FAILED = "stepfunctions-extraction-failed"
SOURCE_EXTRACTION_FAILURE = "extraction-lambda-failure"
SOURCE_EXTRACTION_SUCCESS = "extraction-lambda-success"
TOKEN_RESERVATION_STATUS_CONSUMED = "consumed"
TOKEN_RESERVATION_STATUS_RELEASED = "released"
TOKEN_RESERVATION_STATUS_RESERVED = "reserved"


class BillingSettlementError(RuntimeError):
    """Raised when a billing settlement write cannot be completed."""


@dataclass(frozen=True)
class StatementReservationMetadata:
    """Minimal reservation metadata needed to settle one statement."""

    statement_id: str
    page_count: int
    reservation_ledger_entry_id: str
    status: str


class BillingSettlementService:
    """Encapsulate consume/release settlement writes for workflow outcomes."""

    _serializer = TypeSerializer()
    _ddb_client = ddb_client
    _tenant_billing_table_name = TENANT_BILLING_TABLE_NAME or ""
    _tenant_statements_table_name = TENANT_STATEMENTS_TABLE_NAME or ""
    _tenant_token_ledger_table_name = TENANT_TOKEN_LEDGER_TABLE_NAME or ""
    _tenant_statements_table = tenant_statements_table

    @staticmethod
    def _utc_now_iso() -> str:
        """Return the current UTC timestamp in a stable ISO-8601 format."""
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    @classmethod
    def _serialize_item(cls, item: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Convert a Python dict into DynamoDB client attribute values."""
        return {key: cls._serializer.serialize(value) for key, value in item.items()}

    @classmethod
    def _serialize_key(cls, **key: str) -> dict[str, dict[str, Any]]:
        """Convert a DynamoDB key into the low-level client shape."""
        return cls._serialize_item(key)

    @classmethod
    def _serialize_expression_values(cls, values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Convert expression attribute values into DynamoDB client format."""
        return {key: cls._serializer.serialize(value) for key, value in values.items()}

    @staticmethod
    def _release_ledger_entry_id(statement_id: str) -> str:
        """Return the deterministic ledger entry id for a release settlement."""
        return f"release#{statement_id}"

    @staticmethod
    def _consume_ledger_entry_id(statement_id: str) -> str:
        """Return the deterministic ledger entry id for a consume settlement."""
        return f"consume#{statement_id}"

    @staticmethod
    def _client_request_token(*parts: str) -> str:
        """Build a stable DynamoDB transaction idempotency token."""
        return uuid5(NAMESPACE_URL, "|".join(parts)).hex

    @classmethod
    def _settlement_ledger_item(
        cls, *, tenant_id: str, statement_id: str, ledger_entry_id: str, entry_type: str, token_delta: int, created_at: str, source: str, settles_ledger_entry_id: str
    ) -> dict[str, Any]:
        """Build a consume/release ledger row linked back to the reservation."""
        return {
            "TenantID": tenant_id,
            "LedgerEntryID": ledger_entry_id,
            "EntryType": entry_type,
            "TokenDelta": token_delta,
            "CreatedAt": created_at,
            "Source": source,
            "RelatedStatementID": statement_id,
            "SettlesLedgerEntryID": settles_ledger_entry_id,
        }

    @classmethod
    def get_statement_reservation_metadata(cls, tenant_id: str, statement_id: str) -> StatementReservationMetadata | None:
        """Load reservation metadata stored on the statement header row."""
        response = cls._tenant_statements_table.get_item(
            Key={"TenantID": tenant_id, "StatementID": statement_id}, ProjectionExpression="StatementID, PdfPageCount, ReservationLedgerEntryID, TokenReservationStatus"
        )
        item = response.get("Item")
        if not item:
            return None

        raw_page_count = item.get("PdfPageCount")
        page_count = int(raw_page_count) if isinstance(raw_page_count, int) else int(str(raw_page_count or "0"))
        reservation_ledger_entry_id = str(item.get("ReservationLedgerEntryID") or "").strip()
        status = str(item.get("TokenReservationStatus") or "").strip().lower()
        if not reservation_ledger_entry_id or not status:
            return None

        return StatementReservationMetadata(statement_id=str(item.get("StatementID") or statement_id), page_count=page_count, reservation_ledger_entry_id=reservation_ledger_entry_id, status=status)

    @classmethod
    def _settle_statement_reservation(cls, *, tenant_id: str, statement_id: str, source: str, entry_type: str, next_status: str, update_balance: bool) -> bool:
        """Apply a consume/release settlement when the statement is still reserved."""
        metadata = cls.get_statement_reservation_metadata(tenant_id, statement_id)
        if not metadata:
            logger.warning("Billing settlement skipped; reservation metadata missing", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type)
            return False
        if metadata.status != TOKEN_RESERVATION_STATUS_RESERVED:
            logger.info("Billing settlement skipped; statement already settled", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, current_status=metadata.status)
            return False

        settled_at = cls._utc_now_iso()
        settlement_ledger_entry_id = cls._release_ledger_entry_id(statement_id) if next_status == TOKEN_RESERVATION_STATUS_RELEASED else cls._consume_ledger_entry_id(statement_id)
        effective_token_delta = metadata.page_count if next_status == TOKEN_RESERVATION_STATUS_RELEASED else 0
        transact_items: list[dict[str, Any]] = []

        if update_balance:
            transact_items.append(
                {
                    "Update": {
                        "TableName": cls._tenant_billing_table_name,
                        "Key": cls._serialize_key(TenantID=tenant_id),
                        "UpdateExpression": (
                            "SET TokenBalance = TokenBalance + :page_count, "
                            "UpdatedAt = :updated_at, "
                            "LastLedgerEntryID = :last_ledger_entry_id, "
                            "LastMutationType = :last_mutation_type, "
                            "LastMutationSource = :last_mutation_source"
                        ),
                        "ConditionExpression": "attribute_exists(TenantID) AND attribute_exists(TokenBalance)",
                        "ExpressionAttributeValues": cls._serialize_expression_values(
                            {
                                ":page_count": metadata.page_count,
                                ":updated_at": settled_at,
                                ":last_ledger_entry_id": settlement_ledger_entry_id,
                                ":last_mutation_type": ENTRY_TYPE_RELEASE,
                                ":last_mutation_source": source,
                            }
                        ),
                    }
                }
            )

        transact_items.append(
            {
                "Put": {
                    "TableName": cls._tenant_token_ledger_table_name,
                    "Item": cls._serialize_item(
                        cls._settlement_ledger_item(
                            tenant_id=tenant_id,
                            statement_id=statement_id,
                            ledger_entry_id=settlement_ledger_entry_id,
                            entry_type=entry_type,
                            token_delta=effective_token_delta,
                            created_at=settled_at,
                            source=source,
                            settles_ledger_entry_id=metadata.reservation_ledger_entry_id,
                        )
                    ),
                    "ConditionExpression": "attribute_not_exists(TenantID) AND attribute_not_exists(LedgerEntryID)",
                }
            }
        )
        transact_items.append(
            {
                "Update": {
                    "TableName": cls._tenant_statements_table_name,
                    "Key": cls._serialize_key(TenantID=tenant_id, StatementID=statement_id),
                    "UpdateExpression": "SET TokenReservationStatus = :next_status",
                    "ConditionExpression": "ReservationLedgerEntryID = :reservation_ledger_entry_id AND TokenReservationStatus = :expected_status",
                    "ExpressionAttributeValues": cls._serialize_expression_values(
                        {":reservation_ledger_entry_id": metadata.reservation_ledger_entry_id, ":expected_status": TOKEN_RESERVATION_STATUS_RESERVED, ":next_status": next_status}
                    ),
                }
            }
        )

        try:
            cls._ddb_client.transact_write_items(TransactItems=transact_items, ClientRequestToken=cls._client_request_token(entry_type, tenant_id, statement_id, metadata.reservation_ledger_entry_id))
            logger.info("Settled statement reservation", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, page_count=metadata.page_count, next_status=next_status)
            return True
        except ClientError as exc:
            logger.exception("Billing settlement transaction failed", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, error=str(exc))
            raise BillingSettlementError(f"Failed to settle statement reservation for {statement_id}.") from exc

    @classmethod
    def release_statement_reservation(cls, tenant_id: str, statement_id: str, *, source: str = SOURCE_EXTRACTION_FAILURE) -> bool:
        """Release a statement reservation and return its pages to the balance."""
        return cls._settle_statement_reservation(
            tenant_id=tenant_id, statement_id=statement_id, source=source, entry_type=ENTRY_TYPE_RELEASE, next_status=TOKEN_RESERVATION_STATUS_RELEASED, update_balance=True
        )

    @classmethod
    def consume_statement_reservation(cls, tenant_id: str, statement_id: str, *, source: str = SOURCE_EXTRACTION_SUCCESS) -> bool:
        """Mark a statement reservation as consumed after successful processing."""
        return cls._settle_statement_reservation(
            tenant_id=tenant_id, statement_id=statement_id, source=source, entry_type=ENTRY_TYPE_CONSUME, next_status=TOKEN_RESERVATION_STATUS_CONSUMED, update_balance=False
        )
