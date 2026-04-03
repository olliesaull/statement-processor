"""Flask application for the statement processor service."""

import json
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import Any

import redis
import stripe
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from flask_session import Session
from flask_wtf.csrf import CSRFError, CSRFProtect

from billing_service import LAST_MUTATION_SOURCE_STRIPE_CHECKOUT, BillingService, BillingServiceError, InsufficientTokensError, ReservedStatementUpload
from config import CLIENT_ID, CLIENT_SECRET, DOMAIN_NAME, FLASK_SECRET_KEY, S3_BUCKET_NAME, STAGE, VALKEY_URL, tenant_statements_table
from core.config_suggestion import delete_suggestion, get_pending_suggestion_count, get_pending_suggestions, suggest_config_for_statement
from core.contact_config_metadata import EXAMPLE_CONFIG, FIELD_DESCRIPTIONS
from core.get_contact_config import get_contact_config, set_contact_config
from core.item_classification import guess_statement_item_type
from core.models import ContactConfig, StatementItem
from core.statement_detail_types import MatchByItemId, MatchedInvoiceMap, PaymentNumberMap, StatementItemPayload, StatementRowsByHeader, StatementRowViewModel, XeroDocumentPayload
from core.statement_row_palette import STATEMENT_ROW_CSS_VARIABLES
from logger import logger
from stripe_repository import StripeRepository
from stripe_service import STRIPE_MAX_TOKENS, STRIPE_MIN_TOKENS, STRIPE_PRICE_PER_TOKEN_PENCE, StripeService
from sync import check_load_required, sync_data
from tenant_billing_repository import TenantBillingRepository
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
from utils.statement_rows import format_item_type_label as _format_item_type_label
from utils.statement_rows import xero_ids_for_row as _xero_ids_for_row
from utils.statement_upload_validation import PreparedStatementUpload, build_statement_upload_preflight, prepare_statement_uploads, validate_upload_payload
from utils.statement_view import build_right_rows, build_row_comparisons, get_date_format_from_config, get_number_separators_from_config, match_invoices_to_statement_items, prepare_display_mappings
from utils.storage import StatementJSONNotFoundError, fetch_json_statement, statement_json_s3_key, statement_pdf_s3_key, upload_statement_to_s3
from utils.workflows import start_textraction_state_machine
from xero_repository import get_contacts, get_credit_notes_by_contact, get_invoices_by_contact, get_payments_by_contact

# python3.13 -m gunicorn --reload --bind 0.0.0.0:8080 app:app

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY


# Extract CSRF tokens from JSON bodies BEFORE CSRFProtect registers its
# before_request handler — Flask runs hooks in registration order, so this
# must be first.  CloudFront strips custom headers (X-CSRFToken), so
# JavaScript POSTs include the token in the JSON body instead.
@app.before_request
def _extract_csrf_from_json_body():
    """Copy CSRF token from JSON body to WSGI environ for Flask-WTF."""
    if request.is_json and not request.headers.get("X-CSRFToken"):
        data = request.get_json(silent=True)
        if isinstance(data, dict) and data.get("csrf_token"):
            request.environ["HTTP_X_CSRFTOKEN"] = data["csrf_token"]


# Enable CSRF protection globally — must be AFTER the JSON body hook above.
csrf = CSRFProtect(app)


MAX_UPLOAD_MB = os.getenv("MAX_UPLOAD_MB", "10")
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB) * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Configure Redis-backed server sessions with only required/useful options.
app.config.update(
    SESSION_TYPE="redis",
    SESSION_REDIS=redis.from_url(VALKEY_URL),
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
    SESSION_COOKIE_SECURE=STAGE != "local",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(seconds=1860),
)

Session(app)


# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

XERO_OIDC_METADATA_URL = os.getenv("XERO_OIDC_METADATA_URL", "https://identity.xero.com/.well-known/openid-configuration")

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

_executor = ThreadPoolExecutor(max_workers=5)

# Stripe service instance — used by checkout routes.
stripe_service = StripeService()


DEFAULT_DECIMAL_SEPARATOR = "."
DEFAULT_THOUSANDS_SEPARATOR = ","
DECIMAL_SEPARATOR_OPTIONS = [(".", "Dot (.)"), (",", "Comma (,)")]
THOUSANDS_SEPARATOR_OPTIONS = [("", "None"), (",", "Comma (,)"), (".", "Dot (.)"), (" ", "Space ( )"), ("'", "Apostrophe (')")]
DECIMAL_SEPARATOR_VALUES = {opt[0] for opt in DECIMAL_SEPARATOR_OPTIONS}
THOUSANDS_SEPARATOR_VALUES = {opt[0] for opt in THOUSANDS_SEPARATOR_OPTIONS}


class StatementUploadStartError(RuntimeError):
    """Raised when a reserved statement cannot be handed off to processing."""


@app.context_processor
def inject_pending_review_count():
    """Make pending config review count available to all templates.

    Caches the count in the session for 60 seconds to avoid an S3 list
    call on every page load.
    """
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        return {"pending_config_review_count": 0}

    cache_key = "_pending_review_count"
    cache_ts_key = "_pending_review_count_ts"
    now = time.time()

    # Return cached value if fresh (< 60s old).
    cached_ts = session.get(cache_ts_key, 0)
    if now - cached_ts < 60:
        return {"pending_config_review_count": session.get(cache_key, 0)}

    try:
        count = get_pending_suggestion_count(tenant_id)
    except Exception:
        count = 0

    session[cache_key] = count
    session[cache_ts_key] = now
    return {"pending_config_review_count": count}


@app.errorhandler(CSRFError)
def handle_csrf_error(error: CSRFError):
    """Log CSRF failures with request context and return API-safe JSON.

    The upload preflight and tenant sync flows both use JavaScript ``fetch``
    requests. When Flask-WTF rejects those requests, its default HTML 400
    response gives the frontend very little to work with. This handler keeps
    browser routes simple while making API failures explicit and diagnosable.
    """
    session_cookie_name = app.config.get("SESSION_COOKIE_NAME", "session")
    logger.warning(
        "CSRF validation failed",
        path=request.path,
        method=request.method,
        host=request.host,
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        content_type=request.content_type,
        content_length=request.content_length,
        csrf_error=error.description,
        csrf_header_present=bool(request.headers.get("X-CSRFToken")),
        csrf_form_token_present=bool(request.form.get("csrf_token")),
        cookie_header_present=bool(request.headers.get("Cookie")),
        session_cookie_present=session_cookie_name in request.cookies,
    )

    if request.path.startswith("/api/"):
        return jsonify({"error": "csrf_validation_failed", "message": "Security validation failed. Refresh the page and try again."}), 400

    return "Security validation failed. Refresh the page and try again.", 400


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


def _absolute_app_url(path: str) -> str:
    """Build an absolute application URL from the configured public hostname.

    This mirrors Numerint's simpler Python-side host handling: local
    development uses ``http://localhost:<port>``, while non-local stages always
    generate ``https://<DOMAIN_NAME>`` URLs.
    """
    if STAGE == "local":
        local_port = os.getenv("PORT", "8080")
        return f"http://{DOMAIN_NAME}:{local_port}{path}"
    return f"https://{DOMAIN_NAME}{path}"


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

            # Auto-confirm any pending review suggestions for this contact.
            confirmed, skipped = _auto_confirm_pending_suggestions(tenant_id, selected_contact_id)
            updates["auto_confirmed"] = confirmed
            updates["auto_skipped"] = skipped
    except Exception as exc:
        updates["error"] = f"Failed to save config: {exc}"
        logger.info("Config save failed", tenant_id=tenant_id, contact_id=selected_contact_id, error=exc)
    return updates


def _auto_confirm_pending_suggestions(tenant_id: str | None, contact_id: str) -> tuple[int, int]:
    """Auto-confirm pending config suggestions for a contact after manual save.

    Reserves tokens, deletes the S3 suggestion, clears DynamoDB pending
    status, and starts the extraction workflow for each matching statement.
    Statements are skipped if token reservation fails.

    Returns:
        Tuple of (confirmed_count, skipped_count).
    """
    suggestions = get_pending_suggestions(tenant_id)
    matching = [s for s in suggestions if s.contact_id == contact_id]

    if not matching:
        return 0, 0

    confirmed = 0
    skipped = 0

    for suggestion in matching:
        statement_id = suggestion.statement_id

        # Reserve tokens — skip this statement on billing failure.
        page_count = _get_statement_page_count(tenant_id, statement_id)
        try:
            BillingService.reserve_confirmed_statement(tenant_id, statement_id, page_count)
        except InsufficientTokensError:
            logger.info("Auto-confirm skipped; insufficient tokens", tenant_id=tenant_id, statement_id=statement_id, page_count=page_count)
            skipped += 1
            continue
        except BillingServiceError as exc:
            logger.exception("Auto-confirm skipped; billing error", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
            skipped += 1
            continue

        # Clean up suggestion and clear pending status.
        delete_suggestion(tenant_id, statement_id)
        tenant_statements_table.update_item(
            Key={"TenantID": tenant_id, "StatementID": statement_id},
            UpdateExpression="REMOVE #s",
            ExpressionAttributeNames={"#s": "Status"},
        )

        # Start extraction workflow.
        pdf_key = statement_pdf_s3_key(tenant_id, statement_id)
        json_key = statement_json_s3_key(tenant_id, statement_id)
        start_textraction_state_machine(tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_key, json_key=json_key)

        confirmed += 1
        logger.info("Auto-confirmed pending suggestion", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)

    return confirmed, skipped


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


@app.route("/healthz")
def healthz():
    """Return a minimal unauthenticated liveness response for App Runner."""
    return "", 200


@app.route("/tenant_management")
@route_handler_logging
@xero_token_required
def tenant_management():
    """Render tenant management, consuming one-time messages from session."""
    tenants = session.get("xero_tenants") or []
    current_tenant_id = session.get("xero_tenant_id")
    current_tenant = None
    tenant_ids: list[str] = []
    for tenant in tenants:
        if not isinstance(tenant, dict):
            continue
        tenant_id = tenant.get("tenantId")
        if not tenant_id:
            continue
        tenant_ids.append(tenant_id)
        if tenant_id == current_tenant_id:
            current_tenant = tenant
    # Messages are popped so they only display once.
    message = session.pop("tenant_message", None)
    error = session.pop("tenant_error", None)

    tenant_token_balances: dict[str, int] = {}
    try:
        tenant_token_balances = TenantBillingRepository.get_tenant_token_balances(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant token balances", tenant_ids=tenant_ids, error=exc)

    ct_token_balance = tenant_token_balances.get(current_tenant_id, 0) if current_tenant_id else 0
    logger.info("Rendering tenant_management page", current_tenant_id=current_tenant_id, tenant_ids=tenant_ids, current_tenant_token_balance=ct_token_balance)

    return render_template(
        "tenant_management.html", tenants=tenants, current_tenant=current_tenant, ct_token_balance=ct_token_balance, tenant_token_balances=tenant_token_balances, message=message, error=error
    )


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


def _process_statement_upload(tenant_id: str | None, reserved_upload: ReservedStatementUpload) -> str:
    """Upload a reserved statement PDF and kick off textraction.

    Args:
        tenant_id: Active Xero tenant.
        reserved_upload: Upload row that already has a statement id and token reservation.

    Returns:
        The statement id linked to the upload.

    Raises:
        StatementUploadStartError: S3 upload or workflow startup failed after reservation.
    """
    file_bytes = getattr(reserved_upload.uploaded_file, "content_length", None)
    statement_id = reserved_upload.statement_id
    logger.info(
        "Preparing statement upload",
        tenant_id=tenant_id,
        contact_id=reserved_upload.contact_id,
        contact_name=reserved_upload.contact_name,
        statement_id=statement_id,
        statement_filename=reserved_upload.uploaded_file.filename,
        bytes=file_bytes,
    )

    # Upload PDF to S3 first so downstream processing can read it.
    pdf_statement_key = statement_pdf_s3_key(tenant_id, statement_id)
    try:
        upload_statement_to_s3(fs_like=reserved_upload.uploaded_file, key=pdf_statement_key)
        logger.info("Uploaded statement PDF", tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, s3_key=pdf_statement_key)
    except Exception as exc:
        logger.exception("Failed to upload reserved statement PDF", tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, s3_key=pdf_statement_key, error=exc)
        raise StatementUploadStartError("The statement PDF could not be uploaded.") from exc

    # Kick off background textraction so it's ready by the time the user views it.
    json_statement_key = statement_json_s3_key(tenant_id, statement_id)
    started = start_textraction_state_machine(tenant_id=tenant_id, contact_id=reserved_upload.contact_id, statement_id=statement_id, pdf_key=pdf_statement_key, json_key=json_statement_key)

    log_kwargs = {"tenant_id": tenant_id, "contact_id": reserved_upload.contact_id, "statement_id": statement_id, "pdf_key": pdf_statement_key, "json_key": json_statement_key}

    if started:
        logger.info("Started textraction workflow", **log_kwargs)
    else:
        logger.error("Failed to start textraction workflow", **log_kwargs)
        raise StatementUploadStartError("The statement workflow could not be started.")

    return statement_id


def _handle_reserved_upload_failure(tenant_id: str | None, reserved_upload: ReservedStatementUpload, exc: Exception, error_messages: list[str]) -> None:
    """Release tokens and clean up statement data after upload-start failure."""
    logger.exception("Upload failed after token reservation; releasing tokens", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, contact_id=reserved_upload.contact_id, error=exc)

    release_succeeded = False
    try:
        release_succeeded = BillingService.release_statement_reservation(tenant_id, reserved_upload.statement_id)
    except BillingServiceError as release_exc:
        logger.exception("Failed to release reserved tokens after upload-start failure", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, error=release_exc)

    filename = reserved_upload.uploaded_file.filename or "Unnamed PDF"
    if not release_succeeded:
        error_messages.append(f"{filename}: The upload was not started and token recovery needs operator attention.")
        return

    try:
        delete_statement_data(tenant_id, reserved_upload.statement_id)
    except Exception as cleanup_exc:
        logger.exception("Failed to clean up statement after upload-start failure", tenant_id=tenant_id, statement_id=reserved_upload.statement_id, error=cleanup_exc)

    error_messages.append(f"{filename}: The upload was not started. Any reserved tokens were returned.")


def _reserve_statement_uploads(tenant_id: str | None, prepared_uploads: list[PreparedStatementUpload], error_messages: list[str]) -> list[ReservedStatementUpload]:
    """Reserve tokens for a validated batch and collect user-facing errors."""
    try:
        return BillingService.reserve_statement_uploads(tenant_id, prepared_uploads)
    except InsufficientTokensError:
        logger.info("Upload blocked; token reservation failed due to insufficient balance", tenant_id=tenant_id, files=len(prepared_uploads))
        error_messages.append("The tenant no longer has enough available tokens for this upload. Refresh the page, remove some PDFs, or buy more tokens before trying again.")
    except BillingServiceError as exc:
        logger.exception("Upload blocked; token reservation failed", tenant_id=tenant_id, files=len(prepared_uploads), error=exc)
        error_messages.append("Could not reserve tokens for this upload. Please try again.")
    return []


def _handle_upload_statements_post(tenant_id: str | None, *, contact_lookup: dict[str, str], error_messages: list[str]) -> tuple[int, int]:
    """Validate, reserve, and start workflow processing for one upload POST.

    Returns:
        Tuple of (success_count, review_count) — how many uploads started
        processing and how many were submitted for config review.
    """
    files = [f for f in request.files.getlist("statements") if f and f.filename]
    names = request.form.getlist("contact_names")
    logger.info("Upload statements submitted", tenant_id=tenant_id, files=len(files), names=len(names))

    if not validate_upload_payload(files, names):
        return 0, 0

    prepared_uploads = prepare_statement_uploads(tenant_id, files, names, contact_lookup, error_messages)
    if not prepared_uploads:
        return 0, 0

    # Split uploads into those with config (ready) and those needing review.
    ready_uploads = [u for u in prepared_uploads if not u.needs_config_review]
    review_uploads = [u for u in prepared_uploads if u.needs_config_review]

    # Process ready uploads as before (reserve, upload, start step function).
    uploads_ok = 0
    if ready_uploads:
        reserved_uploads = _reserve_statement_uploads(tenant_id, ready_uploads, error_messages)
        for reserved_upload in reserved_uploads:
            try:
                _process_statement_upload(tenant_id=tenant_id, reserved_upload=reserved_upload)
                uploads_ok += 1
            except StatementUploadStartError as exc:
                _handle_reserved_upload_failure(tenant_id, reserved_upload, exc, error_messages)

    # Submit review uploads — no token reservation until user confirms config.
    review_count = 0
    for upload in review_uploads:
        try:
            statement_id = uuid.uuid4().hex
            _create_review_statement_header(tenant_id, statement_id, upload)

            pdf_key = statement_pdf_s3_key(tenant_id, statement_id)
            upload_statement_to_s3(fs_like=upload.uploaded_file, key=pdf_key)

            _executor.submit(
                suggest_config_for_statement,
                tenant_id=tenant_id,
                contact_id=upload.contact_id,
                contact_name=upload.contact_name,
                statement_id=statement_id,
                pdf_s3_key=pdf_key,
                filename=upload.uploaded_file.filename or "statement.pdf",
                page_count=upload.page_count,
            )
            review_count += 1
            logger.info("Submitted config suggestion job", tenant_id=tenant_id, statement_id=statement_id, contact_name=upload.contact_name)
        except Exception as exc:
            logger.exception("Failed to submit review upload", tenant_id=tenant_id, contact_name=upload.contact_name, error=exc)
            error_messages.append(f"{upload.uploaded_file.filename or 'PDF'}: Failed to upload for config review.")

    return uploads_ok, review_count


def _create_review_statement_header(tenant_id: str | None, statement_id: str, upload: PreparedStatementUpload) -> None:
    """Create a DynamoDB header row for a statement awaiting config review.

    These rows have Status=pending_config_review and no billing reservation
    fields — tokens are reserved later when the user confirms the config.
    """
    tenant_statements_table.put_item(
        Item={
            "TenantID": tenant_id,
            "StatementID": statement_id,
            "OriginalStatementFilename": upload.uploaded_file.filename or "Unnamed PDF",
            "ContactID": upload.contact_id,
            "ContactName": upload.contact_name,
            "UploadedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "Completed": "false",
            "RecordType": "statement",
            "PdfPageCount": upload.page_count,
            "Status": "pending_config_review",
        }
    )


@app.route("/api/upload-statements/preflight", methods=["POST"])
@xero_token_required
@route_handler_logging
def upload_statements_preflight():
    """Count uploaded PDF pages on the server before the real upload is submitted."""
    tenant_id = (session.get("xero_tenant_id") or "").strip()
    if not tenant_id:
        logger.info("Upload preflight rejected; tenant missing")
        return jsonify({"error": "TenantID is required"}), 400

    files = [uploaded_file for uploaded_file in request.files.getlist("statements") if uploaded_file and uploaded_file.filename]
    if not files:
        logger.info("Upload preflight rejected; no files supplied", tenant_id=tenant_id)
        return jsonify({"error": "At least one statement PDF is required"}), 400

    preflight_result = build_statement_upload_preflight(tenant_id, files)
    logger.info(
        "Upload preflight evaluated",
        tenant_id=tenant_id,
        files=len(preflight_result.files),
        total_pages=preflight_result.total_pages,
        available_tokens=preflight_result.available_tokens,
        sufficient=preflight_result.is_sufficient,
        can_submit=preflight_result.can_submit,
        shortfall=preflight_result.shortfall,
    )
    payload = preflight_result.to_response_payload()
    # When the user can't afford the upload, surface a direct link to the token
    # purchase page. Injected here (not in to_response_payload) to keep the
    # validation model free of Flask URL-routing knowledge.
    if preflight_result.shortfall > 0:
        payload["buy_tokens_url"] = url_for("buy_tokens")
    return jsonify(payload), 200


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
    review_count = 0
    if request.method == "POST":
        uploads_ok, review_count = _handle_upload_statements_post(tenant_id, contact_lookup=contact_lookup, error_messages=error_messages)

        if uploads_ok:
            success_count = uploads_ok
        if review_count:
            # Invalidate the cached pending review count so the banner updates.
            session.pop("_pending_review_count_ts", None)
            error_messages.append(f"{review_count} statement{'s' if review_count != 1 else ''} need config review — go to Configuration to confirm.")
        logger.info("Upload statements processed", tenant_id=tenant_id, succeeded=uploads_ok, review=review_count, errors=list(error_messages))

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
        statement_rows.sort(key=lambda r: r.get("_uploaded_at") or datetime.min.replace(tzinfo=UTC), reverse=reverse)
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
    record = get_statement_record(tenant_id, statement_id)
    if record and str(record.get("TokenReservationStatus") or "").strip().lower() == "reserved":
        logger.info("Delete rejected; statement still processing", tenant_id=tenant_id, statement_id=statement_id)
        session["tenant_error"] = "This statement is still processing and cannot be deleted yet."
        return redirect(url_for("statements"))

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
    from utils.statement_excel_export import build_statement_excel_payload  # pylint: disable=import-outside-toplevel

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

    # Redirect to /configs if the statement hasn't been configured yet
    # (viewing the detail page would fail due to missing JSON).
    record_status = str(record.get("Status") or "")
    if record_status in ("pending_config_review", "config_suggestion_failed"):
        contact_name_redirect = str(record.get("ContactName") or "").strip()
        return redirect(url_for("configs", contact_name=contact_name_redirect))

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
        reservation_status = str(record.get("TokenReservationStatus") or "").strip().lower()
        if reservation_status == "released":
            logger.info("Statement processing failed; JSON missing after release", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
            return render_template(
                "statement.html",
                is_processing=False,
                processing_failed=True,
                incomplete_count=0,
                completed_count=0,
                all_statement_rows=[],
                statement_rows=[],
                raw_statement_headers=[],
                has_payment_rows=False,
                **base_context,
            )
        logger.info("Statement JSON pending", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
        return render_template(
            "statement.html",
            is_processing=True,
            processing_failed=False,
            incomplete_count=0,
            completed_count=0,
            all_statement_rows=[],
            statement_rows=[],
            raw_statement_headers=[],
            has_payment_rows=False,
            **base_context,
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
        "processing_failed": False,
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
        import requests  # pylint: disable=import-outside-toplevel

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
            save_result = _save_config_context(tenant_id, request.form)
            auto_confirmed = save_result.pop("auto_confirmed", 0)
            auto_skipped = save_result.pop("auto_skipped", 0)
            context.update(save_result)

            # If pending suggestions were auto-confirmed, update the message
            # and invalidate the cached review count.
            if auto_confirmed > 0:
                session.pop("_pending_review_count_ts", None)
                parts = [context.get("message") or ""]
                parts.append(f"{auto_confirmed} pending statement(s) auto-confirmed and queued for extraction.")
                if auto_skipped > 0:
                    parts.append(f"{auto_skipped} statement(s) skipped due to insufficient tokens.")
                context["message"] = " ".join(p for p in parts if p)
    elif request.args.get("contact_name"):
        # Support ?contact_name= query param for pre-selection (e.g. redirected from failed statement).
        selected_contact_name = request.args["contact_name"].strip()
        context.update(_load_config_context(tenant_id, contact_lookup, selected_contact_name))

    # Load pending config suggestions for the review section.
    # Merge detected headers with LLM-suggested values so autocomplete
    # suggestions include column names the LLM identified from data rows
    # (not just the Textract first row which may be a title rather than
    # real headers).
    pending_suggestions = get_pending_suggestions(tenant_id)
    suggestions_dicts: list[dict[str, Any]] = []
    for s in pending_suggestions:
        d = s.model_dump()
        seen = set(d["detected_headers"])
        extra: list[str] = []
        cfg = d.get("suggested_config", {})
        for field in ("number", "date", "due_date"):
            val = cfg.get(field, "")
            if val and val not in seen:
                extra.append(val)
                seen.add(val)
        for val in cfg.get("total", []):
            if val and val not in seen:
                extra.append(val)
                seen.add(val)
        d["all_headers"] = [h for h in d["detected_headers"] if h] + extra
        suggestions_dicts.append(d)
    context["pending_suggestions"] = suggestions_dicts

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


@app.route("/api/configs/confirm", methods=["POST"])
@active_tenant_required("Please select a tenant.")
@xero_token_required
@route_handler_logging
def confirm_config_suggestion():
    """Confirm an LLM-suggested config and kick off full extraction."""
    tenant_id = session.get("xero_tenant_id")
    data = request.get_json()

    contact_id = data.get("contact_id", "")
    statement_id = data.get("statement_id", "")
    config_payload = data.get("config", {})

    # Validate mandatory fields (server-side source of truth).
    errors = _validate_config_mandatory_fields(config_payload)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    # Reserve tokens now — the review-upload path deferred reservation until
    # the user confirmed the config (design spec: "tokens are reserved later
    # when the user confirms the config and the full Step Function kicks off").
    page_count = _get_statement_page_count(tenant_id, statement_id)
    try:
        BillingService.reserve_confirmed_statement(tenant_id, statement_id, page_count)
    except InsufficientTokensError:
        logger.info("Config confirm blocked; insufficient tokens", tenant_id=tenant_id, statement_id=statement_id, page_count=page_count)
        return jsonify({"ok": False, "errors": ["Not enough tokens to process this statement. Please purchase more tokens."]}), 400
    except BillingServiceError as exc:
        logger.exception("Config confirm blocked; billing error", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
        return jsonify({"ok": False, "errors": ["Could not reserve tokens. Please try again."]}), 500

    # Save config to DynamoDB.
    config = ContactConfig.model_validate(config_payload)
    set_contact_config(tenant_id, contact_id, config)

    # Clean up the S3 suggestion file now that it's been confirmed.
    delete_suggestion(tenant_id, statement_id)

    # Clear the pending status so the statement no longer shows review badges.
    tenant_statements_table.update_item(Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression="REMOVE #s", ExpressionAttributeNames={"#s": "Status"})

    # Start the extraction workflow — the PDF is already in S3 from upload.
    pdf_key = statement_pdf_s3_key(tenant_id, statement_id)
    json_key = statement_json_s3_key(tenant_id, statement_id)
    start_textraction_state_machine(tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_key, json_key=json_key)

    # Invalidate cached pending review count.
    session.pop("_pending_review_count_ts", None)

    logger.info("Config suggestion confirmed", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id)
    return jsonify({"ok": True, "statement_id": statement_id})


@app.route("/api/configs/confirm-all", methods=["POST"])
@active_tenant_required("Please select a tenant.")
@xero_token_required
@route_handler_logging
def confirm_all_config_suggestions():
    """Confirm multiple suggested configs. Skips invalid ones."""
    tenant_id = session.get("xero_tenant_id")
    items = request.get_json().get("items", [])

    confirmed: list[str] = []
    skipped: list[dict[str, Any]] = []

    for item in items:
        config_payload = item.get("config", {})
        errors = _validate_config_mandatory_fields(config_payload)
        if errors:
            skipped.append({"statement_id": item.get("statement_id"), "errors": errors})
            continue

        contact_id = item.get("contact_id", "")
        statement_id = item.get("statement_id", "")

        # Reserve tokens for this statement before processing.
        page_count = _get_statement_page_count(tenant_id, statement_id)
        try:
            BillingService.reserve_confirmed_statement(tenant_id, statement_id, page_count)
        except InsufficientTokensError:
            skipped.append({"statement_id": statement_id, "errors": ["Not enough tokens to process this statement."]})
            continue
        except BillingServiceError as exc:
            logger.exception("Bulk confirm billing error", tenant_id=tenant_id, statement_id=statement_id, error=str(exc))
            skipped.append({"statement_id": statement_id, "errors": ["Could not reserve tokens. Please try again."]})
            continue

        config = ContactConfig.model_validate(config_payload)
        set_contact_config(tenant_id, contact_id, config)
        delete_suggestion(tenant_id, statement_id)

        # Clear pending status before starting the workflow.
        tenant_statements_table.update_item(Key={"TenantID": tenant_id, "StatementID": statement_id}, UpdateExpression="REMOVE #s", ExpressionAttributeNames={"#s": "Status"})

        pdf_key = statement_pdf_s3_key(tenant_id, statement_id)
        json_key = statement_json_s3_key(tenant_id, statement_id)
        start_textraction_state_machine(tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_key, json_key=json_key)

        confirmed.append(statement_id)

    # Invalidate cached pending review count.
    session.pop("_pending_review_count_ts", None)

    logger.info("Bulk config confirm", tenant_id=tenant_id, confirmed=len(confirmed), skipped=len(skipped))
    return jsonify({"confirmed": confirmed, "skipped": skipped})


def _get_statement_page_count(tenant_id: str | None, statement_id: str) -> int:
    """Fetch PdfPageCount from the statement header row in DynamoDB."""
    resp = tenant_statements_table.get_item(Key={"TenantID": tenant_id, "StatementID": statement_id}, ProjectionExpression="PdfPageCount")
    item = resp.get("Item", {})
    raw = item.get("PdfPageCount", 0)
    return int(raw) if raw else 0


def _validate_config_mandatory_fields(config: dict) -> list[str]:
    """Validate mandatory config fields, return list of error messages."""
    errors: list[str] = []
    if not config.get("number"):
        errors.append("'number' (document number column) is required.")
    if not config.get("date"):
        errors.append("'date' (transaction date column) is required.")
    if not config.get("total") or not any(config["total"]):
        errors.append("At least one 'total' column is required.")
    if not config.get("date_format"):
        errors.append("'date_format' is required.")
    return errors


@app.route("/login")
@route_handler_logging
def login():
    """Start the Xero OAuth flow and redirect to the authorize URL."""
    logger.info("Login initiated")
    if not has_cookie_consent():
        logger.info("Login blocked; cookie consent missing")
        return redirect(url_for("cookies"))

    # OIDC nonce ties the auth response to this browser session.
    nonce = secrets.token_urlsafe(24)
    session["oauth_nonce"] = nonce

    callback_url = _absolute_app_url(url_for("callback"))
    logger.info("Redirecting to Xero authorization", scope_count=len(scope_str().split()))
    # Authlib stores state/nonce in session and builds the authorize URL.
    # Building the callback from DOMAIN_NAME keeps the OAuth flow aligned with
    # the canonical public host without adding Flask-side host redirects.
    return oauth.xero.authorize_redirect(redirect_uri=callback_url, nonce=nonce)


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
        # Capture claims so we can extract the user email for Stripe customer creation.
        claims = oauth.xero.parse_id_token(tokens, nonce=nonce)
    except Exception as exc:
        logger.exception("Failed to validate id_token", error=str(exc))
        return "Invalid id_token", 400

    # Store the authenticated user's email for Stripe Customer creation —
    # Authlib has already validated the token above so claims are trustworthy.
    if claims is not None:
        session["xero_user_email"] = claims.get("email", "")

    save_xero_oauth2_token(tokens)
    access_token = tokens.get("access_token")

    import requests  # pylint: disable=import-outside-toplevel

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


@app.route("/pricing")
@route_handler_logging
def pricing():
    """Render the public-facing pricing explanation page (no login required).

    Intentionally has no ``@xero_token_required`` so prospective customers
    can see pricing before signing up.
    """
    return render_template("pricing.html")


@app.route("/buy-tokens")
@xero_token_required
@route_handler_logging
def buy_tokens():
    """Render the token purchase form with current balance and pricing info."""
    tenant_id = session.get("xero_tenant_id")
    token_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    return render_template("buy_tokens.html", token_balance=token_balance, min_tokens=STRIPE_MIN_TOKENS, max_tokens=STRIPE_MAX_TOKENS, price_pence=STRIPE_PRICE_PER_TOKEN_PENCE, error=None)


@app.route("/buy-tokens", methods=["POST"])
@xero_token_required
@route_handler_logging
def buy_tokens_post():
    """Validate token count, store in session, and redirect to billing details.

    Validates the submitted token count against configured min/max limits.
    On success the count is stored in ``session["pending_token_count"]`` for
    the billing details step to consume; the user is then redirected to
    ``/billing-details`` to enter invoice/billing information before Stripe
    checkout is created.
    """
    tenant_id = session.get("xero_tenant_id")
    token_count_raw = request.form.get("token_count", "").strip()

    # Validate input — re-render form on error (this is a form POST, not AJAX;
    # JSON responses would render as raw text in the browser).
    try:
        token_count = int(token_count_raw)
    except (ValueError, TypeError):
        token_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
        return (
            render_template(
                "buy_tokens.html",
                token_balance=token_balance,
                error="Please enter a valid number of tokens.",
                min_tokens=STRIPE_MIN_TOKENS,
                max_tokens=STRIPE_MAX_TOKENS,
                price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
            ),
            400,
        )

    if not STRIPE_MIN_TOKENS <= token_count <= STRIPE_MAX_TOKENS:
        token_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
        return (
            render_template(
                "buy_tokens.html",
                token_balance=token_balance,
                error=f"Token count must be between {STRIPE_MIN_TOKENS} and {STRIPE_MAX_TOKENS}.",
                min_tokens=STRIPE_MIN_TOKENS,
                max_tokens=STRIPE_MAX_TOKENS,
                price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
            ),
            400,
        )

    # Store validated token count in session for the billing details step.
    # Consumed (popped) by POST /api/checkout/create after a successful Stripe
    # session is created so that retrying the billing form keeps the count.
    session["pending_token_count"] = token_count
    return redirect(url_for("billing_details"))


@app.route("/billing-details")
@xero_token_required
@route_handler_logging
def billing_details():
    """Render the billing details form for a token purchase.

    Requires ``session["pending_token_count"]`` to be set by
    ``POST /buy-tokens``; redirects back to ``/buy-tokens`` if absent so the
    user cannot land here directly without selecting a token count first.

    Name and email are pre-filled from the Xero session only. Address fields
    are always blank — each purchase creates a fresh Stripe Customer, so there
    is no persistent customer record to pre-fill address data from.
    """
    if not session.get("pending_token_count"):
        # User landed here without going through the token count step.
        return redirect(url_for("buy_tokens"))
    return render_template(
        "billing_details.html",
        token_count=session["pending_token_count"],
        price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
        default_email=session.get("xero_user_email", ""),
        default_name=session.get("xero_tenant_name", ""),
    )


@app.route("/api/checkout/create", methods=["POST"])
@xero_token_required
@route_handler_logging
def checkout_create():
    """Accept billing details, create a fresh Stripe Customer, and create a Checkout Session.

    Reads the token count from ``session["pending_token_count"]`` (set by
    ``POST /buy-tokens``). Validates the required billing fields submitted via
    the billing details form. On validation failure the billing form is
    re-rendered with an error message and the session key is preserved so the
    user can correct and resubmit without losing their token count selection.

    On success:

    1. A fresh Stripe Customer is created with the user-provided billing details.
       A new customer per checkout means each invoice is attached to a customer
       whose name, email, and address exactly match what was entered — no previous
       purchase's customer record is ever overwritten.
    2. A Stripe Checkout Session is created and the browser is redirected to
       the hosted payment page.
    3. ``session["pending_token_count"]`` is consumed only after the Stripe
       session is successfully created.
    """
    tenant_id = session.get("xero_tenant_id")

    # Guard: token count must have been set by POST /buy-tokens. Redirect back
    # if the user navigated here directly (e.g. via back button after a prior
    # successful checkout that already consumed the session key).
    token_count = session.get("pending_token_count")
    if not token_count:
        return redirect(url_for("buy_tokens"))

    # Read billing fields from the billing details form.
    billing_name = request.form.get("billing_name", "").strip()
    billing_email = request.form.get("billing_email", "").strip()
    billing_line1 = request.form.get("billing_line1", "").strip()
    billing_line2 = request.form.get("billing_line2", "").strip()
    billing_city = request.form.get("billing_city", "").strip()
    billing_state = request.form.get("billing_state", "").strip()
    billing_postal_code = request.form.get("billing_postal_code", "").strip()
    billing_country = request.form.get("billing_country", "").strip()

    # Validate required fields — re-render billing form on failure.
    # session["pending_token_count"] is intentionally NOT popped here so the
    # user can correct errors and resubmit without restarting from /buy-tokens.
    missing = []
    if not billing_name:
        missing.append("Name")
    if not billing_email:
        missing.append("Email")
    if not billing_line1:
        missing.append("Address line 1")
    if not billing_postal_code:
        missing.append("Postal code")
    if not billing_country:
        missing.append("Country")

    if missing:
        return (
            render_template(
                "billing_details.html",
                token_count=token_count,
                price_pence=STRIPE_PRICE_PER_TOKEN_PENCE,
                # Re-fill the form with what the user typed so they only need to
                # correct the missing fields rather than re-enter everything.
                saved=request.form,
                default_email=session.get("xero_user_email", ""),
                default_name=session.get("xero_tenant_name", ""),
                error=f"The following fields are required: {', '.join(missing)}.",
            ),
            400,
        )

    # Build URLs — {CHECKOUT_SESSION_ID} is a Stripe template literal substituted
    # by Stripe before redirecting the browser to the success page.
    success_url = url_for("checkout_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = url_for("checkout_cancel", _external=True)

    try:
        # Create a fresh Stripe Customer for this purchase with the user-provided billing
        # details. A new customer per checkout means each invoice is attached to a customer
        # whose name, email, and address exactly match what was entered — no previous
        # purchase's customer record is ever overwritten.
        customer_id = stripe_service.create_customer(
            name=billing_name,
            email=billing_email,
            address={"line1": billing_line1, "line2": billing_line2, "city": billing_city, "state": billing_state, "postal_code": billing_postal_code, "country": billing_country},
            tenant_id=tenant_id,
        )
        stripe_session = stripe_service.create_checkout_session(customer_id=customer_id, token_count=token_count, tenant_id=tenant_id, success_url=success_url, cancel_url=cancel_url)
    except stripe.StripeError:
        logger.exception("Failed to create Stripe checkout session", tenant_id=tenant_id)
        ref = secrets.token_hex(8)
        return redirect(url_for("checkout_failed", ref=ref))

    # Consume the pending token count only after a successful Stripe session
    # creation so that a Stripe API failure leaves the session key intact and
    # the user can retry from /billing-details without re-selecting token count.
    session.pop("pending_token_count", None)

    return redirect(stripe_session.url, code=303)


@app.route("/checkout/success")
@xero_token_required
@route_handler_logging
def checkout_success():
    """Verify payment, credit tokens idempotently, and show confirmation.

    Retrieves the Stripe session to verify ``payment_status == "paid"`` and
    confirm the session belongs to the authenticated tenant before crediting
    tokens. Idempotency is enforced via ``StripeEventStoreTable`` so a page
    refresh shows the success screen without re-crediting.
    """
    tenant_id = session.get("xero_tenant_id")
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return redirect(url_for("checkout_failed"))

    # Idempotency check — already processed? Show success without re-crediting.
    if StripeRepository.is_session_processed(session_id):
        record = StripeRepository.get_processed_session(session_id)
        if record:
            new_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
            return render_template("checkout_success.html", tokens_credited=int(record["TokensCredited"]), new_balance=new_balance)
        # record is None: tiny race window between is_session_processed and
        # get_processed_session — fall through to normal processing path.

    # Retrieve session from Stripe and verify payment status.
    try:
        stripe_session = stripe_service.retrieve_session(session_id)
    except stripe.StripeError:
        logger.exception("Failed to retrieve Stripe session", session_id=session_id)
        return redirect(url_for("checkout_failed"))

    if stripe_session.payment_status != "paid":
        logger.info("Stripe session not paid", session_id=session_id, payment_status=stripe_session.payment_status)
        return redirect(url_for("checkout_failed"))

    # Security: verify the session belongs to the authenticated tenant.
    # Prevents a user who obtains another tenant's session_id from crediting
    # the wrong account.
    session_tenant_id = stripe_session.metadata.get("tenant_id")
    if session_tenant_id != tenant_id:
        logger.warning("Session tenant_id mismatch", session_id=session_id, session_tenant_id=session_tenant_id, auth_tenant_id=tenant_id)
        return redirect(url_for("checkout_failed"))

    token_count = int(stripe_session.metadata["token_count"])

    # Credit tokens. ledger_entry_id ties this ledger row to the Stripe session
    # in StripeEventStoreTable, enabling audit cross-reference and making the
    # ledger write conditionally idempotent via attribute_not_exists.
    ledger_entry_id = f"purchase#{session_id}"
    BillingService.adjust_token_balance(tenant_id, token_count, source=LAST_MUTATION_SOURCE_STRIPE_CHECKOUT, ledger_entry_id=ledger_entry_id)

    # Mark session as processed so page refreshes don't re-credit.
    StripeRepository.record_processed_session(session_id=session_id, tenant_id=tenant_id, tokens_credited=token_count, ledger_entry_id=ledger_entry_id)

    new_balance = TenantBillingRepository.get_tenant_token_balance(tenant_id)
    return render_template("checkout_success.html", tokens_credited=token_count, new_balance=new_balance)


@app.route("/checkout/cancel")
@xero_token_required
@route_handler_logging
def checkout_cancel():
    """Render the checkout cancellation page.

    Stripe redirects here when the user clicks "Back" on the hosted checkout
    page. No tokens are credited and no Stripe session is stored.
    """
    return render_template("checkout_cancel.html")


@app.route("/checkout/failed")
@xero_token_required
@route_handler_logging
def checkout_failed():
    """Render the checkout failure page with an optional reference ID.

    Shown when Stripe session creation fails or when the success route
    detects an unexpected payment state. The ``ref`` query param is a hex
    string generated at the point of failure to help correlate log entries.
    """
    ref = request.args.get("ref", "")
    return render_template("checkout_failed.html", ref=ref)


@app.route("/.well-known/<path:path>")
def chrome_devtools_ping(path):
    """Respond to Chrome DevTools well-known probes without logging 404s."""
    return "", 204  # No content, indicates "OK but nothing here"


if STAGE == "local":

    @app.route("/test-login")
    def test_login():
        """Seed the Flask session with fake auth for local browser testing.

        Only exists when STAGE=local. Bypasses Xero OAuth so Claude Code
        (or a developer) can browse authenticated pages without credentials.

        Requires PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars
        pointing to a previously-synced tenant.
        """
        tenant_id = os.environ.get("PLAYWRIGHT_TENANT_ID")
        tenant_name = os.environ.get("PLAYWRIGHT_TENANT_NAME")

        if not tenant_id or not tenant_name:
            return "Set PLAYWRIGHT_TENANT_ID and PLAYWRIGHT_TENANT_NAME env vars", 400

        session["xero_oauth2_token"] = {"access_token": "test-token-local", "token_type": "Bearer", "expires_in": 86400, "expires_at": time.time() + 86400}
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_name
        session["xero_tenants"] = [{"tenantId": tenant_id, "tenantName": tenant_name}]
        session["xero_user_email"] = "claude@local-test.dev"
        logger.info("Test login session seeded", tenant_id=tenant_id)

        response = redirect(url_for("tenant_management"))
        response.set_cookie("cookie_consent", "true", max_age=86400, path="/")
        response.set_cookie("session_is_set", "true", max_age=86400, path="/")
        return response
