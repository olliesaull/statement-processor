import io
import json
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
import secrets
from typing import Any, Callable, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    abort,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import HTTPException
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore

import cache_provider
from config import (
    CLIENT_ID,
    CLIENT_SECRET,
    S3_BUCKET_NAME,
    logger,
    s3_client,
    tenant_statements_table,
)
from core.date_utils import coerce_datetime_with_template, format_iso_with
from core.models_comparison import CellComparison
from core.textract_statement import run_textraction
from core.transform import equal
from tenant_data_repository import TenantDataRepository, TenantStatus

# MIME/extension guards for uploads
ALLOWED_EXTENSIONS = {".pdf", ".PDF"}
SCOPES = [
    "offline_access", "openid", "profile", "email", "accounting.transactions", "accounting.reports.read", "accounting.journals.read",
    "accounting.settings", "accounting.contacts", "accounting.attachments", "assets", "projects", "files.read",
]
CSRF_SESSION_KEY = "_csrf_token"
SAFE_CSRF_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_csrf_token() -> str:
    """Return the per-session CSRF token, generating one if missing."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _is_valid_csrf(token: Optional[str]) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        expected = get_csrf_token()
    if not token:
        return False
    try:
        return secrets.compare_digest(token, expected)
    except Exception:
        return False


def enforce_csrf_protection() -> None:
    """Abort unsafe requests if the CSRF token is missing or invalid."""
    if request.method in SAFE_CSRF_METHODS:
        get_csrf_token()
        return

    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or request.args.get("csrf_token")
    if not _is_valid_csrf(token):
        logger.warning("CSRF validation failed", route=request.path, method=request.method)
        abort(400, description="Invalid CSRF token")

def get_xero_oauth2_token() -> Optional[dict]:
    """Return the token dict the SDK expects, or None if not set."""
    return session.get("xero_oauth2_token")

def save_xero_oauth2_token(token: dict) -> None:
    """Persist the whole token dict in the session (or your DB)."""
    session["xero_oauth2_token"] = token


def get_xero_api_client(oauth_token: Optional[dict] = None) -> AccountingApi:
    """Create a thread-safe AccountingApi client, optionally seeded with a specific token."""
    if oauth_token is None:
        token_getter = get_xero_oauth2_token
        token_saver = save_xero_oauth2_token
    else:
        def token_getter() -> Optional[dict]:
            return oauth_token

        def token_saver(new_token: dict) -> None:
            oauth_token.update(new_token)

    api_client = ApiClient(
        Configuration(
            oauth2_token=OAuth2Token(client_id=CLIENT_ID, client_secret=CLIENT_SECRET),
        ),
        pool_threads=1,
        oauth2_token_getter=token_getter,
        oauth2_token_saver=token_saver,
    )

    if oauth_token:
        api_client.set_oauth2_token(oauth_token)

    return AccountingApi(api_client)


def _query_statements_by_completed(tenant_id: Optional[str], completed_value: str) -> List[Dict[str, Any]]:
    """Query statements for a tenant filtered by the Completed flag via GSI."""
    if not tenant_id:
        logger.info("Skipping statement query; tenant missing", completed=completed_value)
        return []

    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "IndexName": "TenantIDCompletedIndex",
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("Completed").eq(completed_value),
        "FilterExpression": Attr("RecordType").not_exists() | Attr("RecordType").eq("statement"),
    }
    logger.info("Querying statements by completion", tenant_id=tenant_id, completed=completed_value)

    while True:
        resp = tenant_statements_table.query(**kwargs)
        batch = resp.get("Items", [])
        items.extend(batch)
        lek = resp.get("LastEvaluatedKey")
        logger.debug("Fetched statement batch", tenant_id=tenant_id, completed=completed_value, batch=len(batch), has_more=bool(lek))
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    logger.info("Collected statements by completion", tenant_id=tenant_id, completed=completed_value, count=len(items))
    return items


def get_statement_record(tenant_id: str, statement_id: str) -> Optional[Dict[str, Any]]:
    """Return the full DynamoDB record for a tenant/statement pair."""
    logger.info("Fetching statement record", tenant_id=tenant_id, statement_id=statement_id)
    response = tenant_statements_table.get_item(
        Key={
            "TenantID": tenant_id,
            "StatementID": statement_id,
        }
    )
    item = response.get("Item")
    logger.debug("Statement record fetched", tenant_id=tenant_id, statement_id=statement_id, found=bool(item))
    return item

def scope_str() -> str:
    """Return Xero OAuth scopes as a space-separated string."""
    return " ".join(SCOPES)


class RedirectToLogin(HTTPException):
    """HTTP exception that produces a redirect to the login route."""
    code = 302

    def __init__(self) -> None:
        super().__init__(description="Redirecting to login")

    def get_response(self, environ=None):  # type: ignore[override]
        return redirect(url_for("login"))


def raise_for_unauthorized(error: Exception) -> None:
    """Redirect the user to login if the Xero API returned 401/403."""
    potential_statuses = []
    for attr in ("status", "status_code", "code"):
        potential_statuses.append(getattr(error, attr, None))

    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status", "status_code", "code"):
            potential_statuses.append(getattr(response, attr, None))

    for status in potential_statuses:
        try:
            status_code = int(status)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue

        if status_code in {401, 403}:
            logger.info("Xero API returned unauthorized/forbidden; redirecting to login", status_code=status_code)
            raise RedirectToLogin()


def get_cached_tenant_status(tenant_id: str) -> Optional[TenantStatus]:
    """Retrieve tenant status from cache, falling back to DynamoDB if missing."""
    if not tenant_id:
        return None

    cached_value = cache_provider.cache.get(f"{tenant_id}_status") if cache_provider.cache else None
    if cached_value:
        try:
            return TenantStatus(cached_value)
        except ValueError:
            return None

    record = TenantDataRepository.get_item(tenant_id)
    if not record:
        return None

    status = record.get("TenantStatus")
    if isinstance(status, TenantStatus):
        cache_provider.set_tenant_status_cache(tenant_id, status)
        return status

    if isinstance(status, str):
        try:
            status_enum = TenantStatus(status)
        except ValueError:
            logger.warning("Encountered unexpected tenant status value", tenant_id=tenant_id, status=status)
            return None

        cache_provider.set_tenant_status_cache(tenant_id, status_enum)
        return status_enum

    logger.warning("Tenant record missing status", tenant_id=tenant_id)
    return None


def xero_token_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Ensure a valid (non-expired) Xero token and active tenant before route access."""
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any):
        tenant_id = session.get("xero_tenant_id")
        token = get_xero_oauth2_token()
        if not tenant_id or not token:
            logger.info("Missing Xero token or tenant; redirecting", route=request.path, tenant_id=tenant_id)
            return redirect(url_for("login"))

        try:
            expires_at = float(token.get("expires_at", 0))
        except (TypeError, ValueError):
            expires_at = 0.0

        if expires_at and time.time() > expires_at:
            logger.info("Xero token expired; redirecting", route=request.path, tenant_id=tenant_id)
            return redirect(url_for("login"))

        return f(*args, **kwargs)

    return decorated_function


def active_tenant_required(message: str = "Please select a tenant before continuing.", redirect_endpoint: str = "tenant_management", flash_key: str = "tenant_error") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Ensure the user has an active tenant selected; otherwise redirect with a message."""
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(f)
        def wrapped(*args: Any, **kwargs: Any):
            tenant_id = session.get("xero_tenant_id")
            if tenant_id:
                return f(*args, **kwargs)
            session[flash_key] = message
            return redirect(url_for(redirect_endpoint))

        return wrapped

    return decorator


def block_when_loading(f: Callable[..., Any]) -> Callable[..., Any]:
    """
    Redirect users away from routes while their active tenant is still loading.
    Uses the in-process cache first and falls back to DynamoDB for safety.
    """
    @wraps(f)
    def decorated_function(*args: Any, **kwargs: Any):
        tenant_id = session.get("xero_tenant_id")
        if tenant_id:
            status = get_cached_tenant_status(tenant_id)
            if status == TenantStatus.LOADING:
                logger.info("Blocking route during load", route=request.path, tenant_id=tenant_id)
                session["tenant_error"] = "Please wait for the initial load to finish before navigating away."
                return redirect(url_for("tenant_management"))

        return f(*args, **kwargs)

    return decorated_function

def route_handler_logging(function):
    @wraps(function)
    def decorator(*args, **kwargs):
        tenant_id = session.get("xero_tenant_id")
        logger.info("Entering route", route=request.path, event_type="USER_TRAIL", path=request.path, tenant_id=tenant_id)

        return function(*args, **kwargs)

    return decorator

def is_allowed_pdf(filename: str, mimetype: str) -> bool:
    """Basic check for PDF uploads by extension and MIME type.

    Note: We intentionally only accept 'application/pdf' to avoid false positives
    like 'application/octet-stream'. If broader support is desired, revisit this.
    """
    ext_ok = Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
    mime_ok = mimetype == "application/pdf"
    return ext_ok and mime_ok


def fmt_date(d: Any) -> Optional[str]:
    """Format datetime/date to ISO date string, else None."""
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    return None


def fmt_invoice_data(inv):
    contact = getattr(inv, "contact", None)

    return {
        "invoice_id": getattr(inv, "invoice_id", None),
        "number": getattr(inv, "invoice_number", None),
        "type": getattr(inv, "type", None),
        "status": getattr(inv, "status", None),
        "date": fmt_date(getattr(inv, "date", None)),
        "due_date": fmt_date(getattr(inv, "due_date", None)),
        "reference": getattr(inv, "reference", None),
        "total": getattr(inv, "total", None),
        "contact_id": getattr(contact, "contact_id", None),
        "contact_name": getattr(contact, "name", None),
    }


def get_contact_for_statement(tenant_id: str, statement_id: str) -> Optional[str]:
    """Get the contact ID for a given statement ID."""
    record = get_statement_record(tenant_id, statement_id)
    if record:
        return record.get("ContactID")
    return None


def get_incomplete_statements() -> List[Dict[str, Any]]:
    """Return statements for the active tenant that are not completed."""
    tenant_id = session.get("xero_tenant_id")
    logger.info("Fetching incomplete statements", tenant_id=tenant_id)
    return _query_statements_by_completed(tenant_id, "false")


def get_completed_statements() -> List[Dict[str, Any]]:
    """Return statements for the active tenant that are marked completed."""
    tenant_id = session.get("xero_tenant_id")
    logger.info("Fetching completed statements", tenant_id=tenant_id)
    return _query_statements_by_completed(tenant_id, "true")


def mark_statement_completed(tenant_id: str, statement_id: str, completed: bool) -> None:
    """Persist a completion flag on the statement record in DynamoDB."""
    tenant_statements_table.update_item(
        Key={
            "TenantID": tenant_id,
            "StatementID": statement_id,
        },
        UpdateExpression="SET #completed = :completed",
        ExpressionAttributeNames={"#completed": "Completed"},
        ExpressionAttributeValues={":completed": "true" if completed else "false"},
        ConditionExpression=Attr("StatementID").exists(),
    )


def get_statement_item_status_map(tenant_id: str, statement_id: str) -> Dict[str, bool]:
    """Return completion status for each statement item keyed by statement_item_id."""
    if not tenant_id or not statement_id:
        return {}

    logger.info("Fetching statement item statuses", tenant_id=tenant_id, statement_id=statement_id)
    statuses: Dict[str, bool] = {}
    prefix = f"{statement_id}#item-"
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(prefix),
        "ProjectionExpression": "#sid, #completed",
        "ExpressionAttributeNames": {"#sid": "StatementID", "#completed": "Completed"},
    }

    while True:
        resp = tenant_statements_table.query(**kwargs)
        for item in resp.get("Items", []):
            statement_item_id = item.get("StatementID")
            if not statement_item_id:
                continue
            completed_val = str(item.get("Completed", "false")).strip().lower()
            statuses[statement_item_id] = completed_val == "true"

        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    logger.info("Fetched statement item statuses", tenant_id=tenant_id, statement_id=statement_id, count=len(statuses))
    return statuses


def set_statement_item_completed(tenant_id: str, statement_item_id: str, completed: bool) -> None:
    """Toggle completion flag for a single statement item."""
    if not tenant_id or not statement_item_id:
        return

    tenant_statements_table.update_item(
        Key={
            "TenantID": tenant_id,
            "StatementID": statement_item_id,
        },
        UpdateExpression="SET #completed = :completed",
        ExpressionAttributeNames={"#completed": "Completed"},
        ExpressionAttributeValues={":completed": "true" if completed else "false"},
    )


def set_all_statement_items_completed(tenant_id: str, statement_id: str, completed: bool) -> None:
    """Set completion flag for all statement items tied to a statement."""
    statuses = get_statement_item_status_map(tenant_id, statement_id)
    if not statuses:
        return

    for statement_item_id in statuses.keys():
        set_statement_item_completed(tenant_id, statement_item_id, completed)


def delete_statement_data(tenant_id: str, statement_id: str) -> None:
    """Delete statement header, items, and associated S3 artifacts."""
    if not tenant_id or not statement_id:
        return

    logger.info("Deleting statement data", tenant_id=tenant_id, statement_id=statement_id)

    # Delete statement header and statement items linked to this statement
    item_prefix = f"{statement_id}"
    query_kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id) & Key("StatementID").begins_with(item_prefix),
        "ProjectionExpression": "#sid",
        "ExpressionAttributeNames": {"#sid": "StatementID"},
    }

    deleted_items = 0
    while True:
        resp = tenant_statements_table.query(**query_kwargs)
        items = resp.get("Items", []) or []
        if not items:
            break
        with tenant_statements_table.batch_writer() as batch:
            for item in items:
                sort_key = item.get("StatementID")
                if not sort_key:
                    continue
                batch.delete_item(Key={"TenantID": tenant_id, "StatementID": sort_key})
                deleted_items += 1
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        query_kwargs["ExclusiveStartKey"] = lek

    # Remove S3 artifacts
    s3_keys = [statement_pdf_s3_key(tenant_id, statement_id), statement_json_s3_key(tenant_id, statement_id)]
    for key in s3_keys:
        try:
            s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=key)
            logger.info("Deleted statement S3 object", tenant_id=tenant_id, statement_id=statement_id, s3_key=key)
        except s3_client.exceptions.NoSuchKey:
            logger.info("Statement S3 object already missing", tenant_id=tenant_id, statement_id=statement_id, s3_key=key)
        except Exception as exc:
            logger.exception("Failed to delete statement S3 object", tenant_id=tenant_id, statement_id=statement_id, s3_key=key, error=exc)
            raise

    logger.info("Statement deletion complete", tenant_id=tenant_id, statement_id=statement_id, items_deleted=deleted_items, s3_objects=len(s3_keys))


def add_statement_to_table(tenant_id: str, entry: Dict[str, str]) -> None:
    item = {
        "TenantID": tenant_id,
        "StatementID": entry["statement_id"],
        "OriginalStatementFilename": entry["statement_name"],
        "ContactID": entry["contact_id"],
        "ContactName": entry["contact_name"],
        # Store upload time in UTC for sorting/filtering
        "UploadedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "Completed": "false",
        "RecordType": "statement",
    }
    try:
        # Ensure we don't overwrite an existing statement for this tenant.
        # NOTE: Table key schema is (TenantID, StatementID). Using StatementID here is intentional.
        tenant_statements_table.put_item(Item=item, ConditionExpression=Attr("StatementID").not_exists())
        logger.info("Statement added to table", tenant_id=tenant_id, statement_id=entry["statement_id"], contact_id=entry.get("contact_id"))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Using single-quotes to simplify nested quotes in f-string
            raise ValueError(f"Statement {entry['statement_name']} already exists") from e
        raise


def _clean_key_segment(value: Optional[str], label: str) -> str:
    segment = (value or "").strip()
    if not segment:
        raise ValueError(f"{label} is required for S3 key construction")
    if any(sep in segment for sep in ("/", "\\")):
        raise ValueError(f"{label} cannot contain path separators")
    return segment


def _statement_s3_key(tenant_id: str, statement_id: str, extension: str) -> str:
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


def fetch_json_statement(tenant_id: str, contact_id: str, bucket: str, json_key: str) -> Tuple[Dict[str, Any], FileStorage]:
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
    data = json.loads(json_bytes.decode("utf-8"))

    filename = json_key.rsplit("/", 1)[-1]
    fs = FileStorage(stream=io.BytesIO(json_bytes), filename=filename)
    return data, fs


def get_or_create_json_statement(tenant_id: str, contact_id: str, bucket: str, pdf_key: str, json_key: str) -> Tuple[Dict[str, Any], FileStorage]:
    """
    Look for JSON statement in S3. If it exists, download and return it.
    Otherwise, run Textract on the PDF, upload the JSON, and return it.

    Returns:
        (data_dict, FileStorage) where:
          - data_dict is the parsed JSON object
          - FileStorage is a file-like wrapper around the JSON (for reuse)
    """
    try:
        data, fs = fetch_json_statement(tenant_id, contact_id, bucket, json_key)
        item_count = len(data.get("statement_items", []) or []) if isinstance(data, dict) else 0
        logger.info("Found existing JSON, downloading", tenant_id=tenant_id, json_key=json_key, items=item_count)
        return data, fs
    except StatementJSONNotFoundError:
        pass

    # Not found → run Textract
    logger.info("Running Textract for missing JSON", tenant_id=tenant_id, json_key=json_key, pdf_key=pdf_key)
    # Use the provided bucket argument for consistency with the read path.
    # Current callers pass the global bucket, so this does not alter behavior.
    json_fs = run_textraction(bucket=bucket, pdf_key=pdf_key, tenant_id=tenant_id, contact_id=contact_id)
    json_fs.stream.seek(0)
    json_bytes = json_fs.stream.read()

    # Parse
    data = json.loads(json_bytes.decode("utf-8"))
    item_count = len(data.get("statement_items", []) or []) if isinstance(data, dict) else 0
    logger.info("Parsed generated JSON", tenant_id=tenant_id, json_key=json_key, items=item_count, bytes=len(json_bytes))

    # Upload new JSON
    upload_statement_to_s3(io.BytesIO(json_bytes), json_key)

    # Return both
    fs = FileStorage(stream=io.BytesIO(json_bytes), filename=json_key.rsplit("/", 1)[-1])
    logger.info("Generated JSON via Textract", tenant_id=tenant_id, json_key=json_key, items=item_count, bytes=len(json_bytes))
    return data, fs


def textract_in_background(tenant_id: str, contact_id: Optional[str], pdf_key: str, json_key: str) -> None:
    """Run get_or_create_json_statement to generate and upload JSON.

    Designed to be run off the request thread. Swallows exceptions after logging.
    """
    try:
        # This will no-op if JSON already exists; otherwise runs Textract and uploads JSON
        get_or_create_json_statement(tenant_id=tenant_id, contact_id=contact_id or "", bucket=S3_BUCKET_NAME, pdf_key=pdf_key, json_key=json_key)
        logger.info("Textraction complete", tenant_id=tenant_id, contact_id=contact_id, pdf_key=pdf_key, json_key=json_key)
    except Exception:
        logger.exception("Textraction failed", tenant_id=tenant_id, contact_id=contact_id, pdf_key=pdf_key, json_key=json_key)

# -----------------------------
# Helpers for statement view
# -----------------------------

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")


def _normalize_separators(value: Any, decimal_separator: Optional[str] = None, thousands_separator: Optional[str] = None) -> Optional[str]:
    """Normalize a raw numeric string using configured separators."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return str(value)

    text = str(value).strip()
    if not text:
        return None

    cleaned = _NON_NUMERIC_RE.sub("", text)

    dec = decimal_separator or "."
    thou = thousands_separator if thousands_separator is not None else ","

    if thou and thou != dec:
        cleaned = cleaned.replace(thou, "")

    if dec and dec != ".":
        cleaned = cleaned.replace(dec, ".")

    return cleaned


def _to_decimal(x: Any, *, decimal_separator: Optional[str] = None, thousands_separator: Optional[str] = None) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    normalized = _normalize_separators(x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
    if normalized is None:
        if isinstance(x, str) and x.strip():
            logger.warning("Unable to normalize numeric value", raw_value=x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        logger.warning("Unable to parse numeric value", raw_value=x, normalized_value=normalized, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
        return None


def format_money(x: Any, *, decimal_separator: Optional[str] = None, thousands_separator: Optional[str] = None) -> str:
    """Format a number with thousands separators and 2 decimals.

    Returns empty string for empty input; returns original string if not numeric.
    """
    d = _to_decimal(x, decimal_separator=decimal_separator, thousands_separator=thousands_separator)
    if d is None:
        return "" if x in (None, "") else str(x)
    return f"{d:,.2f}"

def get_items_template_from_config(contact_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the items template mapping from a contact config.

    Supports three shapes for backward/forward compatibility:
      - Legacy:   contact_config["statement_items"] is a 1-item list of dict.
      - Nested:   contact_config["statement_items"] is a dict.
      - Flattened: the template keys live directly at the root of contact_config.
    """
    if not isinstance(contact_config, dict):
        return {}

    cfg = contact_config.get("statement_items")
    if isinstance(cfg, dict):
        return cfg
    if isinstance(cfg, list) and cfg:
        first = cfg[0]
        return first if isinstance(first, dict) else {}

    # Flattened/root form: assume the root dict itself is the template mapping
    return contact_config


def get_date_format_from_config(contact_config: Dict[str, Any]) -> Optional[str]:
    """Extract the configured date format from a contact configuration."""
    if not isinstance(contact_config, dict):
        return None

    fmt = contact_config.get("date_format")
    return str(fmt) if fmt else None


_ALLOWED_DECIMAL_SEPARATORS = {".", ","}
_ALLOWED_THOUSANDS_SEPARATORS = {",", ".", " ", "'", ""}
_DEFAULT_DECIMAL_SEPARATOR = "."
_DEFAULT_THOUSANDS_SEPARATOR = ","


def get_number_separators_from_config(contact_config: Dict[str, Any]) -> Tuple[str, str]:
    """Return (decimal_separator, thousands_separator) with sensible defaults."""
    if not isinstance(contact_config, dict):
        return _DEFAULT_DECIMAL_SEPARATOR, _DEFAULT_THOUSANDS_SEPARATOR

    dec_raw = contact_config.get("decimal_separator")
    thou_raw = contact_config.get("thousands_separator")

    dec = str(dec_raw).strip() if isinstance(dec_raw, str) else dec_raw
    thou = str(thou_raw) if isinstance(thou_raw, str) else thou_raw

    if dec not in _ALLOWED_DECIMAL_SEPARATORS:
        dec = _DEFAULT_DECIMAL_SEPARATOR
    if thou not in _ALLOWED_THOUSANDS_SEPARATORS:
        thou = _DEFAULT_THOUSANDS_SEPARATOR

    return dec or _DEFAULT_DECIMAL_SEPARATOR, thou if thou is not None else _DEFAULT_THOUSANDS_SEPARATOR


def prepare_display_mappings(items: List[Dict], contact_config: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, str]], Dict[str, str], Optional[str]]:
    """
    Build the display headers, filtered left rows, header->invoice_field map,
    and detect which header corresponds to the invoice "number".

    Returns: (display_headers, rows_by_header, header_to_field, item_number_header)
    """
    # Derive raw headers from the JSON statement (order preserved)
    raw_headers = list(items[0].get("raw", {}).keys()) if items else []

    # Invert the mapping with normalization: statement header -> canonical field
    def _n(s: Any) -> str:
        return " ".join(str(s or "").split()).strip().lower()

    items_template = get_items_template_from_config(contact_config)
    header_to_field_norm: Dict[str, str] = {}
    # Simple (string) mappings first
    for canonical_field, mapped in (items_template or {}).items():
        if canonical_field in {"raw", "date_format", "item_type"}:
            continue
        if canonical_field == "reference":
            continue
        if isinstance(mapped, str) and mapped.strip():
            header_to_field_norm[_n(mapped)] = canonical_field
    # Special case: total can be a list of possible headers; include all candidates.
    mapped_total = (items_template or {}).get("total")
    if isinstance(mapped_total, list) and mapped_total:
        for h in mapped_total:
            if isinstance(h, str) and h.strip():
                header_to_field_norm[_n(h)] = "total"

    # Only display headers present in the mapping (case-insensitive match),
    # and ensure at most one header per canonical field (e.g., only one amount column).
    header_to_field: Dict[str, str] = {}
    display_headers: List[str] = []
    for h in raw_headers:
        canon = header_to_field_norm.get(_n(h))
        if not canon:
            continue
        header_to_field[h] = canon
        display_headers.append(h)

    # Re-order the display headers to prefer familiar Xero columns when present.
    preferred_field_order = ["date", "due_date", "number", "total"]
    ordered_headers: List[str] = []
    for canonical_field in preferred_field_order:
        header_match = next((hdr for hdr in display_headers if header_to_field.get(hdr) == canonical_field), None)
        if header_match:
            ordered_headers.append(header_match)
    # Keep any remaining statement-mapped headers in their existing order (currently are not any but keeping for future).
    for hdr in display_headers:
        if hdr not in ordered_headers:
            ordered_headers.append(hdr)
    display_headers = ordered_headers

    # Convert raw rows into dicts filtered by display headers, normalizing date fields for display
    rows_by_header: List[Dict[str, str]] = []
    date_fmt = get_date_format_from_config(contact_config)
    dec_sep, thou_sep = get_number_separators_from_config(contact_config)
    numeric_fields = {"total"}
    for it in items:
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        row: Dict[str, str] = {}
        for h in display_headers:
            v = raw.get(h, "")
            # If this header maps to a canonical date field, normalize to the configured format
            canon = header_to_field.get(h)
            if canon in {"date", "due_date"}:
                dt = coerce_datetime_with_template(v, date_fmt)
                if dt is not None:
                    if date_fmt:
                        v = format_iso_with(dt, date_fmt)
                    else:
                        v = dt.strftime("%Y-%m-%d")
            elif canon in numeric_fields:
                v = format_money(v, decimal_separator=dec_sep, thousands_separator=thou_sep)
            row[h] = v
        rows_by_header.append(row)

    # Identify which header maps to the canonical "number" field
    item_number_header: Optional[str] = None
    for h in display_headers:
        if header_to_field.get(h) == "number":
            item_number_header = h
            break

    return display_headers, rows_by_header, header_to_field, item_number_header


def match_invoices_to_statement_items(items: List[Dict], rows_by_header: List[Dict[str, str]], item_number_header: Optional[str], invoices: List[Dict]) -> Dict[str, Dict]:
    """
    Build mapping from statement invoice number -> { invoice, statement_item, match_type, match_score, matched_invoice_number }.

    Strategy:
      1) Exact string match on the displayed value.
      2) Substring match on a normalized form (alphanumeric only, case-insensitive),
         e.g. "Invoice # INV-12345" contains "INV12345".
      No generic fuzzy similarity to avoid near-number false positives.
    """
    matched: Dict[str, Dict] = {}
    if not item_number_header:
        return matched

    # Build fast lookup for statement items by their displayed invoice number
    stmt_by_number: Dict[str, Dict] = {}
    for it in items:
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        num = raw.get(item_number_header, "")
        if not num:
            continue
        key = str(num).strip()
        if key:
            stmt_by_number[key] = it

    # 1) Exact matches
    used_invoice_ids: set = set()
    used_invoice_numbers: set = set()
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        key = str(inv_no).strip()
        if not key:
            continue
        stmt_item = stmt_by_number.get(key)
        if stmt_item is not None and key not in matched:
            matched[key] = {
                "invoice": inv,
                "statement_item": stmt_item,
                "match_type": "exact",
                "match_score": 1.0,
                "matched_invoice_number": key,
            }
            logger.info("Exact match", statement_number=key, invoice_number=key, statement_item=stmt_item, xero_item=inv)
            # Track used invoice to exclude from fuzzy matching pool
            inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
            if inv_id:
                used_invoice_ids.add(inv_id)
            used_invoice_numbers.add(key)

    # 2) Substring matches (normalized) for any unmatched numbers
    def _norm_num(s: str) -> str:
        s = str(s or "").upper().strip()
        return "".join(ch for ch in s if ch.isalnum())

    candidates = []
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        inv_no_str = str(inv_no).strip()
        if not inv_no_str:
            continue
        # Exclude invoices already matched exactly
        if (inv.get("invoice_id") if isinstance(inv, dict) else None) in used_invoice_ids:
            continue
        if inv_no_str in used_invoice_numbers:
            continue
        candidates.append((inv_no_str, inv, _norm_num(inv_no_str)))

    numbers_in_rows = [(r.get(item_number_header) or "").strip() for r in rows_by_header if r.get(item_number_header)]
    missing = [n for n in numbers_in_rows if n and n not in matched]

    # Keywords indicating the statement number cell refers to a payment action, not an invoice number
    payment_keywords = ("payment", "paid", "remittance", "receipt")

    for key in missing:
        stmt_item = stmt_by_number.get(key)
        if stmt_item is None:
            continue

        # Guard: if the number cell clearly references a payment, skip substring matching
        lowered = str(key).casefold()
        if any(kw in lowered for kw in payment_keywords):
            logger.info("Skipping substring match due to payment keywords", statement_number=key)
            continue
        target_norm = _norm_num(key)

        hits = []
        for cand_no, inv, cand_norm in candidates:
            inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
            if inv_id in used_invoice_ids or cand_no in used_invoice_numbers:
                continue
            if not target_norm or not cand_norm:
                continue
            if cand_norm == target_norm or cand_norm in target_norm or target_norm in cand_norm:
                hits.append((cand_no, inv, len(cand_norm)))

        if hits:
            # Prefer the most specific (longest normalized) candidate
            hits.sort(key=lambda t: t[2], reverse=True)
            inv_no_best, inv_obj, _ = hits[0]
            matched[key] = {
                "invoice": inv_obj,
                "statement_item": stmt_item,
                "match_type": "substring" if inv_no_best != key else "exact",
                "match_score": 1.0,
                "matched_invoice_number": inv_no_best,
            }
            kind = "Exact" if inv_no_best == key else "Substring"
            logger.info(
                "Statement match",
                match_type=kind,
                statement_number=key,
                invoice_number=inv_no_best,
                statement_item=stmt_item,
                xero_item=inv_obj,
            )
            # Mark this invoice as used to prevent reuse in subsequent substring matches
            inv_id = inv_obj.get("invoice_id") if isinstance(inv_obj, dict) else None
            if inv_id:
                used_invoice_ids.add(inv_id)
            used_invoice_numbers.add(inv_no_best)
        else:
            logger.info("No match for statement number", statement_number=key)

    return matched


def build_right_rows(
    rows_by_header: List[Dict[str, str]],
    display_headers: List[str],
    header_to_field: Dict[str, str],
    matched_map: Dict[str, Dict],
    item_number_header: Optional[str],
    date_format: Optional[str] = None,
    decimal_separator: Optional[str] = None,
    thousands_separator: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Using the matched map, build the right-hand table rows with values from
    the invoice, aligned to the same display headers and row order as the left.
    """
    right_rows = []
    numeric_fields = {"total"}

    for r in rows_by_header:
        inv_no = (r.get(item_number_header) or "").strip() if item_number_header else ""
        rec = (matched_map.get(inv_no, {}) or {})
        inv = rec.get("invoice", {}) if isinstance(rec, dict) else {}

        inv_total = inv.get("total")

        row_right = {}
        for h in display_headers:
            invoice_field = header_to_field.get(h)
            if not invoice_field:
                row_right[h] = ""
                continue

            if invoice_field == "total":
                # Only populate the headers that have a value on the statement side
                left_val = r.get(h)
                if left_val is not None and str(left_val).strip():
                    left_dec = _to_decimal(
                        left_val,
                        decimal_separator=decimal_separator,
                        thousands_separator=thousands_separator,
                    )
                    if left_dec is not None and left_dec == Decimal(0):
                        row_right[h] = format_money(0)
                    else:
                        row_right[h] = format_money(inv_total) if inv_total is not None else ""
                else:
                    row_right[h] = ""
            elif invoice_field in {"due_date", "date"}:
                v = inv.get(invoice_field)
                if v is None:
                    row_right[h] = ""
                else:
                    fmt = date_format or "YYYY-MM-DD"
                    row_right[h] = format_iso_with(v, fmt)
            else:
                val = inv.get(invoice_field, "")
                if invoice_field in numeric_fields:
                    row_right[h] = format_money(val)
                else:
                    row_right[h] = val

        right_rows.append(row_right)

    return right_rows


def build_row_comparisons(left_rows: List[Dict[str, str]], right_rows: List[Dict[str, str]], display_headers: List[str], header_to_field: Optional[Dict[str, str]] = None) -> List[List[CellComparison]]:
    """
    Build per-cell comparison objects for each row.
    """
    comparisons: List[List[CellComparison]] = []
    for left, right in zip(left_rows, right_rows):
        row_cells: List[CellComparison] = []
        for header in display_headers:
            left_val = left.get(header, "") if isinstance(left, dict) else ""
            right_val = right.get(header, "") if isinstance(right, dict) else ""
            # For the canonical invoice number column, treat values as IDs and
            # consider them matching if one normalized string contains the other.
            if header_to_field and header_to_field.get(header) == "number":
                def _norm_id_text(x: Any) -> str:
                    s = "" if x is None else str(x).strip()
                    return "".join(ch for ch in s.upper() if ch.isalnum())
                a, b = _norm_id_text(left_val), _norm_id_text(right_val)
                matches = bool(a and b and (a == b or a in b or b in a))
            else:
                matches = equal(left_val, right_val)
            canonical = (header_to_field or {}).get(header)
            row_cells.append(
                CellComparison(
                    header=header,
                    statement_value="" if left_val is None else str(left_val),
                    xero_value="" if right_val is None else str(right_val),
                    matches=matches,
                    canonical_field=canonical,
                )
            )
        comparisons.append(row_cells)
    return comparisons
