"""Statement upload helpers for reserving tokens and starting extraction.

Extracted from app.py to keep the route file focused on request handling.
These functions handle the multi-step upload pipeline: validating files,
reserving billing tokens, uploading PDFs to S3, and kicking off the
Step Functions extraction workflow.
"""

from typing import Any

from flask import request

from billing_service import BillingService, BillingServiceError, InsufficientTokensError, ReservedStatementUpload
from logger import logger
from utils.dynamo import delete_statement_data
from utils.statement_upload_validation import PreparedStatementUpload, prepare_statement_uploads, validate_upload_payload
from utils.storage import statement_json_s3_key, statement_pdf_s3_key, upload_statement_to_s3
from utils.workflows import start_extraction_state_machine
from xero_repository import get_contacts


class StatementUploadStartError(RuntimeError):
    """Raised when a reserved statement cannot be handed off to processing."""


def get_active_contacts_for_upload() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return active contacts and a name-to-ID lookup for the upload form.

    Fetches all contacts from Xero, filters to active ones, sorts
    alphabetically, and builds a name->ID dict for quick lookup during
    upload validation.

    Returns:
        Tuple of (sorted active contacts list, contact name->ID dict).
    """
    contacts_raw = get_contacts()
    contacts_active = [c for c in contacts_raw if str(c.get("contact_status") or "").upper() == "ACTIVE"]
    contacts_list = sorted(contacts_active, key=lambda c: (c.get("name") or "").casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    return contacts_list, contact_lookup


def process_statement_upload(tenant_id: str | None, reserved_upload: ReservedStatementUpload) -> str:
    """Upload a reserved statement PDF and kick off extraction.

    Uploads the PDF to S3 first so downstream processing can read it,
    then starts the Step Functions extraction workflow.

    Args:
        tenant_id: Active Xero tenant.
        reserved_upload: Upload row that already has a statement id and token reservation.

    Returns:
        The statement id linked to the upload.

    Raises:
        StatementUploadStartError: S3 upload or workflow startup failed after reservation.
    """
    file_bytes = getattr(reserved_upload.uploaded_file, "content_length", None)
    statement_id = reserved_upload.statement_id
    logger.info(
        "Preparing statement upload",
        tenant_id=tenant_id,
        contact_id=reserved_upload.contact_id,
        contact_name=reserved_upload.contact_name,
        statement_id=statement_id,
        statement_filename=reserved_upload.uploaded_file.filename,
        bytes=file_bytes,
    )

    # Upload PDF to S3 first so downstream processing can read it.
    pdf_statement_key = statement_pdf_s3_key(tenant_id, statement_id)
    try:
        upload_statement_to_s3(fs_like=reserved_upload.uploaded_file, key=pdf_statement_key)
        logger.info("Uploaded statement PDF", tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, s3_key=pdf_statement_key)
    except Exception as exc:
        logger.exception("Failed to upload reserved statement PDF", tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, s3_key=pdf_statement_key, error=exc)
        raise StatementUploadStartError("The statement PDF could not be uploaded.") from exc

    # Kick off background extraction so it's ready by the time the user views it.
    json_statement_key = statement_json_s3_key(tenant_id, statement_id)
    started = start_extraction_state_machine(
        tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, pdf_key=pdf_statement_key, json_key=json_statement_key, page_count=reserved_upload.page_count
    )

    log_kwargs = {"tenant_id": tenant_id, "contact_id": reserved_upload.contact_id, "statement_id": statement_id, "pdf_key": pdf_statement_key, "json_key": json_statement_key}

    if started:
        logger.info("Started extraction workflow", **log_kwargs)
    else:
        logger.error("Failed to start extraction workflow", **log_kwargs)
        raise StatementUploadStartError("The statement workflow could not be started.")

    return statement_id


def handle_reserved_upload_failure(tenant_id: str | None, reserved_upload: ReservedStatementUpload, exc: Exception, error_messages: list[str]) -> None:
    """Release tokens and clean up statement data after upload-start failure.

    Attempts to return the reserved tokens to the tenant's balance.
    If token release itself fails, the error message indicates operator
    attention is needed.

    Args:
        tenant_id: Active Xero tenant.
        reserved_upload: The upload that failed to start.
        exc: The exception that caused the failure.
        error_messages: Mutable list to append user-facing errors to.
    """
    logger.exception("Upload failed after token reservation; releasing tokens", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, contact_id=reserved_upload.contact_id, error=exc)

    release_succeeded = False
    try:
        release_succeeded = BillingService.release_statement_reservation(tenant_id, reserved_upload.statement_id)
    except BillingServiceError as release_exc:
        logger.exception("Failed to release reserved tokens after upload-start failure", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, error=release_exc)

    filename = reserved_upload.uploaded_file.filename or "Unnamed PDF"
    if not release_succeeded:
        error_messages.append(f"{filename}: The upload was not started and token recovery needs operator attention.")
        return

    try:
        delete_statement_data(tenant_id, reserved_upload.statement_id)
    except Exception as cleanup_exc:
        logger.exception("Failed to clean up statement after upload-start failure", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, error=cleanup_exc)

    error_messages.append(f"{filename}: The upload was not started. Any reserved tokens were returned.")


def reserve_statement_uploads(tenant_id: str | None, prepared_uploads: list[PreparedStatementUpload], error_messages: list[str]) -> list[ReservedStatementUpload]:
    """Reserve tokens for a validated batch and collect user-facing errors.

    Delegates to the billing service. On insufficient balance or other
    billing errors, appends a user-facing message and returns an empty list.

    Args:
        tenant_id: Active Xero tenant.
        prepared_uploads: Validated upload payloads ready for reservation.
        error_messages: Mutable list to append user-facing errors to.

    Returns:
        List of reserved uploads, or empty list on failure.
    """
    try:
        return BillingService.reserve_statement_uploads(tenant_id, prepared_uploads)
    except InsufficientTokensError:
        logger.info("Upload blocked; token reservation failed due to insufficient balance", tenant_id=tenant_id, files=len(prepared_uploads))
        error_messages.append("The tenant no longer has enough available pages for this upload. Refresh the page, remove some PDFs, or buy more pages before trying again.")
    except BillingServiceError as exc:
        logger.exception("Upload blocked; token reservation failed", tenant_id=tenant_id, files=len(prepared_uploads), error=exc)
        error_messages.append("Could not reserve pages for this upload. Please try again.")
    return []


def handle_upload_statements_post(tenant_id: str | None, *, contact_lookup: dict[str, str], error_messages: list[str]) -> int:
    """Validate, reserve, and start workflow processing for one upload POST.

    All uploads go straight to token reservation and Step Functions -- there
    is no longer a config-review gate because Bedrock extraction derives
    header mappings directly from the PDF content.

    Args:
        tenant_id: Active Xero tenant.
        contact_lookup: Contact name to ID mapping.
        error_messages: Mutable list to append user-facing errors to.

    Returns:
        Number of uploads that started processing successfully.
    """
    files = [f for f in request.files.getlist("statements") if f and f.filename]
    names = request.form.getlist("contact_names")
    logger.info("Upload statements submitted", tenant_id=tenant_id, files=len(files), names=len(names))

    if not validate_upload_payload(files, names):
        return 0

    prepared_uploads = prepare_statement_uploads(tenant_id, files, names, contact_lookup, error_messages)
    if not prepared_uploads:
        return 0

    # Reserve tokens and start the extraction workflow for every valid upload.
    reserved_uploads = reserve_statement_uploads(tenant_id, prepared_uploads, error_messages)
    uploads_ok = 0
    for reserved_upload in reserved_uploads:
        try:
            process_statement_upload(tenant_id=tenant_id, reserved_upload=reserved_upload)
            uploads_ok += 1
        except StatementUploadStartError as exc:
            handle_reserved_upload_failure(tenant_id, reserved_upload, exc, error_messages)

    return uploads_ok
