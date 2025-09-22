import os
import secrets
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests
from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME, STAGE, logger
from core.get_contact_config import get_contact_config, set_contact_config
from core.models import StatementItem
from utils import (
    StatementJSONNotFoundError,
    add_statement_to_table,
    api_client,
    build_right_rows,
    build_row_matches,
    fetch_json_statement,
    get_completed_statements,
    get_contacts,
    get_statement_date_format_from_config,
    get_credit_notes_by_contact,
    get_incomplete_statements,
    get_invoices_by_contact,
    get_statement_record,
    guess_statement_item_type,
    is_allowed_pdf,
    mark_statement_completed,
    match_invoices_to_statement_items,
    prepare_display_mappings,
    route_handler_logging,
    save_xero_oauth2_token,
    scope_str,
    textract_in_background,
    upload_statement_to_s3,
    xero_token_required,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", os.urandom(16))

# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "https://cloudcathode.com/callback" if STAGE == "prod" else "http://localhost:8080/callback"

# Lightweight background executor for non-blocking textraction after upload
# Keep the pool small to avoid overwhelming Textract and our app.
_executor = ThreadPoolExecutor(max_workers=2)


def _set_active_tenant(tenant_id: Optional[str]) -> None:
    """Persist the selected tenant in the session."""
    tenants = session.get("xero_tenants", []) or []
    tenant_map = {t.get("tenantId"): t for t in tenants if t.get("tenantId")}
    if tenant_id and tenant_id in tenant_map:
        session["xero_tenant_id"] = tenant_id
        session["xero_tenant_name"] = tenant_map[tenant_id].get("tenantName")
    else:
        session.pop("xero_tenant_id", None)
        session.pop("xero_tenant_name", None)

FIELD_DESCRIPTIONS: Dict[str, str] = {
    "amount_due": (
        "One or more statement columns that represent the running balance for a line. "
        "If there are separate debit and credit columns, add both so we can pick the right amount."
    ),
    "amount_paid": (
        "Column showing payments or credits applied against the invoice. This is used to display Xero's amount paid."
    ),
    "date": (
        "Invoice or transaction date as it appears on the statement. This helps with formatting and matching."
    ),
    "due_date": (
        "Payment due date from the statement line. Leave blank if the supplier does not show a due date."
    ),
    "number": (
        "Document number on the statement (e.g. invoice number). This is the primary key we use when matching to Xero."
    ),
    "reference": (
        "Any descriptive text that helps identify the transaction (project, PO number, memo, etc.)."
    ),
    "statement_date_format": (
        "Date pattern using SDF tokens (e.g., 'D MMMM YYYY', 'MM/DD/YY'). See the guide below for full token descriptions and examples."
    ),
    "total": (
        "The gross invoice amount on the statement. We use this for comparisons when lining up the Xero totals."
    ),
}

@app.route("/favicon.ico")
def ignore_favicon():
    """Return empty 204 for favicon requests."""
    return "", 204

@app.route("/contacts", methods=["GET", "POST"])
@xero_token_required
@route_handler_logging
def contacts():
    """List tenant contacts; on POST, echo the selected contact ID to logs."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        session["tenant_error"] = "Please select a tenant to view contacts."
        return redirect(url_for("index"))

    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())

    if request.method == "POST":
        contact_name = request.form.get("contact_name")
        for c in contacts_list:
            if c["name"] == contact_name:
                print("*"*88)
                print(f"{contact_name}, {c["contact_id"]}")
                print("*"*88)

    return render_template("contacts.html", contacts=contacts_list)

@app.route("/upload-statements", methods=["GET", "POST"])
@xero_token_required
@route_handler_logging
def upload_statements():
    """Upload one or more PDF statements and register them for processing."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        session["tenant_error"] = "Please select a tenant before uploading statements."
        return redirect(url_for("index"))

    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}
    success_count: Optional[int] = None

    if request.method == "POST":
        files = [f for f in request.files.getlist("statements") if f and f.filename]
        names = request.form.getlist("contact_names")
        uploads_ok = 0

        if not files:
            logger.info("Please add at least one statement (PDF).")
        elif len(files) != len(names):
            logger.info("Each statement must have a contact selected.")
        else:
            for f, contact in zip(files, names):
                if not contact.strip():
                    logger.info("Missing contact", filename=f.filename)
                    continue
                if not is_allowed_pdf(f.filename, f.mimetype):
                    logger.info("Rejected non-PDF upload", filename=f.filename)
                    continue

                contact_id: Optional[str] = contact_lookup.get(contact.strip())
                statement_id = str(uuid.uuid4())

                entry = {
                    "statement_id": statement_id,
                    "statement_name": f.filename,
                    "contact_name": contact.strip(),
                    "contact_id": contact_id,
                }

                # Upload pdf statement to S3
                pdf_statement_key = f"{tenant_id}/{statement_id}.pdf"
                upload_statement_to_s3(fs_like=f, key=pdf_statement_key)

                # Upload statement to ddb
                add_statement_to_table(tenant_id, entry)

                # Kick off background textraction so it's ready by the time the user views it
                json_statement_key = f"{tenant_id}/{statement_id}.json"
                _executor.submit(
                    textract_in_background,
                    tenant_id=tenant_id,
                    contact_id=contact_id,
                    pdf_key=pdf_statement_key,
                    json_key=json_statement_key,
                )

                uploads_ok += 1

        if uploads_ok:
            success_count = uploads_ok

    return render_template(
        "upload_statements.html",
        contacts=contacts_list,
        success_count=success_count,
    )

@app.route("/statements")
@xero_token_required
@route_handler_logging
def statements():
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        session["tenant_error"] = "Please select a tenant to view statements."
        return redirect(url_for("index"))

    view = request.args.get("view", "incomplete").lower()
    show_completed = view == "completed"
    statement_rows = get_completed_statements() if show_completed else get_incomplete_statements()
    message = session.pop("statements_message", None)

    return render_template(
        "statements.html",
        statements=statement_rows,
        show_completed=show_completed,
        message=message,
    )

@app.route("/statement/<statement_id>", methods=["GET", "POST"])
@xero_token_required
@route_handler_logging
def statement(statement_id: str):
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        session["tenant_error"] = "Please select a tenant to view statements."
        return redirect(url_for("index"))

    record = get_statement_record(tenant_id, statement_id)
    if not record:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        if action in {"mark_complete", "mark_incomplete"}:
            completed_flag = action == "mark_complete"
            try:
                mark_statement_completed(tenant_id, statement_id, completed_flag)
                session["statements_message"] = (
                    "Statement marked as complete." if completed_flag else "Statement marked as incomplete."
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.info(
                    "Failed to toggle statement completion",
                    statement_id=statement_id,
                    tenant_id=tenant_id,
                    desired_state=completed_flag,
                    error=exc,
                )
                abort(500)
            return redirect(url_for("statements"))

    route_key = f"{tenant_id}/{statement_id}"
    json_statement_key = f"{route_key}.json"

    contact_id = record.get("ContactID")
    is_completed = record.get("Completed") is True
    try:
        data, _ = fetch_json_statement(
            tenant_id=tenant_id,
            contact_id=contact_id,
            bucket=S3_BUCKET_NAME,
            json_key=json_statement_key,
        )
    except StatementJSONNotFoundError:
        return render_template(
            "statement.html",
            statement_id=statement_id,
            is_processing=True,
            is_completed=is_completed,
        )

    # 1) Parse display configuration and left-side rows
    items = data.get("statement_items", []) or []
    contact_config = get_contact_config(tenant_id, contact_id)
    display_headers, rows_by_header, header_to_field, item_number_header = prepare_display_mappings(items, contact_config)

    # 2) For each row, guess type (invoice/credit note); fetch the needed doc sets
    has_invoice, has_credit_note = False, False
    for it in items:
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        t = guess_statement_item_type(raw)
        if t == "credit_note":
            has_credit_note = True
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

    matched_invoice_to_statement_item = match_invoices_to_statement_items(
        items=items,
        rows_by_header=rows_by_header,
        item_number_header=item_number_header,
        invoices=docs,
    )

    # 3) Build right-hand rows from the matched invoices
    stmt_date_fmt = get_statement_date_format_from_config(contact_config)

    right_rows_by_header = build_right_rows(
        rows_by_header=rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
        matched_map=matched_invoice_to_statement_item,
        item_number_header=item_number_header,
        statement_date_format=stmt_date_fmt,
    )

    # 4) Compare LEFT (statement) vs RIGHT (Xero) for row highlighting
    row_matches = build_row_matches(
        left_rows=rows_by_header,
        right_rows=right_rows_by_header,
        display_headers=display_headers,
    )

    return render_template(
        "statement.html",
        statement_id=statement_id,
        is_processing=False,
        is_completed=is_completed,
        raw_statement_headers=display_headers,
        raw_statement_rows=[[r[h] for h in display_headers] for r in rows_by_header],
        item_number_header=item_number_header,
        right_rows_by_header=right_rows_by_header,
        row_matches=row_matches,
    )

@app.route("/")
@route_handler_logging
def index():
    tenants = session.get("xero_tenants") or []
    active_tenant_id = session.get("xero_tenant_id")
    message = session.pop("tenant_message", None)
    error = session.pop("tenant_error", None)

    active_tenant = next((t for t in tenants if t.get("tenantId") == active_tenant_id), None)

    return render_template(
        "index.html",
        tenants=tenants,
        active_tenant_id=active_tenant_id,
        active_tenant=active_tenant,
        message=message,
        error=error,
        is_authenticated=bool(session.get("access_token")),
    )


@app.route("/tenants/select", methods=["POST"])
@xero_token_required
@route_handler_logging
def select_tenant():
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []

    if tenant_id and any(t.get("tenantId") == tenant_id for t in tenants):
        _set_active_tenant(tenant_id)
        tenant_name = session.get("xero_tenant_name") or tenant_id
        session["tenant_message"] = f"Switched to tenant: {tenant_name}."
    else:
        session["tenant_error"] = "Unable to select tenant. Please try again."

    return redirect(url_for("index"))


@app.route("/tenants/disconnect", methods=["POST"])
@xero_token_required
@route_handler_logging
def disconnect_tenant():
    tenant_id = (request.form.get("tenant_id") or "").strip()
    tenants = session.get("xero_tenants") or []
    tenant = next((t for t in tenants if t.get("tenantId") == tenant_id), None)

    if not tenant:
        session["tenant_error"] = "Tenant not found in session."
        return redirect(url_for("index"))

    connection_id = tenant.get("connectionId")
    access_token = session.get("access_token")

    if connection_id and access_token:
        try:
            resp = requests.delete(
                f"https://api.xero.com/connections/{connection_id}",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
            if resp.status_code not in (200, 204):
                logger.info(
                    "Failed to disconnect tenant",
                    tenant_id=tenant_id,
                    status_code=resp.status_code,
                    body=resp.text,
                )
                session["tenant_error"] = "Unable to disconnect tenant from Xero."
                return redirect(url_for("index"))
        except Exception as exc:
            logger.info("Exception disconnecting tenant", tenant_id=tenant_id, error=exc)
            session["tenant_error"] = "An error occurred while disconnecting the tenant."
            return redirect(url_for("index"))

    # Remove tenant locally regardless (in case it was already disconnected)
    updated = [t for t in tenants if t.get("tenantId") != tenant_id]
    session["xero_tenants"] = updated

    if session.get("xero_tenant_id") == tenant_id:
        next_tenant_id = updated[0]["tenantId"] if updated else None
        _set_active_tenant(next_tenant_id)

    session["tenant_message"] = "Tenant disconnected."
    return redirect(url_for("index"))

@app.route("/configs", methods=["GET", "POST"])
@xero_token_required
@route_handler_logging
def configs():
    """View and edit contact-specific mapping configuration."""
    tenant_id = session.get("xero_tenant_id")
    if not tenant_id:
        session["tenant_error"] = "Please select a tenant before configuring mappings."
        return redirect(url_for("index"))

    # List contacts for dropdown
    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}


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
                if k in StatementItem.model_fields and k != "raw":
                    flat[k] = v

        # Canonical field order from the Pydantic model
        canonical_order = [f for f in StatementItem.model_fields.keys() if f != "raw"]

        rows: List[Dict[str, Any]] = []
        for f in canonical_order:
            val = flat.get(f)
            if f == "amount_due":
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
            if not selected_contact_id:
                error = "Please select a valid contact."
            else:
                try:
                    cfg = get_contact_config(tenant_id, selected_contact_id)
                    # Build rows including canonical fields; do not error if keys are missing
                    mapping_rows = _build_rows(cfg)
                except KeyError:
                    # No existing config: show canonical fields with empty mapping values
                    mapping_rows = _build_rows({})
                    message = "No existing config found. You can create one below."
                except Exception as e:
                    error = f"Failed to load config: {e}"

        elif action == "save_map":
            # Save edited mapping
            selected_contact_id = request.form.get("contact_id")
            selected_contact_name = request.form.get("contact_name")
            try:
                try:
                    existing = get_contact_config(tenant_id, selected_contact_id)
                except KeyError:
                    existing = {}
                # Determine which fields were displayed/edited
                posted_fields = [f for f in request.form.getlist("fields[]") if f]

                # Preserve any root keys not shown in the mapping editor.
                # Explicitly drop legacy 'statement_items' (we no longer store nested mappings).
                preserved = {k: v for k, v in existing.items() if k not in posted_fields + ["statement_items"]}

                # Rebuild mapping from form
                new_map: Dict[str, Any] = {}
                for f in posted_fields:
                    if f == "amount_due":
                        ad_vals = [v.strip() for v in request.form.getlist("map[amount_due][]") if v.strip()]
                        new_map["amount_due"] = ad_vals
                    else:
                        val = request.form.get(f"map[{f}]")
                        new_map[f] = (val or "").strip()
                # Merge and save (root-level only; no nested 'statement_items')
                to_save = {**preserved, **new_map}
                set_contact_config(tenant_id, selected_contact_id, to_save)
                message = "Config updated successfully."
                mapping_rows = _build_rows(to_save)
            except Exception as e:
                error = f"Failed to save config: {e}"

    return render_template(
        "configs.html",
        contacts=contacts_list,
        selected_contact_name=selected_contact_name,
        selected_contact_id=selected_contact_id,
        mapping_rows=mapping_rows,
        message=message,
        error=error,
        field_descriptions=FIELD_DESCRIPTIONS,
    )

@app.route("/login")
@route_handler_logging
def login():
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
    api_client.set_oauth2_token(tokens)

    session["access_token"] = tokens.get("access_token")

    conn_res = requests.get(
        "https://api.xero.com/connections",
        headers={"Authorization": f"Bearer {session["access_token"]}"},
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

    session["xero_tenants"] = tenants

    current = session.get("xero_tenant_id")
    tenant_ids = [t["tenantId"] for t in tenants]
    if current in tenant_ids:
        _set_active_tenant(current)
    elif tenant_ids:
        _set_active_tenant(tenant_ids[0])
    else:
        _set_active_tenant(None)

    # Clear state after successful exchange
    session.pop("oauth_state", None)
    session.pop("oauth_nonce", None)

    return redirect(url_for("index"))

@app.route("/logout")
@route_handler_logging
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(port=8080, debug=True)
