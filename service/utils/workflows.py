"""Workflow helpers for textraction state machine."""

import json

from botocore.exceptions import ClientError

from config import S3_BUCKET_NAME, TEXTRACTION_STATE_MACHINE_ARN, logger, stepfunctions_client


def start_textraction_state_machine(
    tenant_id: str,
    contact_id: str,
    statement_id: str,
    pdf_key: str,
    json_key: str,
) -> bool:
    """Kick off the Step Functions textraction workflow."""
    if not TEXTRACTION_STATE_MACHINE_ARN:
        logger.error(
            "Textraction state machine ARN not configured; skipping execution",
            tenant_id=tenant_id,
            statement_id=statement_id,
        )
        return False

    payload = {
        "tenant_id": tenant_id,
        "contact_id": contact_id,
        "statement_id": statement_id,
        "s3Bucket": S3_BUCKET_NAME,
        "pdfKey": pdf_key,
        "jsonKey": json_key,
    }
    exec_name = f"{tenant_id}-{statement_id}"[:80]

    try:
        stepfunctions_client.start_execution(
            stateMachineArn=TEXTRACTION_STATE_MACHINE_ARN,
            name=exec_name,
            input=json.dumps(payload),
        )
        logger.info(
            "Started textraction state machine",
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            execution_name=exec_name,
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ExecutionAlreadyExists":
            logger.info(
                "Textraction execution already exists",
                tenant_id=tenant_id,
                contact_id=contact_id,
                statement_id=statement_id,
                execution_name=exec_name,
            )
            return True
        logger.exception(
            "Failed to start textraction state machine",
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            execution_name=exec_name,
            error=str(exc),
        )
        return False
    except Exception as exc:
        logger.exception(
            "Unexpected error starting textraction state machine",
            tenant_id=tenant_id,
            contact_id=contact_id,
            statement_id=statement_id,
            execution_name=exec_name,
            error=str(exc),
        )
        return False
