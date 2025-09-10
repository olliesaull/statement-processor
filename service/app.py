import os
import difflib
import secrets
import urllib.parse
import uuid

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
from core.transform import equal, get_contact_config
from utils import (
    add_statement_to_table,
    api_client,
    get_contact_for_statement,
    get_contacts,
    get_incomplete_statements,
    get_invoices_by_contact,
    get_or_create_json_statement,
    is_allowed_pdf,
    save_xero_oauth2_token,
    scope_str,
    upload_statement_to_s3,
    xero_token_required,
)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "http://localhost:8080/callback"

@app.route('/favicon.ico')
def ignore_favicon():
    return ('', 204)  # Empty response, no content

@app.route('/contacts', methods=['GET', 'POST'])
@xero_token_required
def contacts():
    contacts = get_contacts()
    contacts = sorted(contacts, key=lambda c: c["name"].casefold())

    if request.method == 'POST':
        contact_name = request.form.get('contact_name')
        for contact in contacts:
            if contact["name"] == contact_name:
                return redirect(url_for("invoices", contact_id=contact["contact_id"]))

    return render_template("contacts.html", contacts=contacts)

@app.route("/upload-statements", methods=['GET', 'POST'])
@xero_token_required
def upload_statements():
    contacts = get_contacts()
    contacts = sorted(contacts, key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts}

    if request.method == 'POST':
        files = [f for f in request.files.getlist('statements') if f and f.filename]
        names = [n for n in request.form.getlist('contact_names')]

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

                contact_id: str = contact_lookup.get(contact.strip())
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

    return render_template("upload_statements.html", contacts=contacts)

@app.route("/statements")
@xero_token_required
def statements():
    return render_template("statements.html", incomplete_statements=get_incomplete_statements())

@app.route("/statement/<statement_id>", methods=['GET', 'POST'])
@xero_token_required
def statement(statement_id):
    tenant_id = session.get("xero_tenant_id")

    route_key = f"{tenant_id}/{statement_id}"
    pdf_statement_key  = f"{route_key}.pdf"
    json_statement_key = f"{route_key}.json"

    # Get existing JSON or build/upload via Textract
    contact_id = get_contact_for_statement(tenant_id, statement_id)
    data, _ = get_or_create_json_statement(tenant_id, contact_id, S3_BUCKET_NAME, pdf_statement_key, json_statement_key)

    items = data.get("statement_items", []) or []
    raw_statement_headers = list(items[0].get("raw", {}).keys()) if items else []
    raw_statement_rows = [[it.get("raw", {}).get(k, "") for k in raw_statement_headers] for it in items]

    # Contact config -> defines which headers matter
    contact_config = get_contact_config(tenant_id, contact_id)
    items_template = (contact_config.get("statement_items") or [{}])[0]

    # Build header->invoice_field map (invert config)
    header_to_field = {}
    for canonical_field, mapped in items_template.items():
        if isinstance(mapped, str) and mapped.strip():
            header_to_field[mapped.strip()] = canonical_field

    # Only keep headers that appear in the config mapping
    display_headers = [h for h in raw_statement_headers if h in header_to_field]

    # 1) Turn row lists into row dicts (filtered by display_headers only)
    rows_by_header = []
    for row in raw_statement_rows:
        row_dict_full = dict(zip(raw_statement_headers, row))
        row_dict_filtered = {h: row_dict_full[h] for h in display_headers if h in row_dict_full}
        rows_by_header.append(row_dict_filtered)

    # 2) Identify which header column maps to the invoice "number" (if configured)
    item_number_header = None
    for canonical_field, mapped in items_template.items():
        if canonical_field == "number" and isinstance(mapped, str) and mapped in display_headers:
            item_number_header = mapped
            break

    # 2b) Fetch Xero invoices for the contact and build matches
    invoices = get_invoices_by_contact(contact_id)

    # Match Xero invoices to statement items by the detected "number" header
    matched_invoice_to_statement_item = {}
    if item_number_header:
        # Build fast lookup for statement items by their displayed invoice number
        stmt_by_number = {}
        for it in items:
            raw = it.get("raw", {}) if isinstance(it, dict) else {}
            num = raw.get(item_number_header, "")
            if num:
                key = str(num).strip()
                if key:
                    stmt_by_number[key] = it

        # Iterate Xero invoices from this contact and link to statement item (if present)
        for inv in invoices or []:
            inv_no = inv.get("number") if isinstance(inv, dict) else None
            if not inv_no:
                continue
            key = str(inv_no).strip()
            if not key:
                continue
            stmt_item = stmt_by_number.get(key)
            if stmt_item is not None:
                matched_invoice_to_statement_item[key] = {
                    "invoice": inv,
                    "statement_item": stmt_item,
                    "match_type": "exact",
                    "match_score": 1.0,
                    "matched_invoice_number": key,
                }
                print(f"Exact match: statement number '{key}' -> invoice '{key}'")

        # Fallback: fuzzy match any unmatched statement numbers using the same contact invoices
        def _norm_num(s: str) -> str:
            # Uppercase and keep only letters+digits for robust comparison
            s = str(s or "").upper().strip()
            return "".join(ch for ch in s if ch.isalnum())

        # Build candidate list once for speed
        candidates = []
        for inv in invoices or []:
            inv_no = inv.get("number") if isinstance(inv, dict) else None
            if not inv_no:
                continue
            inv_no_str = str(inv_no).strip()
            if not inv_no_str:
                continue
            candidates.append((inv_no_str, inv, _norm_num(inv_no_str)))

        # Determine which statement numbers are still unmatched
        numbers_in_rows = [
            (r.get(item_number_header) or "").strip()
            for r in rows_by_header
            if r.get(item_number_header)
        ]
        missing = [n for n in numbers_in_rows if n and n not in matched_invoice_to_statement_item]

        for key in missing:
            stmt_item = stmt_by_number.get(key)
            if stmt_item is None:
                continue
            target_norm = _norm_num(key)
            best = None
            best_ratio = -1.0
            for cand_no, inv, cand_norm in candidates:
                if target_norm and cand_norm and (target_norm == cand_norm):
                    ratio = 1.0
                elif target_norm and cand_norm and (target_norm in cand_norm or cand_norm in target_norm):
                    ratio = 0.95
                else:
                    ratio = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = (cand_no, inv)

            # Apply a reasonable threshold to avoid poor matches
            if best and best_ratio >= 0.75:
                inv_no_best, inv_obj = best
                matched_invoice_to_statement_item[key] = {
                    "invoice": inv_obj,
                    "statement_item": stmt_item,
                    "match_type": "fuzzy" if best_ratio < 1.0 else "exact",
                    "match_score": round(best_ratio, 3),
                    "matched_invoice_number": inv_no_best,
                }
                kind = "Exact" if best_ratio == 1.0 else "Fuzzy"
                print(f"{kind} match: statement number '{key}' -> invoice '{inv_no_best}' (score {best_ratio:.3f})")
            else:
                print(f"No match for statement number '{key}'")

    # 3) Build right-hand rows using matched invoices (fast lookup per row)
    right_rows_by_header = []
    for r in rows_by_header:
        inv_no = (r.get(item_number_header) or "").strip() if item_number_header else ""
        inv = (matched_invoice_to_statement_item.get(inv_no, {}) or {}).get("invoice", {})
        row_right = {}
        for h in display_headers:
            invoice_field = header_to_field.get(h)
            row_right[h] = inv.get(invoice_field, "") if invoice_field else ""
        right_rows_by_header.append(row_right)

    # 4) Compare LEFT (statement) vs RIGHT (Xero) for the displayed headers
    row_matches = []
    for left, right in zip(rows_by_header, right_rows_by_header):
        ok = True
        cells = {}
        for h in display_headers:
            eq = equal(left.get(h), right.get(h))
            cells[h] = eq
            if not eq:
                ok = False
        row_matches.append(ok)

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
@xero_token_required
def index():
    return render_template("index.html")

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
