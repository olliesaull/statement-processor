"""Storage helpers for statement files and assets."""

import json
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from config import S3_BUCKET_NAME, s3_client
from logger import logger

# MIME/extension guards for uploads
ALLOWED_EXTENSIONS = {".pdf", ".PDF"}


def is_allowed_pdf(filename: str, mimetype: str) -> bool:
    """Basic check for PDF uploads by extension and MIME type.

    Note: We intentionally only accept 'application/pdf' to avoid false positives
    like 'application/octet-stream'. If broader support is desired, revisit this.
    """
    ext_ok = Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
    mime_ok = mimetype == "application/pdf"
    return ext_ok and mime_ok


def _clean_key_segment(value: str | None, label: str) -> str:
    """Validate and normalize an S3 key segment."""
    segment = (value or "").strip()
    if not segment:
        raise ValueError(f"{label} is required for S3 key construction")
    if any(sep in segment for sep in ("/", "\\")):
        raise ValueError(f"{label} cannot contain path separators")
    return segment


def _statement_s3_key(tenant_id: str, statement_id: str, extension: str) -> str:
    """Build the S3 key for a statement asset."""
    tenant_segment = _clean_key_segment(tenant_id, "tenant_id")
    statement_segment = _clean_key_segment(statement_id, "statement_id")
    return f"{tenant_segment}/statements/{statement_segment}{extension}"


def statement_pdf_s3_key(tenant_id: str, statement_id: str) -> str:
    """Return the S3 key for a tenant's statement PDF."""
    return _statement_s3_key(tenant_id, statement_id, ".pdf")


def statement_json_s3_key(tenant_id: str, statement_id: str) -> str:
    """Return the S3 key for a tenant's statement JSON payload."""
    return _statement_s3_key(tenant_id, statement_id, ".json")


def upload_statement_to_s3(fs_like: Any, key: str) -> bool:
    """Upload a file-like object (PDF or JSON stream) to S3.

    Unclear: This uses the global S3 bucket configured for the app rather than
    accepting a bucket parameter. Calls elsewhere pass the same bucket, so we
    retain this behavior to avoid breaking changes.
    """
    stream = getattr(fs_like, "stream", fs_like)

    # Always reset to start
    stream.seek(0)

    try:
        s3_client.upload_fileobj(Fileobj=stream, Bucket=S3_BUCKET_NAME, Key=key)
        logger.info("Uploaded statement asset to S3", key=key)
        return True
    except (BotoCoreError, ClientError) as e:
        logger.exception("Failed to upload to S3", key=key, error=e)
        return False


class StatementJSONNotFoundError(Exception):
    """Raised when the structured JSON for a statement is not yet available."""


def fetch_json_statement(tenant_id: str, bucket: str, json_key: str) -> dict[str, Any]:
    """Download and return the JSON statement from S3.

    Raises:
        StatementJSONNotFoundError: if the object does not exist yet.
    """
    logger.info("Fetching JSON statement", tenant_id=tenant_id, json_key=json_key)
    try:
        s3_client.head_object(Bucket=bucket, Key=json_key)
    except ClientError as e:
        if e.response["Error"].get("Code") == "404":
            raise StatementJSONNotFoundError(json_key) from e
        raise

    obj = s3_client.get_object(Bucket=bucket, Key=json_key)
    json_bytes = obj["Body"].read()
    return json.loads(json_bytes.decode("utf-8"))
