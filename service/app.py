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

from configuration.config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME
from core.get_contact_config import get_contact_config, set_contact_config
from core.models import StatementItem
from utils import (
    add_statement_to_table,
    api_client,
    build_right_rows,
    build_row_matches,
    get_contact_for_statement,
    get_contacts,
    get_credit_notes_by_contact,
    get_incomplete_statements,
    get_invoices_by_contact,
    get_or_create_json_statement,
    guess_statement_item_type,
    is_allowed_pdf,
    match_invoices_to_statement_items,
    prepare_display_mappings,
    save_xero_oauth2_token,
    scope_str,
    textract_in_background,
    upload_statement_to_s3,
    xero_token_required,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# Mirror selected config values in Flask app config for convenience
app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "http://localhost:8080/callback"

# Lightweight background executor for non-blocking textraction after upload
# Keep the pool small to avoid overwhelming Textract and our app.
_executor = ThreadPoolExecutor(max_workers=2)

@app.route("/favicon.ico")
def ignore_favicon():
    """Return empty 204 for favicon requests."""
    return "", 204

@app.route("/contacts", methods=["GET", "POST"])
@xero_token_required
def contacts():
    """List tenant contacts; on POST, echo the selected contact ID to logs."""
    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())

    if request.method == "POST":
        contact_name = request.form.get("contact_name")
        for c in contacts_list:
            if c["name"] == contact_name:
                print("*" * 88)
                print(f"{contact_name}: {c['contact_id']}")
                print("*" * 88)

    return render_template("contacts.html", contacts=contacts_list)

@app.route("/upload-statements", methods=["GET", "POST"])
@xero_token_required
def upload_statements():
    """Upload one or more PDF statements and register them for processing."""
    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}

    if request.method == "POST":
        files = [f for f in request.files.getlist("statements") if f and f.filename]
        names = request.form.getlist("contact_names")

        if not files:
            print("Please add at least one statement (PDF).")
        elif len(files) != len(names):
            print("Each statement must have a contact selected.")
        else:
            # Create statement identifier etc
            tenant_id = session.get("xero_tenant_id")

            for f, contact in zip(files, names):
                if not contact.strip():
                    print(f"Missing contact for '{f.filename}'.")
                    continue
                if not is_allowed_pdf(f.filename, f.mimetype):
                    print(f"Rejected '{f.filename}': only PDFs are allowed.")
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

    return render_template("upload_statements.html", contacts=contacts_list)

@app.route("/statements")
@xero_token_required
def statements():
    return render_template("statements.html", incomplete_statements=get_incomplete_statements())

@app.route("/statement/<statement_id>", methods=["GET", "POST"])
@xero_token_required
def statement(statement_id: str):
    tenant_id = session.get("xero_tenant_id")

    route_key = f"{tenant_id}/{statement_id}"
    pdf_statement_key = f"{route_key}.pdf"
    json_statement_key = f"{route_key}.json"

    # Get existing JSON or build/upload via Textract
    contact_id = get_contact_for_statement(tenant_id, statement_id)
    data, _ = get_or_create_json_statement(tenant_id, contact_id, S3_BUCKET_NAME, pdf_statement_key, json_statement_key)

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
    right_rows_by_header = build_right_rows(
        rows_by_header=rows_by_header,
        display_headers=display_headers,
        header_to_field=header_to_field,
        matched_map=matched_invoice_to_statement_item,
        item_number_header=item_number_header,
        statement_date_format=contact_config.get("statement_date_format"),
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
        raw_statement_headers=display_headers,
        raw_statement_rows=[[r[h] for h in display_headers] for r in rows_by_header],
        item_number_header=item_number_header,
        right_rows_by_header=right_rows_by_header,
        row_matches=row_matches,
    )

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/configs", methods=["GET", "POST"])
@xero_token_required
def configs():
    """View and edit contact-specific mapping configuration."""
    # List contacts for dropdown
    contacts_list = sorted(get_contacts(), key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts_list}

    tenant_id = session.get("xero_tenant_id")

    selected_contact_name: Optional[str] = None
    selected_contact_id: Optional[str] = None
    mapping_rows: List[Dict[str, Any]] = []  # {field, values:list[str], is_multi:bool}
    message: Optional[str] = None
    error: Optional[str] = None

    def _build_rows(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Build table rows for canonical fields, using existing config values if present.

        - Always include the canonical StatementItem fields (excluding 'raw').
        - Prefer root-level mappings; fall back to nested 'statement_items' if present.
        - 'amount_due' supports multiple possible headers (list).
        - 'statement_date_format' is shown and persisted at the root level.
        """
        # Flatten mapping sources: nested 'statement_items' + root-level keys
        nested = cfg.get("statement_items") if isinstance(cfg, dict) else None
        nested = nested if isinstance(nested, dict) else {}
        flat: Dict[str, Any] = {}
        flat.update(nested)
        if isinstance(cfg, dict):
            for k, v in cfg.items():
                if k in StatementItem.model_fields and k != "raw":
                    flat[k] = v

        # Merge raw maps from nested/root (root wins)
        nested_raw = nested.get("raw") if isinstance(nested, dict) else None
        nested_raw = nested_raw if isinstance(nested_raw, dict) else {}
        root_raw = cfg.get("raw") if isinstance(cfg, dict) else None
        root_raw = root_raw if isinstance(root_raw, dict) else {}
        raw_map: Dict[str, Any] = {**nested_raw, **root_raw}

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
        # Add RAW as a collapsible group row with nested items
        raw_items = []
        for rk in sorted(raw_map.keys()):
            rv = raw_map.get(rk)
            raw_items.append({"key": rk, "value": (rv if isinstance(rv, str) else "")})
        rows.append({"field": "raw", "values": [""], "is_multi": False, "raw_items": raw_items})
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
                posted_fields = request.form.getlist("fields[]")
                posted_fields = [f for f in posted_fields if f]

                # Separate 'raw' group from canonical ones
                simple_fields = [f for f in posted_fields if f != "raw"]

                # Preserve any root keys not shown in the mapping editor.
                # Explicitly drop legacy 'statement_items' (we no longer store nested mappings).
                preserved = {k: v for k, v in existing.items() if k not in simple_fields + ["raw", "statement_items"]}

                # Rebuild mapping from form
                new_map: Dict[str, Any] = {}
                for f in simple_fields:
                    if f == "amount_due":
                        ad_vals = [v.strip() for v in request.form.getlist("map[amount_due][]") if v.strip()]
                        new_map["amount_due"] = ad_vals
                    else:
                        val = request.form.get(f"map[{f}]")
                        new_map[f] = (val or "").strip()

                # Build the RAW mapping (merge existing + posted raw keys)
                existing_raw = existing.get("raw") if isinstance(existing.get("raw"), dict) else {}
                new_raw = dict(existing_raw)
                for rk in request.form.getlist("raw_keys[]"):
                    val = request.form.get(f"map[raw][{rk}]")
                    rk_lc = (rk or "").strip().lower()
                    val_lc = (val or "").strip().lower()
                    new_raw[rk_lc] = val_lc

                # Merge and save (root-level only; no nested 'statement_items')
                to_save = {**preserved, **new_map}
                to_save["raw"] = new_raw
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
    )

@app.route("/login")
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
def callback():
    # Handle user-denied or error cases
    if "error" in request.args:
        return f"OAuth error: {request.args.get('error_description', request.args['error'])}", 400

    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return "No authorization code returned from Xero", 400

    # Validate state
    if not state or state != session.get("oauth_state"):
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
        return "No Xero connections found for this user.", 400
    
    # pick the first active tenant (or filter by 'tenantType' as you prefer)
    session["xero_tenant_id"] = connections[0]["tenantId"]

    # Clear state after successful exchange
    session.pop("oauth_state", None)
    session.pop("oauth_nonce", None)

    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(port=8080, debug=True)
