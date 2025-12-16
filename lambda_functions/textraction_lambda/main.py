from typing import Any, Dict, Optional

from config import S3_BUCKET_NAME, logger
from core.textract_statement import run_textraction


def _get_str(event: Dict[str, Any], key: str) -> Optional[str]:
    val = event.get(key)
    return str(val) if val is not None else None


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Textraction lambda invoked", event_keys=list(event.keys()) if isinstance(event, dict) else [])

    required = ["jobId", "statementId", "tenantId", "contactId", "pdfKey", "jsonKey"]
    if not isinstance(event, dict) or any(k not in event for k in required):
        missing = [k for k in required if not isinstance(event, dict) or k not in event]
        logger.error("Missing required event fields", missing=missing)
        return {"status": "error", "message": f"Missing required fields: {', '.join(missing)}"}

    job_id = _get_str(event, "jobId") or ""
    statement_id = _get_str(event, "statementId") or ""
    tenant_id = _get_str(event, "tenantId") or ""
    contact_id = _get_str(event, "contactId") or ""
    pdf_key = _get_str(event, "pdfKey") or ""
    json_key = _get_str(event, "jsonKey") or ""
    pdf_bucket = _get_str(event, "pdfBucket") or S3_BUCKET_NAME

    try:
        result = run_textraction(job_id=job_id, bucket=pdf_bucket, pdf_key=pdf_key, json_key=json_key, tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)
        logger.info("Textraction complete", job_id=job_id, tenant_id=tenant_id, statement_id=statement_id, json_key=json_key)
        return {"status": "ok", "jobId": job_id, "jsonKey": json_key, "result": result}
    except Exception as exc:
        logger.exception("Textraction lambda failed", job_id=job_id, tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        return {"status": "error", "message": str(exc)}
