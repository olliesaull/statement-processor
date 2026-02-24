"""Flask application for the statement processor service."""

import json
import os
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any

import requests
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFProtect
from werkzeug.datastructures import FileStorage

from config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME, SESSION_FERNET_KEY, STAGE
from core.contact_config_metadata import EXAMPLE_CONFIG, FIELD_DESCRIPTIONS
from core.get_contact_config import get_contact_config, set_contact_config
from core.item_classification import guess_statement_item_type
from core.models import ContactConfig, StatementItem
from core.statement_detail_types import MatchByItemId, MatchedInvoiceMap, PaymentNumberMap, StatementItemPayload, StatementRowsByHeader, StatementRowViewModel, XeroDocumentPayload
from core.statement_row_palette import STATEMENT_ROW_CSS_VARIABLES
from logger import logger
from sync import check_load_required, sync_data
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils.auth import (
    active_tenant_required,
    block_when_loading,
    clear_session_is_set_cookie,
    has_cookie_consent,
    route_handler_logging,
    save_xero_oauth2_token,
    scope_str,
    set_session_is_set_cookie,
    xero_token_required,
)
from utils.dynamo import (
    add_statement_to_table,
    delete_statement_data,
    get_completed_statements,
    get_incomplete_statements,
    get_statement_item_status_map,
    get_statement_record,
    mark_statement_completed,
    persist_item_types_to_dynamo,
    set_all_statement_items_completed,
    set_statement_item_completed,
)
from utils.encrypted_chunked_session import EncryptedChunkedSessionInterface
from utils.statement_excel_export import build_statement_excel_payload
from utils.statement_rows import format_item_type_label as _format_item_type_label
from utils.statement_rows import xero_ids_for_row as _xero_ids_for_row
from utils.statement_view import build_right_rows, build_row_comparisons, get_date_format_from_config, get_number_separators_from_config, match_invoices_to_statement_items, prepare_display_mappings
from utils.storage import StatementJSONNotFoundError, fetch_json_statement, is_allowed_pdf, statement_json_s3_key, statement_pdf_s3_key, upload_statement_to_s3
from utils.workflows import start_textraction_state_machine
from xero_repository import get_contacts, get_credit_notes_by_contact, get_invoices_by_contact, get_payments_by_contact

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(16))

# Enable CSRF protection globally
csrf = CSRFProtect(app)

MAX_UPLOAD_MB = os.getenv("MAX_UPLOAD_MB", "10")
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

_session_cookie_secure = (os.getenv("SESSION_COOKIE_SECURE") or "true").strip().lower() in {"1", "true", "yes", "on"}
_session_ttl_seconds = int(os.getenv("SESSION_TTL_SECONDS", "900"))
_session_chunk_size = int(os.getenv("SESSION_COOKIE_CHUNK_SIZE", "3700"))
_session_max_chunks = int(os.getenv("SESSION_COOKIE_MAX_CHUNKS", "8"))
app.config.update(
    SESSION_COOKIE_SECURE=_session_cookie_secure,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_REFRESH_EACH_REQUEST=True,
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=_session_ttl_seconds),
)
app.session_interface = EncryptedChunkedSessionInterface(fernet_key=SESSION_FERNET_KEY, ttl_seconds=_session_ttl_seconds, chunk_size=_session_chunk_size, max_chunks=_session_max_chunks)


# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

XERO_OIDC_METADATA_URL = os.getenv("XERO_OIDC_METADATA_URL", "https://identity.xero.com/.well-known/openid-configuration")

if STAGE == "prod":
    REDIRECT_URI = "https://cloudcathode.com/callback"
elif STAGE == "dev":
    REDIRECT_URI = "https://s7mznicnms.eu-west-1.awsapprunner.com/callback"
else:
    REDIRECT_URI = "http://localhost:8080/callback"

oauth = OAuth(app)
oauth.register(
    name="xero",
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    # Load endpoints + JWKS from OIDC metadata so Authlib can validate id_tokens.
    server_metadata_url=XERO_OIDC_METADATA_URL,
    # Reuse the existing scope string to keep requested permissions unchanged.
    client_kwargs={"scope": scope_str()},
)

_executor = ThreadPoolExecutor(max_workers=2)


DEFAULT_DECIMAL_SEPARATOR = "."
DEFAULT_THOUSANDS_SEPARATOR = ","
DECIMAL_SEPARATOR_OPTIONS = [(".", "Dot (.)"), (",", "Comma (,)")]
THOUSANDS_SEPARATOR_OPTIONS = [("", "None"), (",", "Comma (,)"), (".", "Dot (.)"), (" ", "Space ( )"), ("'", "Apostrophe (')")]
DECIMAL_SEPARATOR_VALUES = {opt[0] for opt in DECIMAL_SEPARATOR_OPTIONS}
THOUSANDS_SEPARATOR_VALUES = {opt[0] for opt in THOUSANDS_SEPARATOR_OPTIONS}


@app.context_processor
def _inject_statement_row_palette_css() -> dict[str, Any]:
    """Expose statement row CSS variables to every template.

    Args:
        None.

    Returns:
        Template variables containing the statement row CSS variable map.
    """
    # We render CSS variables from one shared Python palette so table colors stay consistent between web UI and the Excel export.
    # Payload is tiny so performance impact of injecting on every page is negligible
    return {"statement_row_css_variables": STATEMENT_ROW_CSS_VARIABLES}


def _trigger_initial_sync_if_required(tenant_id: str | None) -> None:
    """Kick off an initial load if the tenant has no cached data yet."""
    if not tenant_id:
        return

    if check_load_required(tenant_id):
        oauth_token = session.get("xero_oauth2_token")
        if not oauth_token:
            logger.warning("Skipping background sync; missing OAuth token", tenant_id=tenant_id)
        else:
            _executor.submit(sync_data, tenant_id, TenantStatus.LOADING, oauth_token)


def _set_active_tenant(tenant_id: str | None) -> None:
    """Persist the selected tenant in the session."""
    tenants = session.get("xero_tenants", []) or []
    tenant_map = {t.get("tenantId"): t for t in tenants if t.get("tenantId")}
    if tenant_id and tenant_id in tenant_map:
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_map[tenant_id].get("tenantName")
        _trigger_initial_sync_if_required(tenant_id)
    else:
        session.pop("xero_tenant_id", None)
        session.pop("xero_tenant_name", None)


def _normalize_decimal_separator(value: str | None) -> str:
    """Coerce the decimal separator to a supported value."""
    if value in DECIMAL_SEPARATOR_VALUES:
        return value or DEFAULT_DECIMAL_SEPARATOR
    return DEFAULT_DECIMAL_SEPARATOR


def _normalize_thousands_separator(value: str | None) -> str:
    """Coerce the thousands separator to a supported value."""
    if value in THOUSANDS_SEPARATOR_VALUES:
        return value if value is not None else DEFAULT_THOUSANDS_SEPARATOR
    return DEFAULT_THOUSANDS_SEPARATOR


def _build_config_rows(cfg: ContactConfig) -> list[dict[str, Any]]:
    """Build table rows for canonical fields using existing config values."""
    flat: dict[str, Any] = {}
    allowed_keys = set(StatementItem.model_fields.keys())
    disallowed = {"raw", "statement_item_id"}
    for key, value in cfg.model_dump().items():
        if key in allowed_keys and key not in disallowed:
            flat[key] = value

    flat.pop("reference", None)
    flat.pop("item_type", None)

    # Canonical field order from the Pydantic model, prioritising config UI alignment.
    preferred_order = ["number", "total", "date", "due_date"]
    model_fields = [f for f in dict(StatementItem.model_fields) if f not in {"raw", "statement_item_id", "item_type"}]
    remaining_fields = [f for f in model_fields if f not in preferred_order]
    canonical_order = preferred_order + remaining_fields

    rows: list[dict[str, Any]] = []
    for f in canonical_order:
        if f in {"reference", "item_type"}:
            continue
        val = flat.get(f)
        if f == "total":
            values = [str(v) for v in val] if isinstance(val, list) else [""]
            rows.append({"field": f, "values": values or [""], "is_multi": True})
        else:
            values = [str(val)] if isinstance(val, str) else [""]
            rows.append({"field": f, "values": values, "is_multi": False})
    return rows


def _load_config_context(tenant_id: str | None, contact_lookup: dict[str, str], selected_contact_name: str) -> dict[str, Any]:
    """Load a contact config and return updates for the template context."""
    selected_contact_id = contact_lookup.get(selected_contact_name)
    logger.info("Config load submitted", tenant_id=tenant_id, contact_name=selected_contact_name, contact_id=selected_contact_id)

    updates: dict[str, Any] = {"selected_contact_name": selected_contact_name, "selected_contact_id": selected_contact_id}

    if not selected_contact_id:
        updates["error"] = "Please select a valid contact."
        logger.info("Config load failed", tenant_id=tenant_id, contact_name=selected_contact_name)
        return updates

    try:
        cfg = get_contact_config(tenant_id, selected_contact_id)
        updates["mapping_rows"] = _build_config_rows(cfg)
        updates["decimal_separator"] = _normalize_decimal_separator(cfg.decimal_separator)
        updates["thousands_separator"] = _normalize_thousands_separator(cfg.thousands_separator)
        updates["date_format"] = cfg.date_format or ""
        logger.info("Config loaded", tenant_id=tenant_id, contact_id=selected_contact_id, keys=len(cfg.model_dump()))
        return updates
    except KeyError:
        updates["mapping_rows"] = _build_config_rows(ContactConfig())
        updates["decimal_separator"] = DEFAULT_DECIMAL_SEPARATOR
        updates["thousands_separator"] = DEFAULT_THOUSANDS_SEPARATOR
        updates["date_format"] = ""
        updates["message"] = "No existing config found. You can create one below."
        logger.info("Config not found", tenant_id=tenant_id, contact_id=selected_contact_id)
        return updates
    except Exception as exc:
        updates["error"] = f"Failed to load config: {exc}"
        logger.info("Config load error", tenant_id=tenant_id, contact_id=selected_contact_id, error=exc)
        return updates


def _save_config_context(tenant_id: str | None, form: Any) -> dict[str, Any]:
    """Persist config edits and return updates for the template context."""
    selected_contact_id = form.get("contact_id")
    selected_contact_name = form.get("contact_name")
    logger.info("Config save submitted", tenant_id=tenant_id, contact_id=selected_contact_id, contact_name=selected_contact_name)

    updates: dict[str, Any] = {"selected_contact_id": selected_contact_id, "selected_contact_name": selected_contact_name}

    try:
        try:
            existing = get_contact_config(tenant_id, selected_contact_id)
        except KeyError:
            existing = ContactConfig()
        posted_fields = [f for f in form.getlist("fields[]") if f]

        selected_decimal_separator = _normalize_decimal_separator(form.get("decimal_separator"))
        selected_thousands_separator = _normalize_thousands_separator(form.get("thousands_separator"))
        selected_date_format = (form.get("date_format") or "").strip()

        # Preserve any root keys not shown in the mapping editor.
        existing_payload = existing.model_dump()
        preserved = {k: v for k, v in existing_payload.items() if k not in posted_fields and k not in {"reference", "item_type"}}

        new_map: dict[str, Any] = {}
        for f in posted_fields:
            if f == "total":
                total_vals = [v.strip() for v in form.getlist("map[total][]") if v.strip()]
                new_map["total"] = total_vals
            else:
                val = form.get(f"map[{f}]")
                new_map[f] = (val or "").strip()
        combined = ContactConfig.model_validate(
            {**preserved, **new_map, "date_format": selected_date_format, "decimal_separator": selected_decimal_separator, "thousands_separator": selected_thousands_separator}
        )

        updates["decimal_separator"] = selected_decimal_separator
        updates["thousands_separator"] = selected_thousands_separator
        updates["date_format"] = selected_date_format

        # Validate required mappings before saving.
        number_value = (new_map.get("number") or "").strip()
        total_values = new_map.get("total") if isinstance(new_map.get("total"), list) else []
        if not number_value:
            updates["error"] = "The 'Number' field is mandatory. Please map the statement column that contains item numbers (e.g. invoice number)."
            updates["message"] = None
            updates["mapping_rows"] = _build_config_rows(combined)
        elif not total_values:
            updates["error"] = "The 'Total' field is mandatory. Please map at least one statement column with totals."
            updates["message"] = None
            updates["mapping_rows"] = _build_config_rows(combined)
        else:
            set_contact_config(tenant_id, selected_contact_id, combined)
            logger.info("Contact config saved", tenant_id=tenant_id, contact_id=selected_contact_id, contact_name=selected_contact_name, config=combined.model_dump())
            updates["message"] = "Config updated successfully."
            updates["mapping_rows"] = _build_config_rows(combined)
    except Exception as exc:
        updates["error"] = f"Failed to save config: {exc}"
        logger.info("Config save failed", tenant_id=tenant_id, contact_id=selected_contact_id, error=exc)
    return updates


@app.route("/api/tenant-statuses", methods=["GET"])
@xero_token_required
def tenant_status():
    """Return tenant sync statuses from DynamoDB."""
    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = [t.get("tenantId") for t in tenant_records if isinstance(t, dict)]
    try:
        tenant_statuses = TenantDataRepository.get_tenant_statuses(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant sync status", tenant_ids=tenant_ids, error=exc)
        return jsonify({"error": "Unable to determine sync status"}), 500

    return jsonify(tenant_statuses), 200


@app.route("/api/tenants/<tenant_id>/sync", methods=["POST"])
@xero_token_required
def trigger_tenant_sync(tenant_id: str):
    """Trigger a background sync for the specified tenant."""
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return jsonify({"error": "TenantID is required"}), 400

    # Only allow syncs for tenants already connected in this session.
    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = {t.get("tenantId") for t in tenant_records if isinstance(t, dict)}
    if tenant_id not in tenant_ids:
        logger.info("Manual sync denied; tenant not authorized", tenant_id=tenant_id)
        return jsonify({"error": "Tenant not authorized"}), 403

    oauth_token = session.get("xero_oauth2_token")
    if not oauth_token:
        logger.warning("Manual sync denied; missing OAuth token", tenant_id=tenant_id)
        return jsonify({"error": "Missing OAuth token"}), 400

    try:
        # Fire-and-forget: sync runs in the background.
        _executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token)  # TODO: Perhaps worth checking if there is row in DDB/files in S3
        logger.info("Manual tenant sync triggered", tenant_id=tenant_id)
        return jsonify({"started": True}), 202
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        return jsonify({"error": "Failed to trigger sync"}), 500


@app.route("/")
@route_handler_logging
def index():
    """Render the landing page."""
    logger.info("Rendering index")
    return render_template("index.html")


@app.route("/tenant_management")
@route_handler_logging
@xero_token_required
def tenant_management():
    """Render tenant management, consuming one-time messages from session."""
    tenants = session.get("xero_tenants") or []
    active_tenant_id = session.get("xero_tenant_id")
    # Messages are popped so they only display once.
    message = session.pop("tenant_message", None)
    error = session.pop("tenant_error", None)

    active_tenant = next((t for t in tenants if t.get("tenantId") == active_tenant_id), None)
    logger.info(
        "Rendering tenant_management page",
        active_tenant_id=active_tenant_id,
        tenants=len(tenants),
        has_message=bool(message),
        has_error=bool(error),
        authenticated=bool(session.get("xero_oauth2_token")),
    )

    return render_template("tenant_management.html", tenants=tenants, active_tenant_id=active_tenant_id, active_tenant=active_tenant, message=message, error=error)


@app.route("/favicon.ico")
def ignore_favicon():
    """Return empty 204 for favicon requests."""
    return "", 204


def _get_active_contacts_for_upload() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Return active contacts and a name -> ID lookup for the upload form."""
    contacts_raw = get_contacts()
    contacts_active = [c for c in contacts_raw if str(c.get("contact_status") or "").upper() == "ACTIVE"]
    contacts_list = sorted(contacts_active, key=lambda c: (c.get("name") or "").casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    return contacts_list, contact_lookup


def _validate_upload_payload(files: list, names: list[str]) -> bool:
    """Validate the number of uploaded files and selected contacts."""
    if not files:
        logger.info("Upload rejected; no statement files provided.")
        return False
    if len(files) != len(names):
        logger.info("Upload rejected; file count does not match contact selections.")
        return False
    return True


def _ensure_contact_config(tenant_id: str | None, contact_id: str, contact_name: str, filename: str, error_messages: list[str]) -> bool:
    """Ensure the contact has a config; on failure, log and append a user-facing error."""
    try:
        get_contact_config(tenant_id, contact_id)
    except KeyError:
        logger.warning("Upload blocked; contact config missing", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=filename)
        error_messages.append(f"Contact '{contact_name}' does not have a statement config yet. Please configure it before uploading.")
        return False
    except Exception as exc:
        logger.exception("Upload blocked; config lookup failed", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=filename, error=exc)
        error_messages.append(f"Could not load the config for '{contact_name}'. Please try again later.")
        return False
    return True


def _process_statement_upload(tenant_id: str | None, uploaded_file: FileStorage, contact_id: str, contact_name: str) -> str:
    """Upload the PDF, register the statement, and kick off textraction."""
    file_bytes = getattr(uploaded_file, "content_length", None)
    statement_id = str(uuid.uuid4())
    logger.info(
        "Preparing statement upload", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_id=statement_id, statement_filename=uploaded_file.filename, bytes=file_bytes
    )

    entry = {"statement_id": statement_id, "statement_name": uploaded_file.filename, "contact_name": contact_name, "contact_id": contact_id}

    # Upload PDF to S3 first so downstream processing can read it.
    pdf_statement_key = statement_pdf_s3_key(tenant_id, statement_id)
    upload_statement_to_s3(fs_like=uploaded_file, key=pdf_statement_key)
    logger.info("Uploaded statement PDF", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, s3_key=pdf_statement_key)

    # Persist statement metadata to DynamoDB.
    add_statement_to_table(tenant_id, entry)
    logger.info("Statement submitted and metadata registered", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, table_entry=entry)

    # Kick off background textraction so it's ready by the time the user views it.
    json_statement_key = statement_json_s3_key(tenant_id, statement_id)
    started = start_textraction_state_machine(tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_statement_key, json_key=json_statement_key)

    log_kwargs = {"tenant_id": tenant_id, "contact_id": contact_id, "statement_id": statement_id, "pdf_key": pdf_statement_key, "json_key": json_statement_key}

    if started:
        logger.info("Started textraction workflow", **log_kwargs)
    else:
        logger.error("Failed to start textraction workflow", **log_kwargs)

    return statement_id


@app.route("/upload-statements", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant before uploading statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def upload_statements():
    """Upload one or more PDF statements and register them for processing."""
    tenant_id = session.get("xero_tenant_id")

    contacts_list, contact_lookup = _get_active_contacts_for_upload()
    success_count: int | None = None
    error_messages: list[str] = []
    logger.info("Rendering upload statements", tenant_id=tenant_id, available_contacts=len(contacts_list))

    uploads_ok = 0
    if request.method == "POST":
        files = [f for f in request.files.getlist("statements") if f and f.filename]
        names = request.form.getlist("contact_names")
        logger.info("Upload statements submitted", tenant_id=tenant_id, files=len(files), names=len(names))
        if _validate_upload_payload(files, names):
            for uploaded_file, contact in zip(files, names, strict=False):
                if not contact.strip():
                    logger.info("Missing contact", statement_filename=uploaded_file.filename)
                    continue
                if not is_allowed_pdf(uploaded_file.filename, uploaded_file.mimetype):
                    logger.info("Rejected non-PDF upload", statement_filename=uploaded_file.filename)
                    continue

                contact_name = contact.strip()
                contact_id: str | None = contact_lookup.get(contact_name)
                if not contact_id:
                    logger.warning("Upload blocked; contact not found", tenant_id=tenant_id, contact_name=contact_name, statement_filename=uploaded_file.filename)
                    error_messages.append(f"Contact '{contact_name}' was not recognised. Please select a contact from the list.")  # nosec B608 - user-facing message only, no SQL execution
                    continue

                if not _ensure_contact_config(tenant_id, contact_id, contact_name, uploaded_file.filename, error_messages):
                    continue

                _process_statement_upload(tenant_id=tenant_id, uploaded_file=uploaded_file, contact_id=contact_id, contact_name=contact_name)
                uploads_ok += 1

        if uploads_ok:
            success_count = uploads_ok
        logger.info("Upload statements processed", tenant_id=tenant_id, succeeded=uploads_ok, errors=list(error_messages))

    return render_template("upload_statements.html", contacts=contacts_list, success_count=success_count, error_messages=error_messages)


@app.route("/instructions")
@route_handler_logging
def instructions():
    """Render the user instructions page."""
    return render_template("instructions.html")


@app.route("/about")
@route_handler_logging
def about():
    """Render the about page."""
    return render_template("about.html")


@app.route("/cookies")
@route_handler_logging
def cookies():
    """Render the cookie policy and consent page."""
    return render_template("cookies.html")


@app.route("/statements")
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statements():
    """Render the statement list with filtering and sorting."""
    tenant_id = session.get("xero_tenant_id")

    # Read query params and normalize sort direction.
    view = request.args.get("view", "incomplete").lower()
    show_completed = view == "completed"
    statement_rows = get_completed_statements() if show_completed else get_incomplete_statements()
    sort_key = request.args.get("sort", "uploaded").lower()
    dir_param = (request.args.get("dir") or "").strip().lower()
    ALLOWED_DIR = {"asc", "desc"}
    default_dir_map = {"contact": "asc", "date_range": "desc", "uploaded": "desc"}
    if sort_key not in {"contact", "date_range", "uploaded"}:
        sort_key = "uploaded"
    current_dir = dir_param if dir_param in ALLOWED_DIR else default_dir_map.get(sort_key, "desc")
    reverse = current_dir == "desc"
    message = session.pop("statements_message", None)

    def _parse_iso_date(value: object) -> date | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            return None

    def _parse_iso_datetime(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        try:
            # Support both "+00:00" and trailing "Z"
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    # Add derived fields for display and sorting.
    for row in statement_rows:
        earliest = _parse_iso_date(row.get("EarliestItemDate"))
        latest = _parse_iso_date(row.get("LatestItemDate"))
        row["_earliest_item_date"] = earliest
        row["_latest_item_date"] = latest
        row["_uploaded_at"] = _parse_iso_datetime(row.get("UploadedAt"))
        if earliest and latest:
            row["ItemDateRangeDisplay"] = earliest.isoformat() if earliest == latest else f"{earliest.isoformat()} - {latest.isoformat()}"
        elif latest:
            row["ItemDateRangeDisplay"] = latest.isoformat()
        elif earliest:
            row["ItemDateRangeDisplay"] = earliest.isoformat()
        else:
            row["ItemDateRangeDisplay"] = "—"

    if sort_key == "date_range":
        statement_rows.sort(key=lambda r: r.get("_latest_item_date") or date.min, reverse=reverse)
    elif sort_key == "uploaded":
        statement_rows.sort(key=lambda r: r.get("_uploaded_at") or datetime.min, reverse=reverse)
    else:
        # Contact: alphabetical or reverse, always keep missing/blank names last
        sort_key = "contact"
        nonempty = [r for r in statement_rows if isinstance(r.get("ContactName"), str) and r.get("ContactName").strip()]
        empty = [r for r in statement_rows if r not in nonempty]
        nonempty.sort(key=lambda r: str(r.get("ContactName")).strip().casefold(), reverse=reverse)
        statement_rows = nonempty + empty

    # Remove helper fields before rendering.
    for row in statement_rows:
        row.pop("_earliest_item_date", None)
        row.pop("_latest_item_date", None)
        row.pop("_uploaded_at", None)

    # Preserve filters when building sort URLs.
    base_args: dict[str, Any] = {}
    if show_completed:
        base_args["view"] = "completed"

    # For each sort key, clicking its button toggles the direction if already active,
    # otherwise applies the default direction for that key.
    def next_dir_for(key: str) -> str:
        if key == sort_key:
            return "asc" if current_dir == "desc" else "desc"
        return default_dir_map.get(key, "desc")

    sort_links = {
        "contact": url_for("statements", **dict(base_args, sort="contact", dir=next_dir_for("contact"))),
        "date_range": url_for("statements", **dict(base_args, sort="date_range", dir=next_dir_for("date_range"))),
        "uploaded": url_for("statements", **dict(base_args, sort="uploaded", dir=next_dir_for("uploaded"))),
    }

    logger.info("Rendering statements", tenant_id=tenant_id, view=view, sort=sort_key, direction=current_dir, statements=len(statement_rows))

    return render_template("statements.html", statements=statement_rows, show_completed=show_completed, message=message, current_sort=sort_key, current_dir=current_dir, sort_links=sort_links)


@app.route("/statement/<statement_id>/delete", methods=["POST"])
@active_tenant_required("Please select a tenant before deleting statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def delete_statement(statement_id: str):
    """Delete the statement and redirect back to the list view."""
    tenant_id = session.get("xero_tenant_id")

    try:
        delete_statement_data(tenant_id, statement_id)
        session["statements_message"] = "Statement deleted."
    except Exception as exc:
        logger.exception("Failed to delete statement", tenant_id=tenant_id, statement_id=statement_id, error=exc)
        session["tenant_error"] = "Unable to delete the statement. Please try again."

    return redirect(url_for("statements"))


def _parse_items_view(raw_value: str | None) -> str:
    """Normalize the statement item filter."""
    items_view = (raw_value or "incomplete").strip().lower()
    if items_view not in {"incomplete", "completed", "all"}:
        return "incomplete"
    return items_view


def _parse_show_payments(raw_value: str | None) -> bool:
    """Normalize the show payments flag."""
    value = (raw_value or "true").strip().lower()
    return value in {"true", "1", "yes", "on"}


def _handle_statement_post_actions(*, tenant_id: str, statement_id: str, form: Any, items_view: str, show_payments: bool) -> Any:
    """Handle POST actions for statement detail views, returning a redirect when applicable."""
    action = form.get("action")
    if action in {"mark_complete", "mark_incomplete"}:
        completed_flag = action == "mark_complete"
        try:
            mark_statement_completed(tenant_id, statement_id, completed_flag)
            try:
                set_all_statement_items_completed(tenant_id, statement_id, completed_flag)
            except Exception as exc:
                logger.exception("Failed to toggle all statement items", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)

            session["statements_message"] = "Statement marked as complete." if completed_flag else "Statement marked as incomplete."
            logger.info("Statement completion updated", tenant_id=tenant_id, statement_id=statement_id, completed=completed_flag)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.exception("Failed to toggle statement completion", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)
            abort(500)
        return redirect(url_for("statements"))

    if action in {"complete_item", "incomplete_item"}:
        statement_item_id = (form.get("statement_item_id") or "").strip()
        if statement_item_id:
            desired_state = action == "complete_item"
            try:
                set_statement_item_completed(tenant_id, statement_item_id, desired_state)
                logger.info("Statement item updated", tenant_id=tenant_id, statement_id=statement_id, statement_item_id=statement_item_id, completed=desired_state)
            except Exception as exc:
                logger.exception(
                    "Failed to toggle statement item completion", statement_id=statement_id, statement_item_id=statement_item_id, tenant_id=tenant_id, desired_state=desired_state, error=exc
                )
        return redirect(url_for("statement", statement_id=statement_id, items_view=items_view, show_payments="true" if show_payments else "false"))

    return None


def _build_match_by_item_id(matched_invoice_to_statement_item: MatchedInvoiceMap) -> MatchByItemId:
    """Return a map of statement_item_id to matched document type/source."""
    match_by_item_id: MatchByItemId = {}
    for match in matched_invoice_to_statement_item.values():
        stmt_item = match.get("statement_item") if isinstance(match, dict) else None
        doc = match.get("invoice") if isinstance(match, dict) else None
        if not isinstance(stmt_item, dict) or not isinstance(doc, dict):
            continue
        statement_item_id = stmt_item.get("statement_item_id")
        if not statement_item_id:
            continue
        doc_type = str(doc.get("type") or "").upper()
        if doc.get("credit_note_id") or doc_type.endswith("CREDIT"):
            match_by_item_id[statement_item_id] = {"type": "credit_note", "source": "credit_note_match"}
        else:
            match_by_item_id[statement_item_id] = {"type": "invoice", "source": "invoice_match"}
    return match_by_item_id


def _build_payment_number_map(invoices: list[XeroDocumentPayload], payments: list[XeroDocumentPayload]) -> PaymentNumberMap:
    """Build a map of invoice number -> payment rows for payment inference."""
    invoice_number_by_id: dict[str, str] = {}
    for inv in invoices:
        inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
        inv_number = str(inv.get("number") or "").strip() if isinstance(inv, dict) else ""
        if inv_id and inv_number:
            invoice_number_by_id[str(inv_id)] = inv_number

    payment_number_map: PaymentNumberMap = {}
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        invoice_id = payment.get("invoice_id")
        if not invoice_id:
            continue
        invoice_number = invoice_number_by_id.get(str(invoice_id))
        if not invoice_number:
            continue
        payment_number_map.setdefault(invoice_number, []).append(payment)
    return payment_number_map


def _classify_statement_items(
    *,
    items: list[StatementItemPayload],
    rows_by_header: StatementRowsByHeader,
    item_number_header: str | None,
    contact_config: ContactConfig,
    matched_invoice_to_statement_item: MatchedInvoiceMap,
    matched_numbers: set[str],
    match_by_item_id: MatchByItemId,
    payment_number_map: PaymentNumberMap,
    statement_id: str,
) -> tuple[list[str], dict[str, str]]:
    """Classify statement items in-place and return item types + updates."""
    classification_updates: dict[str, str] = {}
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        statement_item_id = it.get("statement_item_id")
        raw = it.get("raw", {}) if isinstance(it.get("raw"), dict) else {}
        current_type = str(it.get("item_type") or "").strip().lower()
        row_number = ""
        if item_number_header and idx < len(rows_by_header):
            row_number = str(rows_by_header[idx].get(item_number_header) or "").strip()

        new_type: str | None
        source: str | None
        new_type = None
        source = None

        if statement_item_id and statement_item_id in match_by_item_id:
            entry = match_by_item_id[statement_item_id]
            new_type = entry["type"]
            source = entry["source"]
        elif row_number and row_number in matched_numbers:
            match = matched_invoice_to_statement_item.get(row_number)
            doc = match.get("invoice") if isinstance(match, dict) else None
            if isinstance(doc, dict):
                doc_type = str(doc.get("type") or "").upper()
                if doc.get("credit_note_id") or doc_type.endswith("CREDIT"):
                    new_type = "credit_note"
                    source = "credit_note_match"
                else:
                    new_type = "invoice"
                    source = "invoice_match"
        elif row_number and row_number not in matched_numbers and row_number in payment_number_map:
            new_type = "payment"
            source = "payment_match"

        if not new_type:
            new_type = guess_statement_item_type(raw, it.get("total"), contact_config)
            source = "heuristic"

        if new_type and new_type != current_type:
            it["item_type"] = new_type
            if statement_item_id:
                classification_updates[statement_item_id] = new_type
            logger.info("Statement item type updated", statement_id=statement_id, statement_item_id=statement_item_id, new_type=new_type, previous_type=current_type or "", source=source)

    item_types = [str((it.get("item_type") if isinstance(it, dict) else "") or "").strip().lower() for it in items]
    return item_types, classification_updates


def _persist_classification_updates(*, data: dict[str, Any], statement_id: str, tenant_id: str, json_statement_key: str, classification_updates: dict[str, str]) -> None:
    """Persist updated item types back to S3 and DynamoDB."""
    if not classification_updates:
        return

    try:
        json_payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        upload_statement_to_s3(BytesIO(json_payload), json_statement_key)
        logger.info("Persisted statement item types to S3", statement_id=statement_id, updated=len(classification_updates))
    except Exception as exc:
        logger.exception("Failed to persist statement JSON", statement_id=statement_id, error=str(exc))

    persist_item_types_to_dynamo(tenant_id, classification_updates)
    logger.info("Persisted statement item types to DynamoDB", statement_id=statement_id, updated=len(classification_updates))


def _build_row_matches(rows_by_header: StatementRowsByHeader, item_number_header: str | None, matched_invoice_to_statement_item: MatchedInvoiceMap, row_comparisons: list[list[Any]]) -> list[bool]:
    """Return the per-row match status for coloring and export."""
    if item_number_header:
        row_matches: list[bool] = []
        for r in rows_by_header:
            num = (r.get(item_number_header) or "").strip()
            row_matches.append(bool(num and matched_invoice_to_statement_item.get(num)))
        return row_matches

    # Fallback: if no number mapping, use strict all-cells match
    return [all(cell.matches for cell in row) for row in row_comparisons]


def _build_statement_excel_response(
    *,
    display_headers: list[str],
    rows_by_header: StatementRowsByHeader,
    right_rows_by_header: StatementRowsByHeader,
    row_comparisons: list[list[Any]],
    row_matches: list[bool],
    item_types: list[str],
    items: list[StatementItemPayload],
    item_number_header: str | None,
    matched_invoice_to_statement_item: MatchedInvoiceMap,
    item_status_map: dict[str, bool],
    record: dict[str, Any],
    statement_id: str,
    tenant_id: str,
) -> Any:
    """Build an XLSX export response for the current statement view.

    Args:
        display_headers: Statement display headers.
        rows_by_header: Statement rows keyed by header.
        right_rows_by_header: Xero rows keyed by header.
        row_comparisons: Per-cell comparison results.
        row_matches: Per-row match flags.
        item_types: Item type labels per row.
        items: Statement items payload.
        item_number_header: Header used to map Xero links.
        matched_invoice_to_statement_item: Matched Xero invoice map.
        item_status_map: Statement item completion flags.
        record: Statement metadata record.
        statement_id: Statement identifier.
        tenant_id: Active tenant identifier.

    Returns:
        Flask response containing the XLSX export.
    """
    # Pylint's duplicate-code check compares raw argument forwarding blocks.
    # This explicit mapping is intentional so the route wrapper stays easy to audit.
    # pylint: disable=duplicate-code
    excel_payload, download_name, row_count = build_statement_excel_payload(
        display_headers=display_headers,
        rows_by_header=rows_by_header,
        right_rows_by_header=right_rows_by_header,
        row_comparisons=row_comparisons,
        row_matches=row_matches,
        item_types=item_types,
        items=items,
        item_number_header=item_number_header,
        matched_invoice_to_statement_item=matched_invoice_to_statement_item,
        item_status_map=item_status_map,
        record=record,
        statement_id=statement_id,
    )
    # pylint: enable=duplicate-code
    response = app.response_class(excel_payload, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    logger.info("Statement Excel generated", tenant_id=tenant_id, statement_id=statement_id, rows=row_count, excel_filename=download_name)
    return response


def _item_status(item: StatementItemPayload, item_status_map: dict[str, bool]) -> tuple[str | None, bool]:
    """Return the statement item ID and completion status."""
    statement_item_id = item.get("statement_item_id") if isinstance(item, dict) else None
    if statement_item_id:
        return statement_item_id, item_status_map.get(statement_item_id, False)
    return None, False


def _item_flags(item: StatementItemPayload) -> list[str]:
    """Return normalized, unique flags for a statement item."""
    if not isinstance(item, dict):
        return []
    raw_flags = item.get("_flags") or []
    if not isinstance(raw_flags, list):
        return []
    seen_flags: set[str] = set()
    flags: list[str] = []
    for flag in raw_flags:
        if not isinstance(flag, str):
            continue
        normalized = flag.strip()
        if not normalized or normalized in seen_flags:
            continue
        seen_flags.add(normalized)
        flags.append(normalized)
    return flags


def _build_statement_rows(
    *,
    rows_by_header: StatementRowsByHeader,
    row_comparisons: list[list[Any]],
    row_matches: list[bool],
    items: list[StatementItemPayload],
    item_types: list[str],
    item_status_map: dict[str, bool],
    item_number_header: str | None,
    matched_invoice_to_statement_item: MatchedInvoiceMap,
) -> list[StatementRowViewModel]:
    """Build the rows displayed in the statement detail UI.

    Args:
        rows_by_header: Statement rows keyed by header names.
        row_comparisons: Per-cell comparison results.
        row_matches: Per-row match flags.
        items: Statement item payloads.
        item_types: Item type labels per row.
        item_status_map: Statement item completion flags.
        item_number_header: Header used to map Xero links.
        matched_invoice_to_statement_item: Matched Xero invoice map.

    Returns:
        List of row dicts for the statement detail table.
    """
    statement_rows: list[StatementRowViewModel] = []
    for idx, left_row in enumerate(rows_by_header):
        item = items[idx] if idx < len(items) else {}
        statement_item_id, is_item_completed = _item_status(item, item_status_map)

        flags = _item_flags(item)

        # Build Xero links by extracting IDs from matched data
        xero_invoice_id, xero_credit_note_id = _xero_ids_for_row(item_number_header, left_row, matched_invoice_to_statement_item)

        item_type = (item.get("item_type") if isinstance(item, dict) else None) or (item_types[idx] if idx < len(item_types) else "invoice")
        statement_rows.append(
            {
                "statement_item_id": statement_item_id,
                "cell_comparisons": row_comparisons[idx] if idx < len(row_comparisons) else [],
                "matches": row_matches[idx] if idx < len(row_matches) else False,
                "is_completed": is_item_completed,
                "flags": flags,
                "item_type": item_type,
                "item_type_label": _format_item_type_label(item_type),
                "xero_invoice_id": xero_invoice_id,
                "xero_credit_note_id": xero_credit_note_id,
            }
        )

    return statement_rows


@app.route("/statement/<statement_id>", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statement(statement_id: str):
    """Render the statement detail view, handling actions and exports."""
    tenant_id = session.get("xero_tenant_id")

    record = get_statement_record(tenant_id, statement_id)
    if not record:
        logger.info("Statement record not found", tenant_id=tenant_id, statement_id=statement_id)
        abort(404)

    items_view = _parse_items_view(request.values.get("items_view"))
    show_payments = _parse_show_payments(request.values.get("show_payments"))
    logger.info("Statement detail requested", tenant_id=tenant_id, statement_id=statement_id, items_view=items_view, show_payments=show_payments, method=request.method)

    raw_contact_name = record.get("ContactName")
    contact_name = str(raw_contact_name).strip() if raw_contact_name is not None else ""
    page_heading = contact_name or f"Statement {statement_id}"  # TODO: Could page heading include statement filename instead of statement_id? StatementID is useless for customer

    if request.method == "POST":
        # TODO: This function forces the entire page to re-render when an event occurs (hide/show payments, mark complete/incomplete, etc) - that is slow for large statements
        response = _handle_statement_post_actions(tenant_id=tenant_id, statement_id=statement_id, form=request.form, items_view=items_view, show_payments=show_payments)
        if response is not None:
            return response

    json_statement_key = statement_json_s3_key(tenant_id, statement_id)

    contact_id = record.get("ContactID")
    is_completed = str(record.get("Completed", "")).lower() == "true"
    base_context: dict[str, Any] = {
        "statement_id": statement_id,
        "contact_name": contact_name,
        "page_heading": page_heading,
        "items_view": items_view,
        "show_payments": show_payments,
        "is_completed": is_completed,
    }
    try:
        data = fetch_json_statement(tenant_id=tenant_id, bucket=S3_BUCKET_NAME, json_key=json_statement_key)
    except StatementJSONNotFoundError:
        logger.info("Statement JSON pending", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
        return render_template(
            "statement.html", is_processing=True, incomplete_count=0, completed_count=0, all_statement_rows=[], statement_rows=[], raw_statement_headers=[], has_payment_rows=False, **base_context
        )

    # 1) Parse display configuration and left-side rows
    items: list[StatementItemPayload] = data.get("statement_items", []) or []
    contact_config: ContactConfig = get_contact_config(tenant_id, contact_id)
    decimal_sep, thousands_sep = get_number_separators_from_config(contact_config)
    display_headers, rows_by_header, header_to_field, item_number_header = prepare_display_mappings(items, contact_config)

    # 2) Fetch Xero documents and classify each statement item
    invoices: list[XeroDocumentPayload] = get_invoices_by_contact(contact_id) or []
    credit_notes: list[XeroDocumentPayload] = get_credit_notes_by_contact(contact_id) or []
    payments: list[XeroDocumentPayload] = get_payments_by_contact(contact_id) or []
    logger.info("Fetched Xero documents", statement_id=statement_id, contact_id=contact_id, invoices=len(invoices), credit_notes=len(credit_notes), payments=len(payments))

    docs_for_matching = invoices + credit_notes
    matched_invoice_to_statement_item: MatchedInvoiceMap = match_invoices_to_statement_items(
        items=items, rows_by_header=rows_by_header, item_number_header=item_number_header, invoices=docs_for_matching
    )

    matched_numbers: set[str] = {key for key in matched_invoice_to_statement_item if isinstance(key, str)}
    match_by_item_id = _build_match_by_item_id(matched_invoice_to_statement_item)
    payment_number_map = _build_payment_number_map(invoices, payments)

    item_types, classification_updates = _classify_statement_items(
        items=items,
        rows_by_header=rows_by_header,
        item_number_header=item_number_header,
        contact_config=contact_config,
        matched_invoice_to_statement_item=matched_invoice_to_statement_item,
        matched_numbers=matched_numbers,
        match_by_item_id=match_by_item_id,
        payment_number_map=payment_number_map,
        statement_id=statement_id,
    )

    _persist_classification_updates(data=data, statement_id=statement_id, tenant_id=tenant_id, json_statement_key=json_statement_key, classification_updates=classification_updates)

    # 3) Build right-hand rows from the matched invoices
    date_fmt = get_date_format_from_config(contact_config)

    right_rows_by_header = build_right_rows(
        rows_by_header=rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
        matched_map=matched_invoice_to_statement_item,
        item_number_header=item_number_header,
        date_format=date_fmt,
        decimal_separator=decimal_sep,
        thousands_separator=thousands_sep,
    )

    # 4) Compare LEFT (statement) vs RIGHT (Xero) for per-cell indicators
    row_comparisons = build_row_comparisons(left_rows=rows_by_header, right_rows=right_rows_by_header, display_headers=display_headers, header_to_field=header_to_field)
    # Row highlight: if this row is linked to a Xero document (exact or substring),
    # consider the row a "match" for coloring purposes even if some cells differ.
    row_matches = _build_row_matches(rows_by_header, item_number_header, matched_invoice_to_statement_item, row_comparisons)

    item_status_map = get_statement_item_status_map(tenant_id, statement_id)

    if request.args.get("download") == "xlsx":
        return _build_statement_excel_response(
            display_headers=display_headers,
            rows_by_header=rows_by_header,
            right_rows_by_header=right_rows_by_header,
            row_comparisons=row_comparisons,
            row_matches=row_matches,
            item_types=item_types,
            items=items,
            item_number_header=item_number_header,
            matched_invoice_to_statement_item=matched_invoice_to_statement_item,
            item_status_map=item_status_map,
            record=record,
            statement_id=statement_id,
            tenant_id=tenant_id,
        )

    statement_rows = _build_statement_rows(
        rows_by_header=rows_by_header,
        row_comparisons=row_comparisons,
        row_matches=row_matches,
        items=items,
        item_types=item_types,
        item_status_map=item_status_map,
        item_number_header=item_number_header,
        matched_invoice_to_statement_item=matched_invoice_to_statement_item,
    )

    completed_count = sum(1 for row in statement_rows if row["is_completed"])
    incomplete_count = len(statement_rows) - completed_count
    has_payment_rows = any(row.get("item_type") == "payment" for row in statement_rows)

    if items_view == "completed":
        visible_rows = [row for row in statement_rows if row["is_completed"]]
    elif items_view == "incomplete":
        visible_rows = [row for row in statement_rows if not row["is_completed"]]
    else:
        visible_rows = statement_rows

    if not show_payments:
        visible_rows = [row for row in visible_rows if row.get("item_type") != "payment"]

    logger.info(
        "Statement detail rendered",
        tenant_id=tenant_id,
        statement_id=statement_id,
        visible=len(visible_rows),
        total=len(statement_rows),
        completed=completed_count,
        incomplete=incomplete_count,
        items_view=items_view,
        show_payments=show_payments,
    )

    context: dict[str, Any] = {
        **base_context,
        "is_processing": False,
        "raw_statement_headers": display_headers,
        "statement_rows": visible_rows,
        "all_statement_rows": statement_rows,
        "row_comparisons": row_comparisons,
        "completed_count": completed_count,
        "incomplete_count": incomplete_count,
        "has_payment_rows": has_payment_rows,
    }
    return render_template("statement.html", **context)


@app.route("/tenants/select", methods=["POST"])
@xero_token_required
@route_handler_logging
def select_tenant():
    """Persist the selected tenant in session and return to management view."""
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    logger.info("Tenant selection submitted", tenant_id=tenant_id, available=len(tenants))

    if tenant_id and any(t.get("tenantId") == tenant_id for t in tenants):
        # Update the active tenant and display a success message.
        _set_active_tenant(tenant_id)
        tenant_name = session.get("xero_tenant_name") or tenant_id
        session["tenant_message"] = f"Switched to tenant: {tenant_name}."
        logger.info("Tenant switched", tenant_id=tenant_id, tenant_name=tenant_name)
    else:
        session["tenant_error"] = "Unable to select tenant. Please try again."
        logger.info("Tenant selection failed", tenant_id=tenant_id)

    return redirect(url_for("tenant_management"))


@app.route("/tenants/disconnect", methods=["POST"])
@xero_token_required
@route_handler_logging
def disconnect_tenant():
    """Disconnect the tenant from Xero and update the local session state."""
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    tenant = next((t for t in tenants if t.get("tenantId") == tenant_id), None)
    management_url = url_for("tenant_management")

    if not tenant:
        session["tenant_error"] = "Tenant not found in session."
        return redirect(management_url)

    connection_id = tenant.get("connectionId")
    oauth_token = session.get("xero_oauth2_token")
    access_token = oauth_token.get("access_token") if isinstance(oauth_token, dict) else None
    logger.info("Tenant disconnect submitted", tenant_id=tenant_id, has_connection=bool(connection_id))

    if connection_id and access_token:
        try:
            resp = requests.delete(f"https://api.xero.com/connections/{connection_id}", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            if resp.status_code not in (200, 204):
                logger.error("Failed to disconnect tenant", tenant_id=tenant_id, status_code=resp.status_code, body=resp.text)
                session["tenant_error"] = "Unable to disconnect tenant from Xero."
                return redirect(management_url)
        except Exception as exc:
            logger.exception("Exception disconnecting tenant", tenant_id=tenant_id, error=exc)
            session["tenant_error"] = "An error occurred while disconnecting the tenant."
            return redirect(management_url)

    # Remove tenant locally regardless (in case it was already disconnected).
    updated = [t for t in tenants if t.get("tenantId") != tenant_id]
    session["xero_tenants"] = updated

    if session.get("xero_tenant_id") == tenant_id:
        next_tenant_id = updated[0]["tenantId"] if updated else None
        _set_active_tenant(next_tenant_id)

    session["tenant_message"] = "Tenant disconnected."
    logger.info("Tenant disconnected", tenant_id=tenant_id, remaining=len(updated))
    if not updated:
        return redirect(url_for("index"))
    return redirect(management_url)


@app.route("/configs", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant before configuring mappings.")
@xero_token_required
@route_handler_logging
@block_when_loading
def configs():
    """Render and update the contact mapping configuration UI."""
    tenant_id = session.get("xero_tenant_id")

    contacts_raw = get_contacts()
    contacts_active = [c for c in contacts_raw if str(c.get("contact_status") or "").upper() == "ACTIVE"]
    contacts_list = sorted(contacts_active, key=lambda c: (c.get("name") or "").casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    logger.info("Rendering configs", tenant_id=tenant_id, contacts=len(contacts_list))

    context: dict[str, Any] = {
        "contacts": contacts_list,
        "selected_contact_name": None,
        "selected_contact_id": None,
        "mapping_rows": [],  # {field, values:list[str], is_multi:bool}
        "message": None,
        "error": None,
        "field_descriptions": FIELD_DESCRIPTIONS,
        "date_format": "",
        "decimal_separator": DEFAULT_DECIMAL_SEPARATOR,
        "thousands_separator": DEFAULT_THOUSANDS_SEPARATOR,
        "decimal_separator_options": DECIMAL_SEPARATOR_OPTIONS,
        "thousands_separator_options": THOUSANDS_SEPARATOR_OPTIONS,
    }

    if request.method == "POST":
        action = request.form.get("action")
        if action == "load":
            # Load existing config for the chosen contact name.
            selected_contact_name = (request.form.get("contact_name") or "").strip()
            context.update(_load_config_context(tenant_id, contact_lookup, selected_contact_name))
        elif action == "save_map":
            # Save edited mapping.
            context.update(_save_config_context(tenant_id, request.form))

    context.update(
        {
            "example_rows": _build_config_rows(ContactConfig.model_validate(EXAMPLE_CONFIG)),
            "example_date_format": str(EXAMPLE_CONFIG.get("date_format") or ""),
            "example_decimal_separator": EXAMPLE_CONFIG.get("decimal_separator", DEFAULT_DECIMAL_SEPARATOR),
            "example_thousands_separator": EXAMPLE_CONFIG.get("thousands_separator", DEFAULT_THOUSANDS_SEPARATOR),
            "decimal_separator_labels": dict(DECIMAL_SEPARATOR_OPTIONS),
            "thousands_separator_labels": dict(THOUSANDS_SEPARATOR_OPTIONS),
        }
    )

    return render_template("configs.html", **context)


@app.route("/login")
@route_handler_logging
def login():
    """Start the Xero OAuth flow and redirect to the authorize URL."""
    logger.info("Login initiated")
    if not has_cookie_consent():
        logger.info("Login blocked; cookie consent missing")
        return redirect(url_for("cookies"))

    if not CLIENT_ID or not CLIENT_SECRET:
        return "Missing XERO_CLIENT_ID or XERO_CLIENT_SECRET env vars", 500

    # OIDC nonce ties the auth response to this browser session.
    nonce = secrets.token_urlsafe(24)
    session["oauth_nonce"] = nonce

    logger.info("Redirecting to Xero authorization", scope_count=len(scope_str().split()))
    # Authlib stores state/nonce in session and builds the authorize URL.
    return oauth.xero.authorize_redirect(redirect_uri=REDIRECT_URI, nonce=nonce)


@app.route("/callback")
@route_handler_logging
def callback():  # pylint: disable=too-many-return-statements
    """Handle the OAuth callback, validate tokens, and load tenant context."""
    if not has_cookie_consent():
        logger.info("OAuth callback blocked; cookie consent missing")
        return redirect(url_for("cookies"))

    # Handle user-denied or error cases
    error = request.args.get("error")
    if error is not None:
        error_description = request.args.get("error_description") or error
        logger.error("OAuth error", error_code=400, error_description=error_description, error=error)
        return f"OAuth error: {error_description}", 400

    try:
        tokens = oauth.xero.authorize_access_token()
    except OAuthError as exc:
        error_description = exc.description or exc.error
        logger.error("OAuth error", error_code=400, error_description=error_description, error=exc.error)
        return f"OAuth error: {error_description}", 400

    if not isinstance(tokens, dict):
        logger.error("Invalid token response from Xero", error_code=400)
        return "Invalid token response from Xero", 400

    # id_token is required for OIDC claim + nonce validation.
    if not tokens.get("id_token"):
        logger.error("Missing id_token in OAuth response", error_code=400)
        return "Missing id_token in OAuth response", 400

    nonce = session.pop("oauth_nonce", None)
    # Require the original nonce so we can validate the id_token against it.
    if not nonce:
        logger.error("Missing OAuth nonce in session", error_code=400)
        return "Missing OAuth nonce in session", 400

    try:
        # Validates signature + standard claims and checks nonce matches session.
        oauth.xero.parse_id_token(tokens, nonce=nonce)
    except Exception as exc:
        logger.exception("Failed to validate id_token", error=str(exc))
        return "Invalid id_token", 400

    save_xero_oauth2_token(tokens)
    access_token = tokens.get("access_token")

    conn_res = requests.get("https://api.xero.com/connections", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)

    conn_res.raise_for_status()
    connections = conn_res.json()
    if not connections:
        logger.error("No Xero connections found for this user.", error_code=400)
        return "No Xero connections found for this user.", 400

    tenants = [{"tenantId": conn.get("tenantId"), "tenantName": conn.get("tenantName"), "connectionId": conn.get("id")} for conn in connections if conn.get("tenantId")]

    current = session.get("xero_tenant_id")
    tenant_ids = [t["tenantId"] for t in tenants]

    # Store the latest tenant list before updating the active tenant.
    session["xero_tenants"] = tenants

    for tid in tenant_ids:
        _trigger_initial_sync_if_required(tid)

    if current in tenant_ids:
        _set_active_tenant(current)
    elif tenant_ids:
        first_tenant = tenant_ids[0]
        _set_active_tenant(first_tenant)
    else:
        _set_active_tenant(None)

    logger.info("OAuth callback processed", tenants=len(tenants))
    response = redirect(url_for("tenant_management"))
    return set_session_is_set_cookie(response)


@app.route("/logout")
@route_handler_logging
def logout():
    """Clear the session and return to the landing page."""
    logger.info("Logout requested", had_tenant=bool(session.get("xero_tenant_id")))
    session.clear()
    response = redirect(url_for("index"))
    return clear_session_is_set_cookie(response)


@app.route("/.well-known/<path:path>")
def chrome_devtools_ping(path):
    """Respond to Chrome DevTools well-known probes without logging 404s."""
    return "", 204  # No content, indicates "OK but nothing here"


if __name__ == "__main__":
    app.run(port=8080)
