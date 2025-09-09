import secrets
import urllib.parse
import uuid

import requests
from flask import (
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from configuration.config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME
from utils import (
    add_statement_to_table,
    api_client,
    equal,
    get_contact_config,
    get_contact_for_statement,
    get_contacts,
    get_incomplete_statements,
    get_invoices_by_numbers,
    get_or_create_json_statement,
    is_allowed_pdf,
    save_xero_oauth2_token,
    scope_str,
    upload_statement_to_s3,
    xero_token_required,
)

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "http://localhost:8080/callback"

def register_routes(app):
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

        # 2) Extract invoice numbers using config-mapped header for "number"
        item_number_header = None
        for canonical_field, mapped in items_template.items():
            if canonical_field == "number" and isinstance(mapped, str) and mapped in display_headers:
                item_number_header = mapped
                break

        if item_number_header:
            invoice_numbers = [r.get(item_number_header, "") for r in rows_by_header if r.get(item_number_header)]
        else:
            invoice_numbers = []

        # 3) Fetch invoices keyed by invoice number
        invoices_by_number = get_invoices_by_numbers(invoice_numbers=invoice_numbers)

        # 4) Align rows
        aligned_rows = []
        for r in rows_by_header:
            inv_no = r.get(item_number_header) if item_number_header else None
            aligned_rows.append({
                "statement": r,
                "invoice": invoices_by_number.get(inv_no) if inv_no else None,
                "invoice_number": inv_no
            })

        # 5) Build right-hand rows using invoice + config map
        right_rows_by_header = []
        for ar in aligned_rows:
            inv = ar["invoice"] or {}
            row_right = {}
            for h in display_headers:
                invoice_field = header_to_field.get(h)
                row_right[h] = inv.get(invoice_field, "") if invoice_field else ""
            right_rows_by_header.append(row_right)

        # 6) Compare LEFT (statement) vs RIGHT (Xero) for the displayed headers
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
            aligned_rows=aligned_rows,
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
