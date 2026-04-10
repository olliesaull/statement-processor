"""Lambda entry point for statement extraction.

This handler:
- Validates the StepFunctions payload with Pydantic
- Normalizes identifiers and resolves the source bucket
- Delegates to `core.statement_processor.run_extraction`
- Returns a compact response for downstream state handling
"""

from typing import Any

from pydantic import ValidationError
from src.enums import ProcessingStage

from config import S3_BUCKET_NAME
from core.billing import SOURCE_EXTRACTION_FAILURE, SOURCE_EXTRACTION_SUCCESS, BillingSettlementError, BillingSettlementService
from core.models import ExtractionEvent
from core.processing_progress import update_processing_stage
from core.statement_processor import run_extraction
from logger import logger


def _release_reserved_tokens(tenant_id: str, statement_id: str, *, source: str) -> bool:
    """Release reserved tokens after extraction failure."""
    try:
        released = BillingSettlementService.release_statement_reservation(tenant_id, statement_id, source=source)
        logger.info("Released reserved tokens after failure", tenant_id=tenant_id, statement_id=statement_id, source=source, released=released)
        return released
    except BillingSettlementError as exc:
        logger.exception("Failed to release reserved tokens", tenant_id=tenant_id, statement_id=statement_id, source=source, error=str(exc))
        return False


def _consume_reserved_tokens(tenant_id: str, statement_id: str) -> bool:
    """Consume reserved tokens after successful processing."""
    try:
        consumed = BillingSettlementService.consume_statement_reservation(tenant_id, statement_id, source=SOURCE_EXTRACTION_SUCCESS)
        logger.info("Consumed reserved tokens after success", tenant_id=tenant_id, statement_id=statement_id, consumed=consumed)
        return consumed
    except BillingSettlementError as exc:
        logger.exception("Failed to consume reserved tokens", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
    """Validate the incoming event and orchestrate statement extraction."""
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
        logger.exception("Extraction lambda failed", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        update_processing_stage(tenant_id, statement_id, ProcessingStage.FAILED)
        released = _release_reserved_tokens(tenant_id, statement_id, source=SOURCE_EXTRACTION_FAILURE)
        message = str(exc)
        if not released:
            message = f"{message} (token release needs operator attention)"
        return {"status": "error", "statementId": statement_id, "jsonKey": json_key, "message": message}

    statement_payload = result.get("statement") if isinstance(result, dict) else None
    if isinstance(statement_payload, dict):
        statement_items = statement_payload.get("statement_items")
        item_count = len(statement_items) if isinstance(statement_items, list) else 0
        earliest_item_date = statement_payload.get("earliest_item_date")
        latest_item_date = statement_payload.get("latest_item_date")
    else:
        item_count = 0
        earliest_item_date = None
        latest_item_date = None

    if not _consume_reserved_tokens(tenant_id, statement_id):
        return {"status": "error", "statementId": statement_id, "jsonKey": json_key, "message": "Statement processed but billing settlement failed."}

    return {
        "status": "ok",
        "statementId": statement_id,
        "tenantId": tenant_id,
        "contactId": contact_id,
        "jsonKey": json_key,
        "filename": result.get("filename") if isinstance(result, dict) else None,
        "itemCount": item_count,
        "earliestItemDate": earliest_item_date,
        "latestItemDate": latest_item_date,
    }
