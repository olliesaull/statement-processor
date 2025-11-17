import json
import os
import secrets
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Set

import requests
from botocore.exceptions import ClientError
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_caching import Cache
from flask_session import Session
from openpyxl import Workbook
from werkzeug.utils import secure_filename

import cache_provider
from config import (
    CLIENT_ID,
    CLIENT_SECRET,
    S3_BUCKET_NAME,
    STAGE,
    logger,
    tenant_statements_table,
)
from core.contact_config_metadata import EXAMPLE_CONFIG, FIELD_DESCRIPTIONS
from core.get_contact_config import get_contact_config, set_contact_config
from core.item_classification import guess_statement_item_type
from core.models import StatementItem
from sync import check_load_required, sync_data
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils import (
    StatementJSONNotFoundError,
    active_tenant_required,
    add_statement_to_table,
    block_when_loading,
    build_right_rows,
    build_row_comparisons,
    delete_statement_data,
    enforce_csrf_protection,
    fetch_json_statement,
    get_completed_statements,
    get_csrf_token,
    get_date_format_from_config,
    get_incomplete_statements,
    get_number_separators_from_config,
    get_statement_item_status_map,
    get_statement_record,
    is_allowed_pdf,
    mark_statement_completed,
    match_invoices_to_statement_items,
    prepare_display_mappings,
    route_handler_logging,
    save_xero_oauth2_token,
    scope_str,
    set_all_statement_items_completed,
    set_statement_item_completed,
    statement_json_s3_key,
    statement_pdf_s3_key,
    textract_in_background,
    upload_statement_to_s3,
    xero_token_required,
)
from xero_repository import (
    get_contacts,
    get_credit_notes_by_contact,
    get_invoices_by_contact,
    get_payments_by_contact,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(16))

os.makedirs(app.instance_path, exist_ok=True)
session_dir = os.path.join(app.instance_path, "flask_session")
os.makedirs(session_dir, exist_ok=True)
app.config.update(
    SESSION_TYPE="filesystem",
    SESSION_FILE_DIR=session_dir,
    SESSION_PERMANENT=False,
    SESSION_USE_SIGNER=True,
)
Session(app)

cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 0})
cache_provider.set_cache(cache)


@app.before_request
def _apply_csrf_protection() -> None:
    enforce_csrf_protection()


@app.context_processor
def _inject_csrf_token() -> Dict[str, Any]:
    return {"csrf_token": get_csrf_token}

# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "https://cloudcathode.com/callback" if STAGE == "prod" else "http://localhost:8080/callback"

_executor = ThreadPoolExecutor(max_workers=2)


DEFAULT_DECIMAL_SEPARATOR = "."
DEFAULT_THOUSANDS_SEPARATOR = ","
DECIMAL_SEPARATOR_OPTIONS = [
    (".", "Dot (.)"),
    (",", "Comma (,)"),
]
THOUSANDS_SEPARATOR_OPTIONS = [
    ("", "None"),
    (",", "Comma (,)"),
    (".", "Dot (.)"),
    (" ", "Space ( )"),
    ("'", "Apostrophe (')"),
]


def _trigger_initial_sync_if_required(tenant_id: Optional[str]) -> None:
    """Kick off an initial load if the tenant has no cached data yet."""
    if not tenant_id:
        return

    if check_load_required(tenant_id):
        oauth_token = session.get("xero_oauth2_token")
        if not oauth_token:
            logger.warning("Skipping background sync; missing OAuth token", tenant_id=tenant_id)
        else:
            _executor.submit(sync_data, tenant_id, TenantStatus.LOADING, oauth_token)


def _set_active_tenant(tenant_id: Optional[str]) -> None:
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


@app.route("/api/tenant-statuses", methods=["GET"])
@xero_token_required
def tenant_status():
    """Return the list of tenant IDs currently syncing."""
    tenant_records = session.get("xero_tenants", []) or []
    tenant_ids = [t.get("tenantId") for t in tenant_records if isinstance(t, dict)]
    try:
        tenant_statuses = TenantDataRepository.get_tenant_statuses(tenant_ids)
    except Exception as exc:
        logger.exception("Failed to load tenant sync status", tenant_ids=tenant_ids, error=exc)
        return jsonify({"error": "Unable to determine sync status"}), 500

    for tenant_id, status in tenant_statuses.items():
        if tenant_id:
            cache_provider.set_tenant_status_cache(tenant_id, str(status))

    return jsonify(tenant_statuses), 200


@app.route("/api/tenants/<tenant_id>/sync", methods=["POST"])
@xero_token_required
def trigger_tenant_sync(tenant_id: str):
    """Trigger a background sync for the specified tenant."""
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        return jsonify({"error": "TenantID is required"}), 400

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
        _executor.submit(sync_data, tenant_id, TenantStatus.SYNCING, oauth_token) # TODO: Perhaps worth checking if there is row in DDB/files in S3
        logger.info("Manual tenant sync triggered", tenant_id=tenant_id)
        return jsonify({"started": True}), 202
    except Exception as exc:
        logger.exception("Failed to trigger manual sync", tenant_id=tenant_id, error=exc)
        return jsonify({"error": "Failed to trigger sync"}), 500


@app.route("/")
@route_handler_logging
def index():
    logger.info("Rendering index")
    return render_template("index.html")


@app.route("/tenant_management")
@route_handler_logging
@xero_token_required
def tenant_management():
    tenants = session.get("xero_tenants") or []
    active_tenant_id = session.get("xero_tenant_id")
    message = session.pop("tenant_message", None)
    error = session.pop("tenant_error", None)

    active_tenant = next((t for t in tenants if t.get("tenantId") == active_tenant_id), None)
    logger.info("Rendering tenant_management page", active_tenant_id=active_tenant_id, tenants=len(tenants), has_message=bool(message), has_error=bool(error), authenticated=bool(session.get("access_token")))

    return render_template("tenant_management.html", tenants=tenants, active_tenant_id=active_tenant_id, active_tenant=active_tenant, message=message, error=error)


@app.route("/favicon.ico")
def ignore_favicon():
    """Return empty 204 for favicon requests."""
    return "", 204


@app.route("/upload-statements", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant before uploading statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def upload_statements():
    """Upload one or more PDF statements and register them for processing."""
    tenant_id = session.get("xero_tenant_id")

    contacts_raw = get_contacts()
    contacts_list = sorted(contacts_raw, key=lambda c: (c.get("name") or "").casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    success_count: Optional[int] = None
    error_messages: List[str] = []
    logger.info("Rendering upload statements", tenant_id=tenant_id, available_contacts=len(contacts_list))

    uploads_ok = 0
    if request.method == "POST":
        files = [f for f in request.files.getlist("statements") if f and f.filename]
        names = request.form.getlist("contact_names")
        uploads_ok = 0
        logger.info("Upload statements submitted", tenant_id=tenant_id, files=len(files), names=len(names))
        if not files:
            logger.info("Please add at least one statement (PDF).")
        elif len(files) != len(names):
            logger.info("Each statement must have a contact selected.")
        else:
            for f, contact in zip(files, names):
                file_bytes = getattr(f, "content_length", None)
                if not contact.strip():
                    logger.info("Missing contact", statement_filename=f.filename)
                    continue
                if not is_allowed_pdf(f.filename, f.mimetype):
                    logger.info("Rejected non-PDF upload", statement_filename=f.filename)
                    continue

                contact_name = contact.strip()
                contact_id: Optional[str] = contact_lookup.get(contact_name)
                if not contact_id:
                    logger.warning("Upload blocked; contact not found", tenant_id=tenant_id, contact_name=contact_name, statement_filename=f.filename)
                    error_messages.append(f"Contact '{contact_name}' was not recognised. Please select a contact from the list.")
                    continue

                try:
                    get_contact_config(tenant_id, contact_id)
                except KeyError:
                    logger.warning("Upload blocked; contact config missing", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=f.filename)
                    error_messages.append(f"Contact '{contact_name}' does not have a statement config yet. Please configure it before uploading.")
                    continue
                except Exception as exc:
                    logger.exception("Upload blocked; config lookup failed", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_filename=f.filename, error=exc)
                    error_messages.append(f"Could not load the config for '{contact_name}'. Please try again later.")
                    continue

                statement_id = str(uuid.uuid4())
                logger.info("Preparing statement upload", tenant_id=tenant_id, contact_id=contact_id, contact_name=contact_name, statement_id=statement_id, statement_filename=f.filename, bytes=file_bytes)

                entry = {
                    "statement_id": statement_id,
                    "statement_name": f.filename,
                    "contact_name": contact_name,
                    "contact_id": contact_id,
                }

                # Upload pdf statement to S3
                pdf_statement_key = statement_pdf_s3_key(tenant_id, statement_id)
                upload_statement_to_s3(fs_like=f, key=pdf_statement_key)
                logger.info("Uploaded statement PDF", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, s3_key=pdf_statement_key)

                # Upload statement to ddb
                add_statement_to_table(tenant_id, entry)
                logger.info("Registered statement metadata", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, table_entry=entry)

                logger.info("Statement submitted", statement_id=statement_id, tenant_id=tenant_id, contact_id=contact_id)

                # Kick off background textraction so it's ready by the time the user views it
                json_statement_key = statement_json_s3_key(tenant_id, statement_id)
                _executor.submit(textract_in_background, tenant_id=tenant_id, contact_id=contact_id, pdf_key=pdf_statement_key, json_key=json_statement_key)
                logger.info("Queued Textract background job", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_statement_key, json_key=json_statement_key)

                uploads_ok += 1

        if uploads_ok:
            success_count = uploads_ok
        logger.info("Upload statements processed", tenant_id=tenant_id, succeeded=uploads_ok, errors=len(error_messages))

    return render_template("upload_statements.html", contacts=contacts_list, success_count=success_count, error_messages=error_messages)


@app.route("/instructions")
@xero_token_required
@route_handler_logging
def instructions():
    return render_template("instructions.html")

@app.route("/statements")
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statements():
    tenant_id = session.get("xero_tenant_id")

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

    def _parse_iso_date(value: object) -> Optional[date]:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return date.fromisoformat(stripped)
        except ValueError:
            return None

    def _parse_iso_datetime(value: object) -> Optional[datetime]:
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

    for row in statement_rows:
        earliest = _parse_iso_date(row.get("EarliestItemDate"))
        latest = _parse_iso_date(row.get("LatestItemDate"))
        row["_earliest_item_date"] = earliest
        row["_latest_item_date"] = latest
        row["_uploaded_at"] = _parse_iso_datetime(row.get("UploadedAt"))
        if earliest and latest:
            row["ItemDateRangeDisplay"] = earliest.isoformat() if earliest == latest else f"{earliest.isoformat()} – {latest.isoformat()}"
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

    for row in statement_rows:
        row.pop("_earliest_item_date", None)
        row.pop("_latest_item_date", None)
        row.pop("_uploaded_at", None)

    base_args: Dict[str, Any] = {}
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

    return render_template(
        "statements.html",
        statements=statement_rows,
        show_completed=show_completed,
        message=message,
        current_sort=sort_key,
        current_dir=current_dir,
        sort_links=sort_links,
    )


@app.route("/statement/<statement_id>/delete", methods=["POST"])
@active_tenant_required("Please select a tenant before deleting statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def delete_statement(statement_id: str):
    tenant_id = session.get("xero_tenant_id")

    try:
        delete_statement_data(tenant_id, statement_id)
        session["statements_message"] = "Statement deleted."
    except Exception as exc:
        logger.exception("Failed to delete statement", tenant_id=tenant_id, statement_id=statement_id, error=exc)
        session["tenant_error"] = "Unable to delete the statement. Please try again."

    return redirect(url_for("statements"))

@app.route("/statement/<statement_id>", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant to view statements.")
@xero_token_required
@route_handler_logging
@block_when_loading
def statement(statement_id: str):
    tenant_id = session.get("xero_tenant_id")

    record = get_statement_record(tenant_id, statement_id)
    if not record:
        logger.info("Statement record not found", tenant_id=tenant_id, statement_id=statement_id)
        abort(404)

    items_view = (request.values.get("items_view") or "incomplete").strip().lower()
    if items_view not in {"incomplete", "completed", "all"}:
        items_view = "incomplete"
    show_payments_raw = (request.values.get("show_payments") or "true").strip().lower()
    show_payments = show_payments_raw in {"true", "1", "yes", "on"}
    logger.info("Statement detail requested", tenant_id=tenant_id, statement_id=statement_id, items_view=items_view, show_payments=show_payments, method=request.method)

    contact_name = ""
    if record:
        raw_contact_name = record.get("ContactName")
        if isinstance(raw_contact_name, str):
            contact_name = raw_contact_name.strip()
        elif raw_contact_name is not None:
            contact_name = str(raw_contact_name).strip()
    page_heading = contact_name or f"Statement {statement_id}"

    if request.method == "POST":
        action = request.form.get("action")
        if action in {"mark_complete", "mark_incomplete"}:
            completed_flag = action == "mark_complete"
            try:
                mark_statement_completed(tenant_id, statement_id, completed_flag)
                try:
                    set_all_statement_items_completed(tenant_id, statement_id, completed_flag)
                except Exception as exc:
                    logger.exception("Failed to toggle all statement items", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)

                session["statements_message"] = ("Statement marked as complete." if completed_flag else "Statement marked as incomplete.")
                logger.info("Statement completion updated", tenant_id=tenant_id, statement_id=statement_id, completed=completed_flag)
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("Failed to toggle statement completion", statement_id=statement_id, tenant_id=tenant_id, desired_state=completed_flag, error=exc)
                abort(500)
            return redirect(url_for("statements"))

        elif action in {"complete_item", "incomplete_item"}:
            statement_item_id = (request.form.get("statement_item_id") or "").strip()
            if statement_item_id:
                desired_state = action == "complete_item"
                try:
                    set_statement_item_completed(tenant_id, statement_item_id, desired_state)
                    logger.info("Statement item updated", tenant_id=tenant_id, statement_id=statement_id, statement_item_id=statement_item_id, completed=desired_state)
                except Exception as exc:
                    logger.exception("Failed to toggle statement item completion", statement_id=statement_id, statement_item_id=statement_item_id, tenant_id=tenant_id, desired_state=desired_state, error=exc)
            return redirect(url_for("statement", statement_id=statement_id, items_view=items_view, show_payments="true" if show_payments else "false"))

    json_statement_key = statement_json_s3_key(tenant_id, statement_id)

    contact_id = record.get("ContactID")
    is_completed = str(record.get("Completed", "")).lower() == "true"
    try:
        data, _ = fetch_json_statement(tenant_id=tenant_id, contact_id=contact_id, bucket=S3_BUCKET_NAME, json_key=json_statement_key)
    except StatementJSONNotFoundError:
        logger.info("Statement JSON pending", tenant_id=tenant_id, statement_id=statement_id, json_key=json_statement_key)
        return render_template(
            "statement.html",
            statement_id=statement_id,
            contact_name=contact_name,
            page_heading=page_heading,
            is_processing=True,
            is_completed=is_completed,
            items_view=items_view,
            show_payments=show_payments,
            incomplete_count=0,
            completed_count=0,
            all_statement_rows=[],
            statement_rows=[],
            raw_statement_headers=[],
            has_payment_rows=False,
        )

    # 1) Parse display configuration and left-side rows
    items = data.get("statement_items", []) or []
    contact_config = get_contact_config(tenant_id, contact_id)
    decimal_sep, thousands_sep = get_number_separators_from_config(contact_config)
    display_headers, rows_by_header, header_to_field, item_number_header = prepare_display_mappings(items, contact_config)

    # 2) Fetch Xero documents and classify each statement item
    invoices = get_invoices_by_contact(contact_id) or []
    credit_notes = get_credit_notes_by_contact(contact_id) or []
    payments = get_payments_by_contact(contact_id) or []
    logger.info("Fetched Xero documents", statement_id=statement_id, contact_id=contact_id, invoices=len(invoices), credit_notes=len(credit_notes), payments=len(payments))

    docs_for_matching = invoices + credit_notes
    matched_invoice_to_statement_item = match_invoices_to_statement_items(
        items=items,
        rows_by_header=rows_by_header,
        item_number_header=item_number_header,
        invoices=docs_for_matching,
    )

    matched_numbers: Set[str] = {key for key in matched_invoice_to_statement_item.keys() if isinstance(key, str)}
    match_by_item_id: Dict[str, Dict[str, str]] = {}
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

    invoice_number_by_id: Dict[str, str] = {}
    for inv in invoices:
        inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
        inv_number = str(inv.get("number") or "").strip() if isinstance(inv, dict) else ""
        if inv_id and inv_number:
            invoice_number_by_id[str(inv_id)] = inv_number

    payment_number_map: Dict[str, List[Dict[str, Any]]] = {}
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

    classification_updates: Dict[str, str] = {}
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        statement_item_id = it.get("statement_item_id")
        raw = it.get("raw", {}) if isinstance(it.get("raw"), dict) else {}
        current_type = str(it.get("item_type") or "").strip().lower()
        row_number = ""
        if item_number_header and idx < len(rows_by_header):
            row_number = str(rows_by_header[idx].get(item_number_header) or "").strip()

        new_type: Optional[str]
        source: Optional[str]
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

    if classification_updates:
        try:
            json_payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            upload_statement_to_s3(BytesIO(json_payload), json_statement_key)
            logger.info("Persisted statement item types to S3", statement_id=statement_id, updated=len(classification_updates))
        except Exception as exc:
            logger.exception("Failed to persist statement JSON", statement_id=statement_id, error=str(exc))

        for statement_item_id, new_type in classification_updates.items():
            try:
                tenant_statements_table.update_item(
                    Key={"TenantID": tenant_id, "StatementID": statement_item_id},
                    UpdateExpression="SET item_type = :item_type",
                    ExpressionAttributeValues={":item_type": new_type},
                )
            except ClientError as exc:
                logger.exception("Failed to persist item type to DynamoDB", statement_id=statement_id, statement_item_id=statement_item_id, item_type=new_type, error=str(exc))
        logger.info("Persisted statement item types to DynamoDB", statement_id=statement_id, updated=len(classification_updates))

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
    row_comparisons = build_row_comparisons(
        left_rows=rows_by_header,
        right_rows=right_rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
    )
    # Row highlight: if this row is linked to a Xero document (exact or substring),
    # consider the row a "match" for coloring purposes even if some cells differ.
    if item_number_header:
        row_matches: List[bool] = []
        for r in rows_by_header:
            num = (r.get(item_number_header) or "").strip()
            row_matches.append(bool(num and matched_invoice_to_statement_item.get(num)))
    else:
        # Fallback: if no number mapping, use strict all-cells match
        row_matches = [all(cell.matches for cell in row) for row in row_comparisons]

    item_status_map = get_statement_item_status_map(tenant_id, statement_id)

    if request.args.get("download") == "xlsx":
        header_labels = []
        statement_headers: List[str] = []
        xero_headers: List[str] = []

        for header in display_headers:
            label = (header or "").replace("_", " ").strip()
            if label:
                label = label[0].upper() + label[1:]
            else:
                label = header or ""
            header_labels.append((header, label))
            statement_headers.append(f"Statement {label}")
            xero_headers.append(f"Xero {label}")

        excel_headers = ["Item Type"] + statement_headers + xero_headers + ["Status"]

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Statement"
        worksheet.append(excel_headers)

        row_count = max(len(rows_by_header), len(right_rows_by_header))
        for idx in range(row_count):
            left_row = rows_by_header[idx] if idx < len(rows_by_header) else {}
            right_row = right_rows_by_header[idx] if idx < len(right_rows_by_header) else {}

            row_values: List[Any] = [item_types[idx] if idx < len(item_types) else ""]
            for src_header, label in header_labels:
                left_value = left_row.get(src_header, "") if isinstance(left_row, dict) else ""
                row_values.append("" if left_value is None else left_value)

            for src_header, label in header_labels:
                right_value = right_row.get(src_header, "") if isinstance(right_row, dict) else ""
                row_values.append("" if right_value is None else right_value)

            status_label = ""
            item = items[idx] if idx < len(items) else {}
            statement_item_id = item.get("statement_item_id") if isinstance(item, dict) else None
            if statement_item_id:
                status_label = "Completed" if item_status_map.get(statement_item_id, False) else "Incomplete"
            # Providing status in the sheet lets users filter finished work out quickly.
            row_values.append(status_label)

            worksheet.append(row_values)

        output = BytesIO()
        workbook.save(output)
        output.seek(0)
        excel_payload = output.getvalue()
        output.close()

        response = app.response_class(excel_payload, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        earliest_date_raw = record.get("EarliestItemDate")
        latest_date_raw = record.get("LatestItemDate")
        earliest_date: Optional[date] = None
        latest_date: Optional[date] = None
        try:
            if isinstance(earliest_date_raw, str):
                earliest_date = date.fromisoformat(earliest_date_raw.strip())
        except ValueError:
            earliest_date = None
        try:
            if isinstance(latest_date_raw, str):
                latest_date = date.fromisoformat(latest_date_raw.strip())
        except ValueError:
            latest_date = None

        if earliest_date and latest_date:
            if earliest_date == latest_date:
                date_segment = earliest_date.strftime("%Y-%m-%d")
            else:
                date_segment = f"{earliest_date.strftime('%Y-%m-%d')}_{latest_date.strftime('%Y-%m-%d')}"
        elif latest_date or earliest_date:
            chosen = latest_date or earliest_date
            date_segment = chosen.strftime("%Y-%m-%d") if chosen else ""
        else:
            date_segment = ""

        contact_name = record.get("ContactName") if isinstance(record, dict) else ""
        contact_segment = secure_filename(str(contact_name or "").strip()) or f"statement_{statement_id}"

        parts = [contact_segment]
        if date_segment:
            parts.append(date_segment)
        download_name = "_".join(parts) + "_export.xlsx"

        response.headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        logger.info("Statement Excel generated", tenant_id=tenant_id, statement_id=statement_id, rows=row_count, excel_filename=download_name)
        return response

    statement_rows: List[Dict[str, Any]] = []
    for idx, left_row in enumerate(rows_by_header):
        item = items[idx] if idx < len(items) else {}
        statement_item_id = item.get("statement_item_id") if isinstance(item, dict) else None
        is_item_completed = False
        if statement_item_id:
            is_item_completed = item_status_map.get(statement_item_id, False)

        right_row_dict = right_rows_by_header[idx] if idx < len(right_rows_by_header) else {}
        flags = []
        if isinstance(item, dict):
            raw_flags = item.get("_flags") or []
            if isinstance(raw_flags, list):
                seen_flags = set()
                for flag in raw_flags:
                    if not isinstance(flag, str):
                        continue
                    normalized = flag.strip()
                    if not normalized or normalized in seen_flags:
                        continue
                    seen_flags.add(normalized)
                    flags.append(normalized)

        # Build Xero links by extracting IDs from matched data
        xero_invoice_id: Optional[str] = None
        xero_credit_note_id: Optional[str] = None
        if item_number_header and idx < len(rows_by_header):
            row_number = str(rows_by_header[idx].get(item_number_header) or "").strip()
            if row_number:
                match = matched_invoice_to_statement_item.get(row_number)
                if isinstance(match, dict):
                    inv = match.get("invoice")
                    if isinstance(inv, dict):
                        cn_id = inv.get("credit_note_id")
                        if isinstance(cn_id, str) and cn_id.strip():
                            xero_credit_note_id = cn_id.strip()
                        inv_id = inv.get("invoice_id")
                        if isinstance(inv_id, str) and inv_id.strip():
                            xero_invoice_id = inv_id.strip()

        statement_rows.append(
            {
                "statement_item_id": statement_item_id,
                "left_values": [left_row.get(h, "") for h in display_headers],
                "right_values": [right_row_dict.get(h, "") for h in display_headers],
                "cell_comparisons": row_comparisons[idx] if idx < len(row_comparisons) else [],
                "matches": row_matches[idx] if idx < len(row_matches) else False,
                "is_completed": is_item_completed,
                "flags": flags,
                "item_type": (
                    (item.get("item_type") if isinstance(item, dict) else None)
                    or (item_types[idx] if idx < len(item_types) else "invoice")
                ),
                "xero_invoice_id": xero_invoice_id,
                "xero_credit_note_id": xero_credit_note_id,
            }
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

    logger.info("Statement detail rendered", tenant_id=tenant_id, statement_id=statement_id, visible=len(visible_rows), total=len(statement_rows), completed=completed_count, incomplete=incomplete_count, items_view=items_view, show_payments=show_payments)

    return render_template(
        "statement.html",
        statement_id=statement_id,
        contact_name=contact_name,
        page_heading=page_heading,
        is_processing=False,
        is_completed=is_completed,
        raw_statement_headers=display_headers,
        statement_rows=visible_rows,
        all_statement_rows=statement_rows,
        row_comparisons=row_comparisons,
        completed_count=completed_count,
        incomplete_count=incomplete_count,
        items_view=items_view,
        show_payments=show_payments,
        has_payment_rows=has_payment_rows,
    )


@app.route("/tenants/select", methods=["POST"])
@xero_token_required
@route_handler_logging
def select_tenant():
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    logger.info("Tenant selection submitted", tenant_id=tenant_id, available=len(tenants))

    if tenant_id and any(t.get("tenantId") == tenant_id for t in tenants):
        _set_active_tenant(tenant_id)
        tenant_name = session.get("xero_tenant_name") or tenant_id
        session["tenant_message"] = f"Switched to tenant: {tenant_name}."
        logger.info("Tenant switched", tenant_id=tenant_id, tenant_name=tenant_name)
    else:
        session["tenant_error"] = "Unable to select tenant. Please try again."
        logger.info("Tenant selection failed", tenant_id=tenant_id)

    return redirect(url_for("tenant_management"))


@app.route("/tenants/sync", methods=["POST"])
@active_tenant_required("Please select a tenant before synchronising.", flash_key="tenant_message")
@xero_token_required
@route_handler_logging
def sync_tenant():
    tenant_id = session.get("xero_tenant_id")

    session["tenant_message"] = "Tenant data is refreshed on demand; no manual sync is required."
    logger.info("Manual tenant sync requested", tenant_id=tenant_id)
    return redirect(url_for("tenant_management"))


@app.route("/tenants/disconnect", methods=["POST"])
@xero_token_required
@route_handler_logging
def disconnect_tenant():
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    tenant = next((t for t in tenants if t.get("tenantId") == tenant_id), None)

    if not tenant:
        session["tenant_error"] = "Tenant not found in session."
        return redirect(url_for("tenant_management"))

    connection_id = tenant.get("connectionId")
    access_token = session.get("access_token")
    logger.info("Tenant disconnect submitted", tenant_id=tenant_id, has_connection=bool(connection_id))

    if connection_id and access_token:
        try:
            resp = requests.delete(f"https://api.xero.com/connections/{connection_id}", headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            if resp.status_code not in (200, 204):
                logger.error("Failed to disconnect tenant", tenant_id=tenant_id, status_code=resp.status_code, body=resp.text)
                session["tenant_error"] = "Unable to disconnect tenant from Xero."
                return redirect(url_for("tenant_management"))
        except Exception as exc:
            logger.exception("Exception disconnecting tenant", tenant_id=tenant_id, error=exc)
            session["tenant_error"] = "An error occurred while disconnecting the tenant."
            return redirect(url_for("tenant_management"))

    # Remove tenant locally regardless (in case it was already disconnected)
    updated = [t for t in tenants if t.get("tenantId") != tenant_id]
    session["xero_tenants"] = updated

    if session.get("xero_tenant_id") == tenant_id:
        next_tenant_id = updated[0]["tenantId"] if updated else None
        _set_active_tenant(next_tenant_id)

    session["tenant_message"] = "Tenant disconnected."
    logger.info("Tenant disconnected", tenant_id=tenant_id, remaining=len(updated))
    if not updated:
        return redirect(url_for("index"))
    return redirect(url_for("tenant_management"))

@app.route("/configs", methods=["GET", "POST"])
@active_tenant_required("Please select a tenant before configuring mappings.")
@xero_token_required
@route_handler_logging
@block_when_loading
def configs():
    """View and edit contact-specific mapping configuration."""
    tenant_id = session.get("xero_tenant_id")

    contacts_raw = get_contacts()
    contacts_list = sorted(
        contacts_raw,
        key=lambda c: (c.get("name") or "").casefold(),
    )
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    logger.info("Rendering configs", tenant_id=tenant_id, contacts=len(contacts_list))


    selected_contact_name: Optional[str] = None
    selected_contact_id: Optional[str] = None
    mapping_rows: List[Dict[str, Any]] = []  # {field, values:list[str], is_multi:bool}
    message: Optional[str] = None
    error: Optional[str] = None
    selected_decimal_separator: str = DEFAULT_DECIMAL_SEPARATOR
    selected_thousands_separator: str = DEFAULT_THOUSANDS_SEPARATOR
    selected_date_format: str = ""

    # Normalise dropdown values so we only persist supported separators.
    def _normalize_decimal_separator(value: Optional[str]) -> str:
        if value in {opt[0] for opt in DECIMAL_SEPARATOR_OPTIONS}:
            return value or DEFAULT_DECIMAL_SEPARATOR
        return DEFAULT_DECIMAL_SEPARATOR

    def _normalize_thousands_separator(value: Optional[str]) -> str:
        if value in {opt[0] for opt in THOUSANDS_SEPARATOR_OPTIONS}:
            return value if value is not None else DEFAULT_THOUSANDS_SEPARATOR
        return DEFAULT_THOUSANDS_SEPARATOR

    def _build_rows(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build table rows for canonical fields using existing config values."""
        # Flatten mapping sources: nested 'statement_items' + root-level keys
        nested = cfg.get("statement_items") if isinstance(cfg, dict) else None
        nested = nested if isinstance(nested, dict) else {}
        flat: Dict[str, Any] = {}
        flat.update(nested)
        allowed_keys = set(StatementItem.model_fields.keys())
        disallowed = {"raw", "statement_item_id"}
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if k in allowed_keys and k not in disallowed:
                    flat[k] = v

        flat.pop("reference", None)
        flat.pop("item_type", None)

        # Canonical field order from the Pydantic model, prioritising config UI alignment
        preferred_order = ["number", "total", "date", "due_date"]
        model_fields = [f for f in StatementItem.model_fields.keys() if f not in {"raw", "statement_item_id", "item_type"}]
        remaining_fields = [f for f in model_fields if f not in preferred_order]
        canonical_order = preferred_order + remaining_fields

        rows: List[Dict[str, Any]] = []
        for f in canonical_order:
            if f in {"reference", "item_type"}:
                continue
            val = flat.get(f)
            if f == "total":
                values = [str(v) for v in val] if isinstance(val, list) else ([str(val)] if isinstance(val, str) else [""])
                rows.append({"field": f, "values": values or [""], "is_multi": True})
            else:
                values = [str(val)] if isinstance(val, str) else [""]
                rows.append({"field": f, "values": values, "is_multi": False})
        return rows

    if request.method == "POST":
        action = request.form.get("action")
        if action == "load":
            # Load existing config for the chosen contact name
            selected_contact_name = (request.form.get("contact_name") or "").strip()
            selected_contact_id = contact_lookup.get(selected_contact_name)
            logger.info("Config load submitted", tenant_id=tenant_id, contact_name=selected_contact_name, contact_id=selected_contact_id)
            if not selected_contact_id:
                error = "Please select a valid contact."
                logger.info("Config load failed", tenant_id=tenant_id, contact_name=selected_contact_name)
            else:
                try:
                    cfg = get_contact_config(tenant_id, selected_contact_id)
                    # Build rows including canonical fields; do not error if keys are missing
                    mapping_rows = _build_rows(cfg)
                    selected_decimal_separator = _normalize_decimal_separator(str(cfg.get("decimal_separator", "")))
                    selected_thousands_separator = _normalize_thousands_separator(str(cfg.get("thousands_separator", "")))
                    selected_date_format = str((cfg.get("date_format") or "")) if isinstance(cfg, dict) else ""
                    logger.info("Config loaded", tenant_id=tenant_id, contact_id=selected_contact_id, keys=len(cfg) if isinstance(cfg, dict) else 0)
                except KeyError:
                    # No existing config: show canonical fields with empty mapping values
                    mapping_rows = _build_rows({})
                    selected_decimal_separator = DEFAULT_DECIMAL_SEPARATOR
                    selected_thousands_separator = DEFAULT_THOUSANDS_SEPARATOR
                    selected_date_format = ""
                    message = "No existing config found. You can create one below."
                    logger.info("Config not found", tenant_id=tenant_id, contact_id=selected_contact_id)
                except Exception as e:
                    error = f"Failed to load config: {e}"
                    logger.info("Config load error", tenant_id=tenant_id, contact_id=selected_contact_id, error=e)

        elif action == "save_map":
            # Save edited mapping
            selected_contact_id = request.form.get("contact_id")
            selected_contact_name = request.form.get("contact_name")
            logger.info("Config save submitted", tenant_id=tenant_id, contact_id=selected_contact_id, contact_name=selected_contact_name)
            try:
                try:
                    existing = get_contact_config(tenant_id, selected_contact_id)
                except KeyError:
                    existing = {}
                # Determine which fields were displayed/edited
                posted_fields = [f for f in request.form.getlist("fields[]") if f]

                selected_decimal_separator = _normalize_decimal_separator(request.form.get("decimal_separator"))
                selected_thousands_separator = _normalize_thousands_separator(request.form.get("thousands_separator"))
                selected_date_format = (request.form.get("date_format") or "").strip()

                # Preserve any root keys not shown in the mapping editor.
                # Explicitly drop legacy 'statement_items' (we no longer store nested mappings).
                preserved = {k: v for k, v in existing.items() if k not in posted_fields + ["statement_items"] and k not in {"reference", "item_type"}}

                # Rebuild mapping from form
                new_map: Dict[str, Any] = {}
                for f in posted_fields:
                    if f == "total":
                        total_vals = [v.strip() for v in request.form.getlist("map[total][]") if v.strip()]
                        new_map["total"] = total_vals
                    else:
                        val = request.form.get(f"map[{f}]")
                        new_map[f] = (val or "").strip()
                number_value = (new_map.get("number") or "").strip()
                if not number_value:
                    error = "The 'Number' field is mandatory. Please map the statement column that contains invoice numbers."
                    message = None
                    combined = {
                        **preserved,
                        **new_map,
                        "date_format": selected_date_format,
                        "decimal_separator": selected_decimal_separator,
                        "thousands_separator": selected_thousands_separator,
                    }
                    mapping_rows = _build_rows(combined)
                else:
                    # Merge and save (root-level only; no nested 'statement_items')
                    to_save = {
                        **preserved,
                        **new_map,
                        "date_format": selected_date_format,
                        "decimal_separator": selected_decimal_separator,
                        "thousands_separator": selected_thousands_separator,
                    }
                    set_contact_config(tenant_id, selected_contact_id, to_save)
                    logger.info("Contact config saved", tenant_id=tenant_id, contact_id=selected_contact_id, contact_name=selected_contact_name, config=to_save)
                    message = "Config updated successfully."
                    mapping_rows = _build_rows(to_save)
            except Exception as e:
                error = f"Failed to save config: {e}"
                logger.info("Config save failed", tenant_id=tenant_id, contact_id=selected_contact_id, error=e)

    example_rows = _build_rows(EXAMPLE_CONFIG)
    example_date_format = str(EXAMPLE_CONFIG.get("date_format") or "")
    example_decimal_separator = EXAMPLE_CONFIG.get("decimal_separator", DEFAULT_DECIMAL_SEPARATOR)
    example_thousands_separator = EXAMPLE_CONFIG.get("thousands_separator", DEFAULT_THOUSANDS_SEPARATOR)
    decimal_separator_labels = dict(DECIMAL_SEPARATOR_OPTIONS)
    thousands_separator_labels = dict(THOUSANDS_SEPARATOR_OPTIONS)

    return render_template(
        "configs.html",
        contacts=contacts_list,
        selected_contact_name=selected_contact_name,
        selected_contact_id=selected_contact_id,
        mapping_rows=mapping_rows,
        example_rows=example_rows,
        message=message,
        error=error,
        field_descriptions=FIELD_DESCRIPTIONS,
        date_format=selected_date_format,
        decimal_separator=selected_decimal_separator,
        thousands_separator=selected_thousands_separator,
        decimal_separator_options=DECIMAL_SEPARATOR_OPTIONS,
        thousands_separator_options=THOUSANDS_SEPARATOR_OPTIONS,
        example_date_format=example_date_format,
        example_decimal_separator=example_decimal_separator,
        example_thousands_separator=example_thousands_separator,
        decimal_separator_labels=decimal_separator_labels,
        thousands_separator_labels=thousands_separator_labels,
    )

@app.route("/login")
@route_handler_logging
def login():
    logger.info("Login initiated")
    if not CLIENT_ID or not CLIENT_SECRET:
        return "Missing XERO_CLIENT_ID or XERO_CLIENT_SECRET env vars", 500

    # Create and store a CSRF state
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state

    # Optional but recommended for OIDC
    nonce = secrets.token_urlsafe(24)
    session["oauth_nonce"] = nonce

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": scope_str(),
        "state": state,
    }
    logger.info("Redirecting to Xero authorization", scope_count=len(scope_str().split()))
    return redirect(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")

@app.route("/callback")
@route_handler_logging
def callback():
    # Handle user-denied or error cases
    if "error" in request.args:
        logger.error("OAuth error", error_code=400, error_description=request.args.get('error_description'), error=request.args['error'])
        return f"OAuth error: {request.args.get('error_description', request.args['error'])}", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        logger.error("No authorization code returned from Xero", error_code=400)
        return "No authorization code returned from Xero", 400

    # Validate state
    if not state or state != session.get("oauth_state"):
        logger.error("Invalid OAuth state", error_code=400)
        abort(400, "Invalid OAuth state")

    # Exchange code for tokens
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    # Xero expects client_secret_basic (HTTP Basic) auth for token endpoint
    token_res = requests.post(TOKEN_URL, data=data, headers=headers, auth=(CLIENT_ID, CLIENT_SECRET))
    if token_res.status_code != 200:
        logger.error("Error fetching token", error=token_res.text, error_code=400)
        return f"Error fetching token: {token_res.text}", 400

    tokens = token_res.json()
    save_xero_oauth2_token(tokens)

    session["access_token"] = tokens.get("access_token")

    conn_res = requests.get(
        "https://api.xero.com/connections",
        headers={"Authorization": f"Bearer {session['access_token']}"},
        timeout=20,
    )

    conn_res.raise_for_status()
    connections = conn_res.json()
    if not connections:
        logger.error("No Xero connections found for this user.", error_code=400)
        return "No Xero connections found for this user.", 400

    tenants = [
        {
            "tenantId": conn.get("tenantId"),
            "tenantName": conn.get("tenantName"),
            "tenantType": conn.get("tenantType"),
            "connectionId": conn.get("id"),
        }
        for conn in connections
        if conn.get("tenantId")
    ]

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

    # Clear state after successful exchange
    session.pop("oauth_state", None)
    session.pop("oauth_nonce", None)

    logger.info("OAuth callback processed", tenants=len(tenants))
    return redirect(url_for("tenant_management"))

@app.route("/logout")
@route_handler_logging
def logout():
    logger.info("Logout requested", had_tenant=bool(session.get("xero_tenant_id")))
    session.clear()
    return redirect(url_for("index"))


@app.route('/.well-known/<path:path>')
def chrome_devtools_ping(path):
    # Avoids 404 error being logged when chrome developer tools is open
    # /.well-known/appspecific/com.chrome.devtools.json
    return '', 204  # No content, indicates "OK but nothing here"


if __name__ == "__main__":
    app.run(port=8080, debug=True)
