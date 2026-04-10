"""Lambda entry point for statement extraction.

This handler:
- Validates the StepFunctions payload with Pydantic
- Normalizes identifiers and resolves the source bucket
- Delegates to `core.statement_processor.run_extraction`
- Returns a compact response for downstream state handling
"""

from typing import Any

from pydantic import ValidationError
from sp_common.enums import ProcessingStage

from config import S3_BUCKET_NAME
from core.billing import SOURCE_EXTRACTION_FAILURE, SOURCE_EXTRACTION_SUCCESS, BillingSettlementError, BillingSettlementService
from core.models import ExtractionEvent
from core.processing_progress import update_processing_stage
from core.statement_processor import run_extraction
from logger import logger

# region Billing helpers


def _release_reserved_tokens(tenant_id: str, statement_id: str, *, source: str) -> bool:
    """Release reserved tokens after extraction failure.

    Returns True if the reservation was successfully released, False if the
    settlement service raised a BillingSettlementError. The caller decides
    whether to surface a warning message when False is returned.
    """
    try:
        released = BillingSettlementService.release_statement_reservation(tenant_id, statement_id, source=source)
        logger.info("Released reserved tokens after failure", tenant_id=tenant_id, statement_id=statement_id, source=source, released=released)
        return released
    except BillingSettlementError as exc:
        logger.exception("Failed to release reserved tokens", tenant_id=tenant_id, statement_id=statement_id, source=source, error=str(exc))
        return False


def _consume_reserved_tokens(tenant_id: str, statement_id: str) -> bool:
    """Consume reserved tokens after successful processing.

    Returns True if the reservation was successfully consumed, False if the
    settlement service raised a BillingSettlementError. A False return means
    the extraction succeeded but billing is in an inconsistent state — the
    caller should treat this as a hard error.
    """
    try:
        consumed = BillingSettlementService.consume_statement_reservation(tenant_id, statement_id, source=SOURCE_EXTRACTION_SUCCESS)
        logger.info("Consumed reserved tokens after success", tenant_id=tenant_id, statement_id=statement_id, consumed=consumed)
        return consumed
    except BillingSettlementError as exc:
        logger.exception("Failed to consume reserved tokens", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        return False


# endregion


# region Lambda handler


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
    """Validate the incoming event and orchestrate statement extraction.

    Handles three outcomes:
    1. Event validation failure → immediate error response.
    2. Extraction failure → mark stage as FAILED, release tokens, return error.
    3. Extraction success → consume tokens, return summary response.
    """
    logger.info("Extraction lambda invoked", event_keys=list(event.keys()) if isinstance(event, dict) else [])

    try:
        payload = ExtractionEvent.model_validate(event)
    except ValidationError as exc:
        logger.error("Event failed validation", errors=exc.errors())
        return {"status": "error", "message": "Invalid event payload", "errors": exc.errors()}

    statement_id = payload.statement_id
    tenant_id = payload.tenant_id
    contact_id = payload.contact_id
    pdf_key = payload.pdf_key
    json_key = payload.json_key
    pdf_bucket = payload.pdf_bucket or S3_BUCKET_NAME
    page_count = payload.page_count

    logger.debug("Resolved extraction inputs", tenant_id=tenant_id, statement_id=statement_id, contact_id=contact_id, pdf_bucket=pdf_bucket, page_count=page_count)

    try:
        result = run_extraction(bucket=pdf_bucket, pdf_key=pdf_key, json_key=json_key, tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, page_count=page_count)
        logger.info("Extraction complete", tenant_id=tenant_id, statement_id=statement_id, json_key=json_key)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Broad catch is intentional: any failure in run_extraction must trigger
        # stage update and token release before returning an error to Step Functions.
        logger.exception("Extraction lambda failed", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        update_processing_stage(tenant_id, statement_id, ProcessingStage.FAILED)
        released = _release_reserved_tokens(tenant_id, statement_id, source=SOURCE_EXTRACTION_FAILURE)
        message = str(exc)
        if not released:
            # Token release failed — flag for operator review so pages aren't lost.
            message = f"{message} (token release needs operator attention)"
        return {"status": "error", "statementId": statement_id, "jsonKey": json_key, "message": message}

    statement_payload = result.statement
    statement_items = statement_payload.get("statement_items")
    item_count = len(statement_items) if isinstance(statement_items, list) else 0
    earliest_item_date = statement_payload.get("earliest_item_date")
    latest_item_date = statement_payload.get("latest_item_date")

    if not _consume_reserved_tokens(tenant_id, statement_id):
        return {"status": "error", "statementId": statement_id, "jsonKey": json_key, "message": "Statement processed but billing settlement failed."}

    return {
        "status": "ok",
        "statementId": statement_id,
        "tenantId": tenant_id,
        "contactId": contact_id,
        "jsonKey": json_key,
        "filename": result.filename,
        "itemCount": item_count,
        "earliestItemDate": earliest_item_date,
        "latestItemDate": latest_item_date,
    }


# endregion
