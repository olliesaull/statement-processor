"""Storage helpers for statement files and assets."""

import json
import os
import time
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from config import LOCAL_DATA_DIR, S3_BUCKET_NAME, s3_client
from logger import logger

# MIME/extension guards for uploads
ALLOWED_EXTENSIONS = {".pdf", ".PDF"}


PDF_MAGIC = b"%PDF-"

# TTL for cached statement JSON files (seconds).
STATEMENT_CACHE_TTL_SECONDS = 900  # 15 minutes


def is_allowed_pdf(filename: str, mimetype: str, stream: Any = None) -> bool:
    """Check an upload is a genuine PDF by extension, MIME type, and magic bytes.

    Note: We intentionally only accept 'application/pdf' to avoid false positives
    like 'application/octet-stream'. If broader support is desired, revisit this.

    When *stream* is provided, the first bytes are checked for the ``%PDF-``
    magic header to catch spoofed extensions/MIME types. The stream position
    is restored after the check.
    """
    ext_ok = Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
    mime_ok = mimetype == "application/pdf"
    if not (ext_ok and mime_ok):
        return False

    # If a stream is available, verify the file actually starts with %PDF-.
    if stream is not None:
        stream.seek(0)
        header = stream.read(len(PDF_MAGIC))
        stream.seek(0)
        if header != PDF_MAGIC:
            return False

    return True


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


def _statement_cache_path(json_key: str) -> str:
    """Derive the local cache path for a statement JSON S3 key.

    The json_key format is '{tenant_id}/statements/{statement_id}.json'.
    Cache path: '{LOCAL_DATA_DIR}/{tenant_id}/statements/{statement_id}.json'.
    """
    return os.path.join(LOCAL_DATA_DIR, json_key)


def _read_cached_statement(cache_path: str) -> dict[str, Any] | None:
    """Return cached statement JSON if the file exists and is within the TTL.

    Returns None if the file is missing, unreadable, or older than
    STATEMENT_CACHE_TTL_SECONDS.
    """
    try:
        mtime = os.path.getmtime(cache_path)
    except OSError:
        # File does not exist or is inaccessible — treat as a cache miss.
        return None

    age_seconds = time.time() - mtime
    if age_seconds > STATEMENT_CACHE_TTL_SECONDS:
        logger.info("Statement cache expired", cache_path=cache_path, age_seconds=round(age_seconds))
        return None

    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Statement loaded from disk cache", cache_path=cache_path)
        return data
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read cached statement", cache_path=cache_path)
        return None


def _write_statement_cache(cache_path: str, data: dict[str, Any]) -> None:
    """Write statement JSON to the local disk cache.

    Failure is non-fatal — if the write fails, the next request will simply
    fetch from S3 again rather than crashing the response.
    """
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        logger.info("Statement cached to disk", cache_path=cache_path)
    except OSError:
        # Cache write failure is non-fatal — next request will just hit S3 again.
        logger.exception("Failed to write statement cache", cache_path=cache_path)


def fetch_json_statement(tenant_id: str, bucket: str, json_key: str) -> dict[str, Any]:
    """Download and return the JSON statement from S3, with local disk caching.

    On first fetch, the JSON is downloaded from S3 and cached to disk under
    LOCAL_DATA_DIR. Subsequent calls within the TTL (15 minutes) return the
    cached copy without hitting S3.

    The S3 JSON is effectively immutable for a given statement ID — re-uploads
    create new statement IDs — so the TTL is mainly for disk space hygiene
    rather than data freshness.

    Raises:
        StatementJSONNotFoundError: if the object does not exist in S3 (and no
            valid cache exists).
    """
    cache_path = _statement_cache_path(json_key)

    # Try disk cache first to avoid an S3 round-trip on repeated loads
    # (e.g. filter changes, pagination, marking items complete).
    cached = _read_cached_statement(cache_path)
    if cached is not None:
        return cached

    # Cache miss or stale — fetch from S3.
    logger.info("Fetching JSON statement from S3", tenant_id=tenant_id, json_key=json_key)
    try:
        s3_client.head_object(Bucket=bucket, Key=json_key)
    except ClientError as e:
        if e.response["Error"].get("Code") == "404":
            raise StatementJSONNotFoundError(json_key) from e
        raise

    obj = s3_client.get_object(Bucket=bucket, Key=json_key)
    json_bytes = obj["Body"].read()
    data = json.loads(json_bytes.decode("utf-8"))

    # Write to disk cache for subsequent loads within the TTL.
    _write_statement_cache(cache_path, data)

    return data
