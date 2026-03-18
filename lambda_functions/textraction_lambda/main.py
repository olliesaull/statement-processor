"""
Lambda entry point for statement textraction.

This handler:
- Validates the StepFunctions payload with Pydantic
- Normalizes identifiers and resolves the source bucket
- Delegates to `core.textract_statement.run_textraction`
- Returns a compact response for downstream state handling
"""

from typing import Any

from pydantic import ValidationError

from config import S3_BUCKET_NAME
from core.billing import SOURCE_TEXTRACT_FAILED, SOURCE_TEXTRACTION_FAILURE, SOURCE_TEXTRACTION_SUCCESS, BillingSettlementError, BillingSettlementService
from core.models import TextractionEvent
from core.textract_statement import run_textraction
from logger import logger


def _release_reserved_tokens(tenant_id: str, statement_id: str, *, source: str) -> bool:
    """Release reserved tokens after textract or processing failure."""
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
        consumed = BillingSettlementService.consume_statement_reservation(tenant_id, statement_id, source=SOURCE_TEXTRACTION_SUCCESS)
        logger.info("Consumed reserved tokens after success", tenant_id=tenant_id, statement_id=statement_id, consumed=consumed)
        return consumed
    except BillingSettlementError as exc:
        logger.exception("Failed to consume reserved tokens", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        return False


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:  # pylint: disable=unused-argument
    """Validate the incoming event and orchestrate the textraction run."""
    # Entry point for AWS Lambda: validate input and orchestrate textraction.
    logger.info("Textraction lambda invoked", event_keys=list(event.keys()) if isinstance(event, dict) else [])

    try:
        # Validate and coerce the incoming payload into a typed model
        payload = TextractionEvent.model_validate(event)
    except ValidationError as exc:
        logger.error("Event failed validation", errors=exc.errors())
        return {"status": "error", "message": "Invalid event payload", "errors": exc.errors()}

    # Pull normalized values; default bucket falls back to config when absent
    job_id = payload.job_id
    statement_id = payload.statement_id
    tenant_id = payload.tenant_id
    contact_id = payload.contact_id
    pdf_key = payload.pdf_key
    json_key = payload.json_key
    pdf_bucket = payload.pdf_bucket or S3_BUCKET_NAME
    textract_status = (payload.textract_status or "").strip().upper()

    logger.debug("Resolved textraction inputs", job_id=job_id, tenant_id=tenant_id, statement_id=statement_id, contact_id=contact_id, pdf_bucket=pdf_bucket, textract_status=textract_status)

    if textract_status == "FAILED":
        released = _release_reserved_tokens(tenant_id, statement_id, source=SOURCE_TEXTRACT_FAILED)
        message = "Textract reported FAILED and reserved tokens were released." if released else "Textract reported FAILED and token release needs operator attention."
        return {"status": "error", "jobId": job_id, "statementId": statement_id, "jsonKey": json_key, "message": message}

    try:
        result = run_textraction(job_id=job_id, bucket=pdf_bucket, pdf_key=pdf_key, json_key=json_key, tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)
        logger.info("Textraction complete", job_id=job_id, tenant_id=tenant_id, statement_id=statement_id, json_key=json_key)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("Textraction lambda failed", job_id=job_id, tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        released = _release_reserved_tokens(tenant_id, statement_id, source=SOURCE_TEXTRACTION_FAILURE)
        message = str(exc)
        if not released:
            message = f"{message} (token release needs operator attention)"
        return {"status": "error", "jobId": job_id, "statementId": statement_id, "jsonKey": json_key, "message": message}

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
        return {"status": "error", "jobId": job_id, "statementId": statement_id, "jsonKey": json_key, "message": "Statement processed but billing settlement failed."}

    # Keep Step Functions state payload intentionally small. Full statement output is persisted to S3.
    return {
        "status": "ok",
        "jobId": job_id,
        "statementId": statement_id,
        "tenantId": tenant_id,
        "contactId": contact_id,
        "jsonKey": json_key,
        "filename": result.get("filename") if isinstance(result, dict) else None,
        "itemCount": item_count,
        "earliestItemDate": earliest_item_date,
        "latestItemDate": latest_item_date,
    }
