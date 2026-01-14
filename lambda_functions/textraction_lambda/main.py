"""
Lambda entry point for statement textraction.

This handler:
- Validates the StepFunctions payload with Pydantic
- Normalizes identifiers and resolves the source bucket
- Delegates to `core.textract_statement.run_textraction`
- Returns a structured response for downstream state handling
"""

from typing import Any, Dict

from pydantic import ValidationError

from config import S3_BUCKET_NAME, logger
from core.models import TextractionEvent
from core.textract_statement import run_textraction


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:  # pylint: disable=unused-argument
    """Validate the incoming event and orchestrate the textraction run."""
    # Entry point for AWS Lambda: validate input and orchestrate textraction.
    logger.info(
        "Textraction lambda invoked",
        event_keys=list(event.keys()) if isinstance(event, dict) else [],
    )

    try:
        # Validate and coerce the incoming payload into a typed model
        payload = TextractionEvent.model_validate(event)
    except ValidationError as exc:
        logger.error("Event failed validation", errors=exc.errors())
        return {
            "status": "error",
            "message": "Invalid event payload",
            "errors": exc.errors(),
        }

    # Pull normalized values; default bucket falls back to config when absent
    job_id = payload.job_id
    statement_id = payload.statement_id
    tenant_id = payload.tenant_id
    contact_id = payload.contact_id
    pdf_key = payload.pdf_key
    json_key = payload.json_key
    pdf_bucket = payload.pdf_bucket or S3_BUCKET_NAME

    logger.debug(
        "Resolved textraction inputs",
        job_id=job_id,
        tenant_id=tenant_id,
        statement_id=statement_id,
        contact_id=contact_id,
        pdf_bucket=pdf_bucket,
    )

    try:
        result = run_textraction(
            job_id=job_id,
            bucket=pdf_bucket,
            pdf_key=pdf_key,
            json_key=json_key,
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
        )
        logger.info(
            "Textraction complete",
            job_id=job_id,
            tenant_id=tenant_id,
            statement_id=statement_id,
            json_key=json_key,
        )
        # Return a structured success payload so the state machine can persist the JSON key/job tracking (for logging + associating textraction with this execution).
        return {"status": "ok", "jobId": job_id, "jsonKey": json_key, "result": result}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception(
            "Textraction lambda failed",
            job_id=job_id,
            tenant_id=tenant_id,
            statement_id=statement_id,
            error=str(exc),
        )
        return {
            "status": "error",
            "message": str(exc),
        }  # Mark StepFunction execution as failed.
