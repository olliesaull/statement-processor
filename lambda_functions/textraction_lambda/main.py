import json
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Skeleton handler for textraction processing.

    Expects event fields such as jobId, tenantId, contactId, statementId, pdfBucket, pdfKey, jsonKey.
    """
    logger.info("Textraction lambda invoked")

    return {
        "status": "ok",
        "message": "Textraction lambda skeleton",
        "input": json.dumps(event),
    }
