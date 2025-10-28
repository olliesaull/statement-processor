import os
import secrets
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any, Dict, List, Optional

import requests
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
from werkzeug.utils import secure_filename
from openpyxl import Workbook

import cache_provider
from config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME, STAGE, logger
from core.contact_config_metadata import EXAMPLE_CONFIG, FIELD_DESCRIPTIONS
from core.get_contact_config import get_contact_config, set_contact_config
from core.item_classification import guess_statement_item_type
from core.models import StatementItem
from sync import check_load_required, sync_data
from tenant_data_repository import TenantDataRepository, TenantStatus
from utils import (
    StatementJSONNotFoundError,
    add_statement_to_table,
    block_when_loading,
    build_right_rows,
    build_row_comparisons,
    fetch_json_statement,
    get_completed_statements,
    get_date_format_from_config,
    get_incomplete_statements,
    get_statement_item_status_map,
    get_statement_record,
    is_allowed_pdf,
    mark_statement_completed,
    match_invoices_to_statement_items,
    prepare_display_mappings,
    delete_statement_data,
    route_handler_logging,
    save_xero_oauth2_token,
    scope_str,
    set_all_statement_items_completed,
    set_statement_item_completed,
    textract_in_background,
    upload_statement_to_s3,
    xero_token_required,
    active_tenant_required,
)
from xero_repository import (
    get_contacts,
    get_credit_notes_by_contact,
    get_invoices_by_contact,
    get_payments_by_contact,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(16))

cache = Cache(app, config={"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 0})
cache_provider.set_cache(cache)

# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "https://cloudcathode.com/callback" if STAGE == "prod" else "http://localhost:8080/callback"

_executor = ThreadPoolExecutor(max_workers=2)


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

    return render_template(
        "tenant_management.html",
        tenants=tenants,
        active_tenant_id=active_tenant_id,
        active_tenant=active_tenant,
        message=message,
        error=error,
    )


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
                pdf_statement_key = f"{tenant_id}/statements/{statement_id}.pdf"
                upload_statement_to_s3(fs_like=f, key=pdf_statement_key)
                logger.info("Uploaded statement PDF", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, s3_key=pdf_statement_key)

                # Upload statement to ddb
                add_statement_to_table(tenant_id, entry)
                logger.info("Registered statement metadata", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, table_entry=entry)

                logger.info("Statement submitted", statement_id=statement_id, tenant_id=tenant_id, contact_id=contact_id)

                # Kick off background textraction so it's ready by the time the user views it
                json_statement_key = f"{tenant_id}/statements/{statement_id}.json"
                _executor.submit(textract_in_background, tenant_id=tenant_id, contact_id=contact_id, pdf_key=pdf_statement_key, json_key=json_statement_key)
                logger.info("Queued Textract background job", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, pdf_key=pdf_statement_key, json_key=json_statement_key)

                uploads_ok += 1

        if uploads_ok:
            success_count = uploads_ok
        logger.info("Upload statements processed", tenant_id=tenant_id, succeeded=uploads_ok, errors=len(error_messages))

    return render_template(
        "upload_statements.html",
        contacts=contacts_list,
        success_count=success_count,
        error_messages=error_messages,
    )

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
    message = session.pop("statements_message", None)
    logger.info("Rendering statements", tenant_id=tenant_id, view=view, statements=len(statement_rows))

    return render_template("statements.html", statements=statement_rows, show_completed=show_completed, message=message)


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

    route_key = f"{tenant_id}/statements/{statement_id}"
    json_statement_key = f"{route_key}.json"

    contact_id = record.get("ContactID")
    is_completed = str(record.get("Completed", "")).lower() == "true"
    try:
        data, _ = fetch_json_statement(
            tenant_id=tenant_id,
            contact_id=contact_id,
            bucket=S3_BUCKET_NAME,
            json_key=json_statement_key,
        )
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
    display_headers, rows_by_header, header_to_field, item_number_header = prepare_display_mappings(items, contact_config)

    # 2) For each row, guess type (invoice/credit note/payment); fetch the needed doc sets
    has_invoice, has_credit_note, has_payment = False, False, False
    item_types: List[str] = []
    for idx, it in enumerate(items):
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        existing_type = ""
        if isinstance(it, dict):
            existing_type = str(it.get("item_type") or "").strip().lower()
        if existing_type not in {"invoice", "credit_note", "payment"}:
            t = guess_statement_item_type(raw)
            if isinstance(it, dict):
                it["item_type"] = t
        else:
            t = existing_type
        statement_item_id = it.get("statement_item_id") if isinstance(it, dict) else None
        logger.info("Statement type classified", statement_id=statement_id, statement_item_id=statement_item_id, item_type=t)
        item_types.append(t)
        if t == "credit_note":
            has_credit_note = True
        elif t == "payment":
            has_payment = True
        else:
            has_invoice = True

    if has_invoice and has_credit_note:
        invs = get_invoices_by_contact(contact_id) or []
        cns = get_credit_notes_by_contact(contact_id) or []
        docs = invs + cns
    elif has_credit_note:
        docs = get_credit_notes_by_contact(contact_id) or []
    else:
        docs = get_invoices_by_contact(contact_id) or []

    if has_payment:
        payments = get_payments_by_contact(contact_id) or []
        logger.info("Payments fetched for statement items", statement_id=statement_id, contact_id=contact_id, count=len(payments))

    matched_invoice_to_statement_item = match_invoices_to_statement_items(
        items=items,
        rows_by_header=rows_by_header,
        item_number_header=item_number_header,
        invoices=docs,
    )

    # 3) Build right-hand rows from the matched invoices
    date_fmt = get_date_format_from_config(contact_config)

    right_rows_by_header = build_right_rows(
        rows_by_header=rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
        matched_map=matched_invoice_to_statement_item,
        item_number_header=item_number_header,
        date_format=date_fmt,
    )

    # 4) Compare LEFT (statement) vs RIGHT (Xero) for row highlighting
    row_comparisons = build_row_comparisons(
        left_rows=rows_by_header,
        right_rows=right_rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
    )
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

            row_values: List[Any] = [
                item_types[idx] if idx < len(item_types) else ""
            ]
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

        response = app.response_class(
            excel_payload,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        download_name = f"statement_{statement_id}.xlsx"
        original_name = str(record.get("OriginalStatementFilename") or "").strip()
        if original_name:
            base, _ = os.path.splitext(original_name)
            sanitized_base = secure_filename(base) or f"statement_{statement_id}"
            download_name = f"{sanitized_base}_export.xlsx"
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
                flags = [str(flag) for flag in raw_flags]

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

    def _build_rows(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build table rows for canonical fields using existing config values."""
        # Flatten mapping sources: nested 'statement_items' + root-level keys
        nested = cfg.get("statement_items") if isinstance(cfg, dict) else None
        nested = nested if isinstance(nested, dict) else {}
        flat: Dict[str, Any] = {}
        flat.update(nested)
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if k in StatementItem.model_fields and k not in {"raw", "statement_item_id"}:
                    flat[k] = v

        flat.pop("reference", None)
        flat.pop("item_type", None)

        # Canonical field order from the Pydantic model, prioritising config UI alignment
        preferred_order = [
            "number",
            "total",
            "date",
            "due_date",
            "date_format",
        ]
        model_fields = [
            f
            for f in StatementItem.model_fields.keys()
            if f not in {"raw", "statement_item_id", "item_type"}
        ]
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
                    logger.info("Config loaded", tenant_id=tenant_id, contact_id=selected_contact_id, keys=len(cfg) if isinstance(cfg, dict) else 0)
                except KeyError:
                    # No existing config: show canonical fields with empty mapping values
                    mapping_rows = _build_rows({})
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

                # Preserve any root keys not shown in the mapping editor.
                # Explicitly drop legacy 'statement_items' (we no longer store nested mappings).
                preserved = {
                    k: v
                    for k, v in existing.items()
                    if k not in posted_fields + ["statement_items"] and k not in {"reference", "item_type"}
                }

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
                    combined = {**preserved, **new_map}
                    mapping_rows = _build_rows(combined)
                else:
                    # Merge and save (root-level only; no nested 'statement_items')
                    to_save = {**preserved, **new_map}
                    set_contact_config(tenant_id, selected_contact_id, to_save)
                    logger.info("Contact config saved", tenant_id=tenant_id, contact_id=selected_contact_id, contact_name=selected_contact_name, config=to_save)
                    message = "Config updated successfully."
                    mapping_rows = _build_rows(to_save)
            except Exception as e:
                error = f"Failed to save config: {e}"
                logger.info("Config save failed", tenant_id=tenant_id, contact_id=selected_contact_id, error=e)

    example_rows = _build_rows(EXAMPLE_CONFIG)

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
