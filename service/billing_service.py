"""Billing mutations for token reservations and settlement.

This module owns every write that changes token availability inside the web
service. The hot-path balance lives in ``TenantBillingTable`` while the
immutable audit trail lives in ``TenantTokenLedgerTable``. Reservation writes
also create the statement header rows so the workflow has stable metadata to
settle later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from sp_common.enums import ProcessingStage, TokenReservationStatus
from werkzeug.datastructures import FileStorage

from config import TENANT_BILLING_TABLE_NAME, TENANT_STATEMENTS_TABLE_NAME, TENANT_TOKEN_LEDGER_TABLE_NAME, ddb_client, tenant_statements_table
from logger import logger
from utils.statement_upload_validation import PreparedStatementUpload

ENTRY_TYPE_CONSUME = "CONSUME"
ENTRY_TYPE_RELEASE = "RELEASE"
ENTRY_TYPE_RESERVE = "RESERVE"
ENTRY_TYPE_ADJUSTMENT = "ADJUSTMENT"
LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT = "manual-script"
LAST_MUTATION_SOURCE_STRIPE_CHECKOUT = "stripe-checkout"
LAST_MUTATION_TYPE_ADJUSTMENT = "ADJUSTMENT"
LAST_MUTATION_SOURCE_UPLOAD_SUBMIT = "upload-submit"
LAST_MUTATION_TYPE_RELEASE = "RELEASE"
LAST_MUTATION_TYPE_RESERVE = "RESERVE"
SOURCE_UPLOAD_START_FAILURE = "service-upload-start-failure"
SOURCE_UPLOAD_SUBMIT = "service-upload-submit"
STATEMENT_RECORD_TYPE = "statement"
WELCOME_GRANT_TOKENS = 5
LAST_MUTATION_SOURCE_WELCOME_GRANT = "welcome-grant"


class BillingServiceError(RuntimeError):
    """Base exception for billing mutations."""


class InsufficientTokensError(BillingServiceError):
    """Raised when the tenant balance cannot cover a reservation."""


@dataclass(frozen=True)
class ReservedStatementUpload:
    """Prepared upload enriched with billing reservation metadata."""

    uploaded_file: FileStorage
    contact_id: str
    contact_name: str
    page_count: int
    statement_id: str
    reservation_ledger_entry_id: str


@dataclass(frozen=True)
class StatementReservationMetadata:
    """Minimal reservation metadata stored on statement header rows."""

    statement_id: str
    page_count: int
    reservation_ledger_entry_id: str
    status: str


@dataclass(frozen=True)
class TokenAdjustmentResult:
    """Result metadata returned after a manual token adjustment."""

    tenant_id: str
    token_delta: int
    ledger_entry_id: str
    updated_at: str


class BillingService:
    """Encapsulate atomic billing writes across snapshot and ledger tables."""

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
    def _reservation_ledger_entry_id(statement_id: str) -> str:
        """Return the deterministic ledger entry id for the initial reservation."""
        return f"reserve#{statement_id}"

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
        seed = "|".join(parts)
        return uuid5(NAMESPACE_URL, seed).hex

    @classmethod
    def _statement_header_item(cls, tenant_id: str, reserved_upload: ReservedStatementUpload, uploaded_at: str) -> dict[str, Any]:
        """Build the initial statement header row stored at reservation time."""
        return {
            "TenantID": tenant_id,
            "StatementID": reserved_upload.statement_id,
            "OriginalStatementFilename": reserved_upload.uploaded_file.filename or "Unnamed PDF",
            "ContactID": reserved_upload.contact_id,
            "ContactName": reserved_upload.contact_name,
            "UploadedAt": uploaded_at,
            "Completed": "false",
            "RecordType": STATEMENT_RECORD_TYPE,
            "PdfPageCount": reserved_upload.page_count,
            "ReservationLedgerEntryID": reserved_upload.reservation_ledger_entry_id,
            # Settlement can be retried by Step Functions; we need one persisted
            # state flag to make consume/release operations conditionally idempotent.
            "TokenReservationStatus": TokenReservationStatus.RESERVED,
            # Set initial processing stage so the UI can track progress
            # from the moment of upload. The Lambda transitions this
            # through chunking → extracting → post_processing → complete.
            "ProcessingStage": ProcessingStage.QUEUED,
        }

    @classmethod
    def _reservation_ledger_item(cls, tenant_id: str, reserved_upload: ReservedStatementUpload, created_at: str) -> dict[str, Any]:
        """Build the immutable reservation audit row for one statement."""
        return {
            "TenantID": tenant_id,
            "LedgerEntryID": reserved_upload.reservation_ledger_entry_id,
            "EntryType": ENTRY_TYPE_RESERVE,
            "TokenDelta": -reserved_upload.page_count,
            "CreatedAt": created_at,
            "Source": SOURCE_UPLOAD_SUBMIT,
            "RelatedStatementID": reserved_upload.statement_id,
        }

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
    def _adjustment_ledger_item(cls, *, tenant_id: str, ledger_entry_id: str, token_delta: int, created_at: str, source: str, price_per_token_pence: float | None = None) -> dict[str, Any]:
        """Build an immutable ledger row for a manual balance adjustment."""
        item: dict[str, Any] = {"TenantID": tenant_id, "LedgerEntryID": ledger_entry_id, "EntryType": ENTRY_TYPE_ADJUSTMENT, "TokenDelta": token_delta, "CreatedAt": created_at, "Source": source}
        if price_per_token_pence is not None:
            # DynamoDB requires Decimal for numeric types (floats are rejected).
            item["PricePerTokenPence"] = Decimal(str(price_per_token_pence))
        return item

    @classmethod
    def _raise_for_transaction_failure(cls, exc: ClientError, *, tenant_id: str, context: str) -> None:
        """Translate DynamoDB transaction failures into service exceptions."""
        error = exc.response.get("Error", {})
        code = error.get("Code")
        cancellation_reasons = exc.response.get("CancellationReasons", [])

        logger.warning("Billing transaction failed", tenant_id=tenant_id, context=context, error_code=code, cancellation_reasons=cancellation_reasons)

        if code == "TransactionCanceledException":
            first_reason = cancellation_reasons[0].get("Code") if cancellation_reasons else None
            if first_reason == "ConditionalCheckFailed":
                raise InsufficientTokensError("The tenant does not have enough available tokens for this upload.") from exc

        raise BillingServiceError("Billing transaction failed.") from exc

    @classmethod
    def reserve_statement_uploads(cls, tenant_id: str, prepared_uploads: list[PreparedStatementUpload]) -> list[ReservedStatementUpload]:
        """Reserve tokens and create statement rows for an upload batch.

        Args:
            tenant_id: Tenant the upload belongs to.
            prepared_uploads: Validated uploads ready for reservation.

        Returns:
            The same uploads enriched with statement ids and reservation ledger ids.

        Raises:
            InsufficientTokensError: The billing snapshot does not cover the batch.
            BillingServiceError: Any other transactional write failure.
        """
        if not tenant_id:
            raise BillingServiceError("TenantID is required for token reservation.")
        if not prepared_uploads:
            return []

        uploaded_at = cls._utc_now_iso()
        reserved_uploads = [
            ReservedStatementUpload(
                uploaded_file=prepared_upload.uploaded_file,
                contact_id=prepared_upload.contact_id,
                contact_name=prepared_upload.contact_name,
                page_count=prepared_upload.page_count,
                statement_id=(statement_id := str(uuid4())),
                reservation_ledger_entry_id=cls._reservation_ledger_entry_id(statement_id),
            )
            for prepared_upload in prepared_uploads
        ]
        total_pages = sum(upload.page_count for upload in reserved_uploads)
        last_reservation_id = reserved_uploads[-1].reservation_ledger_entry_id

        transact_items: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": cls._tenant_billing_table_name,
                    "Key": cls._serialize_key(TenantID=tenant_id),
                    "UpdateExpression": (
                        "SET TokenBalance = TokenBalance - :total_pages, "
                        "UpdatedAt = :updated_at, "
                        "LastLedgerEntryID = :last_ledger_entry_id, "
                        "LastMutationType = :last_mutation_type, "
                        "LastMutationSource = :last_mutation_source"
                    ),
                    "ConditionExpression": "attribute_exists(TenantID) AND attribute_exists(TokenBalance) AND TokenBalance >= :total_pages",
                    "ExpressionAttributeValues": cls._serialize_expression_values(
                        {
                            ":total_pages": total_pages,
                            ":updated_at": uploaded_at,
                            ":last_ledger_entry_id": last_reservation_id,
                            ":last_mutation_type": LAST_MUTATION_TYPE_RESERVE,
                            ":last_mutation_source": LAST_MUTATION_SOURCE_UPLOAD_SUBMIT,
                        }
                    ),
                }
            }
        ]

        for reserved_upload in reserved_uploads:
            transact_items.append(
                {
                    "Put": {
                        "TableName": cls._tenant_token_ledger_table_name,
                        "Item": cls._serialize_item(cls._reservation_ledger_item(tenant_id, reserved_upload, uploaded_at)),
                        "ConditionExpression": "attribute_not_exists(TenantID) AND attribute_not_exists(LedgerEntryID)",
                    }
                }
            )
            transact_items.append(
                {
                    "Put": {
                        "TableName": cls._tenant_statements_table_name,
                        "Item": cls._serialize_item(cls._statement_header_item(tenant_id, reserved_upload, uploaded_at)),
                        "ConditionExpression": "attribute_not_exists(TenantID) AND attribute_not_exists(StatementID)",
                    }
                }
            )

        try:
            cls._ddb_client.transact_write_items(
                TransactItems=transact_items, ClientRequestToken=cls._client_request_token("reserve", tenant_id, *(upload.statement_id for upload in reserved_uploads))
            )
            logger.info("Reserved upload tokens", tenant_id=tenant_id, statements=len(reserved_uploads), total_pages=total_pages, last_ledger_entry_id=last_reservation_id)
        except ClientError as exc:
            cls._raise_for_transaction_failure(exc, tenant_id=tenant_id, context="reserve_statement_uploads")

        return reserved_uploads

    @classmethod
    def reserve_confirmed_statement(cls, tenant_id: str, statement_id: str, page_count: int) -> str:
        """Reserve tokens for a statement confirmed through config suggestion.

        Unlike ``reserve_statement_uploads`` (which creates the statement header),
        this updates an existing header row — the one created at upload time by
        ``_create_review_statement_header`` — with billing reservation fields so
        the Lambda can settle the reservation after processing.

        Args:
            tenant_id: Tenant the statement belongs to.
            statement_id: Statement whose header should be updated.
            page_count: Number of PDF pages to deduct from the token balance.

        Returns:
            The reservation ledger entry id.

        Raises:
            InsufficientTokensError: The tenant balance cannot cover the pages.
            BillingServiceError: Any other transactional write failure.
        """
        if not tenant_id:
            raise BillingServiceError("TenantID is required for token reservation.")
        if page_count <= 0:
            raise BillingServiceError("page_count must be a positive integer.")

        reservation_id = cls._reservation_ledger_entry_id(statement_id)
        reserved_at = cls._utc_now_iso()

        transact_items: list[dict[str, Any]] = [
            # Deduct tokens from the billing snapshot.
            {
                "Update": {
                    "TableName": cls._tenant_billing_table_name,
                    "Key": cls._serialize_key(TenantID=tenant_id),
                    "UpdateExpression": (
                        "SET TokenBalance = TokenBalance - :pages, "
                        "UpdatedAt = :updated_at, "
                        "LastLedgerEntryID = :last_ledger_entry_id, "
                        "LastMutationType = :last_mutation_type, "
                        "LastMutationSource = :last_mutation_source"
                    ),
                    "ConditionExpression": "attribute_exists(TenantID) AND attribute_exists(TokenBalance) AND TokenBalance >= :pages",
                    "ExpressionAttributeValues": cls._serialize_expression_values(
                        {
                            ":pages": page_count,
                            ":updated_at": reserved_at,
                            ":last_ledger_entry_id": reservation_id,
                            ":last_mutation_type": LAST_MUTATION_TYPE_RESERVE,
                            ":last_mutation_source": LAST_MUTATION_SOURCE_UPLOAD_SUBMIT,
                        }
                    ),
                }
            },
            # Immutable reservation audit row in the ledger.
            {
                "Put": {
                    "TableName": cls._tenant_token_ledger_table_name,
                    "Item": cls._serialize_item(
                        {
                            "TenantID": tenant_id,
                            "LedgerEntryID": reservation_id,
                            "EntryType": ENTRY_TYPE_RESERVE,
                            "TokenDelta": -page_count,
                            "CreatedAt": reserved_at,
                            "Source": SOURCE_UPLOAD_SUBMIT,
                            "RelatedStatementID": statement_id,
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(TenantID) AND attribute_not_exists(LedgerEntryID)",
                }
            },
            # Stamp reservation metadata on the existing statement header.
            {
                "Update": {
                    "TableName": cls._tenant_statements_table_name,
                    "Key": cls._serialize_key(TenantID=tenant_id, StatementID=statement_id),
                    "UpdateExpression": "SET ReservationLedgerEntryID = :rid, TokenReservationStatus = :status",
                    "ConditionExpression": "attribute_exists(TenantID) AND attribute_exists(StatementID)",
                    "ExpressionAttributeValues": cls._serialize_expression_values({":rid": reservation_id, ":status": TokenReservationStatus.RESERVED}),
                }
            },
        ]

        try:
            cls._ddb_client.transact_write_items(TransactItems=transact_items, ClientRequestToken=cls._client_request_token("reserve-confirm", tenant_id, statement_id))
            logger.info("Reserved tokens for confirmed statement", tenant_id=tenant_id, statement_id=statement_id, page_count=page_count, reservation_ledger_entry_id=reservation_id)
        except ClientError as exc:
            cls._raise_for_transaction_failure(exc, tenant_id=tenant_id, context="reserve_confirmed_statement")

        return reservation_id

    @classmethod
    def adjust_token_balance(
        cls, tenant_id: str, token_delta: int, *, source: str = LAST_MUTATION_SOURCE_MANUAL_ADJUSTMENT, ledger_entry_id: str | None = None, price_per_token_pence: float | None = None
    ) -> TokenAdjustmentResult:
        """Apply a manual token adjustment atomically to snapshot and ledger.

        Args:
            tenant_id: Tenant whose balance should be adjusted.
            token_delta: Signed token change. Positive grants tokens; negative
                removes them.
            source: Audit source persisted to the billing snapshot and ledger.
            ledger_entry_id: Optional explicit entry ID. When provided (e.g.
                ``purchase#<session_id>`` for Stripe purchases) it is used
                directly instead of generating a random UUID. This enables
                audit cross-reference between StripeEventStoreTable and
                TenantTokenLedgerTable, and makes the ledger Put conditionally
                idempotent via ``attribute_not_exists``.

        Returns:
            Metadata describing the applied adjustment.

        Raises:
            BillingServiceError: Tenant id/token delta are invalid or the
                transaction could not be committed.
            InsufficientTokensError: A negative adjustment would overdraw the
                tenant balance.
        """
        if not tenant_id:
            raise BillingServiceError("TenantID is required for token adjustment.")
        if token_delta == 0:
            raise BillingServiceError("TokenDelta must be non-zero.")

        adjusted_at = cls._utc_now_iso()
        # Use caller-supplied ID when provided (Stripe purchases); otherwise
        # generate a random UUID to guarantee uniqueness for manual adjustments.
        ledger_entry_id = ledger_entry_id if ledger_entry_id is not None else f"adjustment#{uuid4()}"
        expression_values = {
            ":zero": 0,
            ":token_delta": token_delta,
            ":updated_at": adjusted_at,
            ":last_ledger_entry_id": ledger_entry_id,
            ":last_mutation_type": LAST_MUTATION_TYPE_ADJUSTMENT,
            ":last_mutation_source": source,
        }
        update_item: dict[str, Any] = {
            "TableName": cls._tenant_billing_table_name,
            "Key": cls._serialize_key(TenantID=tenant_id),
            "UpdateExpression": (
                "SET TokenBalance = if_not_exists(TokenBalance, :zero) + :token_delta, "
                "UpdatedAt = :updated_at, "
                "LastLedgerEntryID = :last_ledger_entry_id, "
                "LastMutationType = :last_mutation_type, "
                "LastMutationSource = :last_mutation_source"
            ),
            "ExpressionAttributeValues": cls._serialize_expression_values(expression_values),
        }

        if token_delta < 0:
            update_item["ConditionExpression"] = "attribute_exists(TenantID) AND attribute_exists(TokenBalance) AND TokenBalance >= :required_tokens"
            expression_values[":required_tokens"] = abs(token_delta)
            update_item["ExpressionAttributeValues"] = cls._serialize_expression_values(expression_values)

        transact_items: list[dict[str, Any]] = [
            {"Update": update_item},
            {
                "Put": {
                    "TableName": cls._tenant_token_ledger_table_name,
                    "Item": cls._serialize_item(
                        cls._adjustment_ledger_item(
                            tenant_id=tenant_id, ledger_entry_id=ledger_entry_id, token_delta=token_delta, created_at=adjusted_at, source=source, price_per_token_pence=price_per_token_pence
                        )
                    ),
                    "ConditionExpression": "attribute_not_exists(TenantID) AND attribute_not_exists(LedgerEntryID)",
                }
            },
        ]

        try:
            cls._ddb_client.transact_write_items(TransactItems=transact_items, ClientRequestToken=cls._client_request_token("adjustment", tenant_id, ledger_entry_id, str(token_delta)))
            logger.info("Adjusted tenant token balance", tenant_id=tenant_id, token_delta=token_delta, ledger_entry_id=ledger_entry_id, source=source)
        except ClientError as exc:
            cls._raise_for_transaction_failure(exc, tenant_id=tenant_id, context="adjust_token_balance")

        return TokenAdjustmentResult(tenant_id=tenant_id, token_delta=token_delta, ledger_entry_id=ledger_entry_id, updated_at=adjusted_at)

    @classmethod
    def get_statement_reservation_metadata(cls, tenant_id: str, statement_id: str) -> StatementReservationMetadata | None:
        """Load the reservation metadata stored on a statement header row."""
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
        """Settle a reserved statement by consuming or releasing it.

        Returns ``True`` when the settlement was applied and ``False`` when the
        statement is already in a terminal state or no reservation metadata was
        found.
        """
        metadata = cls.get_statement_reservation_metadata(tenant_id, statement_id)
        if not metadata:
            logger.warning("Billing settlement skipped; reservation metadata missing", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type)
            return False
        if metadata.status != TokenReservationStatus.RESERVED:
            logger.info("Billing settlement skipped; statement already settled", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, current_status=metadata.status)
            return False

        settled_at = cls._utc_now_iso()
        settlement_ledger_entry_id = cls._release_ledger_entry_id(statement_id) if next_status == TokenReservationStatus.RELEASED else cls._consume_ledger_entry_id(statement_id)
        effective_token_delta = metadata.page_count if next_status == TokenReservationStatus.RELEASED else 0
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
                                ":last_mutation_type": LAST_MUTATION_TYPE_RELEASE,
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
                        {":reservation_ledger_entry_id": metadata.reservation_ledger_entry_id, ":expected_status": TokenReservationStatus.RESERVED, ":next_status": next_status}
                    ),
                }
            }
        )

        try:
            cls._ddb_client.transact_write_items(TransactItems=transact_items, ClientRequestToken=cls._client_request_token(entry_type, tenant_id, statement_id, metadata.reservation_ledger_entry_id))
            logger.info("Settled statement reservation", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, page_count=metadata.page_count, next_status=next_status)
            return True
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") == "TransactionCanceledException":
                logger.warning(
                    "Billing settlement transaction cancelled", tenant_id=tenant_id, statement_id=statement_id, entry_type=entry_type, cancellation_reasons=exc.response.get("CancellationReasons", [])
                )
            raise BillingServiceError(f"Failed to settle statement reservation for {statement_id}.") from exc

    @classmethod
    def release_statement_reservation(cls, tenant_id: str, statement_id: str, *, source: str = SOURCE_UPLOAD_START_FAILURE) -> bool:
        """Release a statement reservation and return its pages to the balance."""
        return cls._settle_statement_reservation(
            tenant_id=tenant_id, statement_id=statement_id, source=source, entry_type=ENTRY_TYPE_RELEASE, next_status=TokenReservationStatus.RELEASED, update_balance=True
        )

    @classmethod
    def consume_statement_reservation(cls, tenant_id: str, statement_id: str, *, source: str = SOURCE_UPLOAD_SUBMIT) -> bool:
        """Mark a statement reservation as consumed after successful processing."""
        return cls._settle_statement_reservation(
            tenant_id=tenant_id, statement_id=statement_id, source=source, entry_type=ENTRY_TYPE_CONSUME, next_status=TokenReservationStatus.CONSUMED, update_balance=False
        )
