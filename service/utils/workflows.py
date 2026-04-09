"""Workflow helpers for extraction state machine."""

import json

from botocore.exceptions import ClientError

from config import EXTRACTION_STATE_MACHINE_ARN, S3_BUCKET_NAME, stepfunctions_client
from logger import logger


def start_extraction_state_machine(tenant_id: str, contact_id: str, statement_id: str, pdf_key: str, json_key: str, page_count: int) -> bool:
    """Kick off the Step Functions extraction workflow."""
    if not EXTRACTION_STATE_MACHINE_ARN:
        logger.error("Extraction state machine ARN not configured; skipping execution", tenant_id=tenant_id, statement_id=statement_id)
        return False

    payload = {"tenant_id": tenant_id, "contact_id": contact_id, "statement_id": statement_id, "s3Bucket": S3_BUCKET_NAME, "pdfKey": pdf_key, "jsonKey": json_key, "pageCount": page_count}
    exec_name = f"{tenant_id}-{statement_id}"[:80]

    try:
        stepfunctions_client.start_execution(stateMachineArn=EXTRACTION_STATE_MACHINE_ARN, name=exec_name, input=json.dumps(payload))
        logger.info("Started extraction state machine", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, execution_name=exec_name)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ExecutionAlreadyExists":
            logger.info("Extraction execution already exists", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, execution_name=exec_name)
            return True
        logger.exception("Failed to start extraction state machine", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, execution_name=exec_name, error=str(exc))
        return False
    except Exception as exc:
        logger.exception("Unexpected error starting extraction state machine", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, execution_name=exec_name, error=str(exc))
        return False
