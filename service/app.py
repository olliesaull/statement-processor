import os
import secrets
import urllib.parse
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Dict

import requests
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore
from xero_python.exceptions import AccountingBadRequestException

from configuration.resources import ddb, s3

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

CLIENT_ID = os.environ.get("XERO_CLIENT_ID")
CLIENT_SECRET = os.environ.get("XERO_CLIENT_SECRET")

AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
REDIRECT_URI = "http://localhost:8080/callback"

SCOPES = [
    "offline_access", "openid", "profile", "email", "accounting.transactions", "accounting.reports.read", "accounting.journals.read",
    "accounting.settings", "accounting.contacts", "accounting.attachments", "assets", "projects", "files.read",
]

ALLOWED_EXTENSIONS = {'.pdf'}
UPLOAD_DIR = Path('uploads')

def scope_str():
    return " ".join(SCOPES)

def get_xero_oauth2_token():
    # Return the dict the SDK expects, or None if not set
    return session.get("xero_oauth2_token")

def save_xero_oauth2_token(token: dict):
    # Persist the whole token dict in the session (or your DB)
    session["xero_oauth2_token"] = token


app.config["CLIENT_ID"] = CLIENT_ID
app.config["CLIENT_SECRET"] = CLIENT_SECRET
api_client = ApiClient(
    Configuration(
        # debug=app.config["DEBUG"],
        oauth2_token=OAuth2Token(
            client_id=app.config["CLIENT_ID"], client_secret=app.config["CLIENT_SECRET"]
        ),
    ),
    pool_threads=1,
    oauth2_token_getter=get_xero_oauth2_token,
    oauth2_token_saver=save_xero_oauth2_token,
)
api = AccountingApi(api_client)

# region Functions

def is_allowed_pdf(filename: str, mimetype: str) -> bool:
    ext_ok = Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
    # Some browsers may send 'application/pdf' or 'application/octet-stream' for PDFs.
    mime_ok = (mimetype == 'application/pdf')
    return ext_ok and mime_ok

def _fmt_date(d):
    # Xero SDK returns datetime/date or None
    if isinstance(d, (datetime, date)):
        return d.strftime("%Y-%m-%d")
    return None

def get_invoices(contact_id):
    tenant_id = session["xero_tenant_id"]

    try:
        result = api.get_invoices(
            tenant_id,
            where=f'Contact.ContactID==Guid("{contact_id}")',
            order="InvoiceNumber ASC",
            page=1,
            include_archived=False,
            created_by_my_app=False,
            unitdp=2,
            summary_only=False,
            page_size=50,
        )

        invoices = []
        for inv in (result.invoices or []):
            # Minimal contact summary (safe getattr)
            c = getattr(inv, "contact", None)
            contact = {
                "contact_id": getattr(c, "contact_id", None),
                "name": getattr(c, "name", None),
                "email": getattr(c, "email_address", None),
                "is_customer": getattr(c, "is_customer", None),
                "is_supplier": getattr(c, "is_supplier", None),
                "status": getattr(c, "contact_status", None),
            } if c else None

            # Monetary fields
            total = getattr(inv, "total", None)
            amount_paid = getattr(inv, "amount_paid", None)
            amount_credited = getattr(inv, "amount_credited", None)
            amount_due = getattr(inv, "amount_due", None)

            # Compute a consistent "amount_remaining" (use API field if present; fall back to calc)
            if amount_due is None and None not in (total, amount_paid, amount_credited):
                amount_due_calc = (total or 0) - (amount_paid or 0) - (amount_credited or 0)
            else:
                amount_due_calc = amount_due

            invoices.append({
                "invoice_id": getattr(inv, "invoice_id", None),
                "number": getattr(inv, "invoice_number", None),
                "type": getattr(inv, "type", None),                 # e.g., ACCREC / ACCPAY
                "status": getattr(inv, "status", None),

                "date": _fmt_date(getattr(inv, "date", None)),
                "due_date": _fmt_date(getattr(inv, "due_date", None)),

                "reference": getattr(inv, "reference", None),

                "subtotal": getattr(inv, "sub_total", None),
                "total_tax": getattr(inv, "total_tax", None),
                "total": total,

                "amount_paid": amount_paid,
                "amount_credited": amount_credited,
                "amount_due": amount_due_calc,   # normalized remaining balance

                "contact": contact,
            })

        return invoices

    except AccountingBadRequestException:
        # Xero returned a 400
        return []
    except Exception:
        # Catch-all for other errors (network, token, etc.)
        return []

def get_contacts():
    tenant_id = session["xero_tenant_id"]

    try:
        result = api.get_contacts(
            xero_tenant_id=tenant_id,
            page=1,
            page_size=60,  # default is 100; keep smaller for testing
        )

        contacts = [
            {
                "contact_id": c.contact_id,
                "name": c.name,
                "email": c.email_address,
                "is_customer": c.is_customer,
                "is_supplier": c.is_supplier,
                "status": c.contact_status,
            }
            for c in result.contacts or []
        ]

        return contacts

    except AccountingBadRequestException as e:
        # Xero returned a 400
        print(f"AccountingBadRequestException: {e}")
        return []
    except Exception as e:
        # Catch-all for other errors (network, token, etc.)
        print(f"Error: {e}")
        return []

def add_statement_to_table(tenant_id: str, entry: Dict[str, str]):
    dynamo_table = ddb.Table("StatementProcessorTable")

    item = {
        "TenantID": tenant_id,
        "ContactID": entry["contact_id"],
        "ContactName": entry["contact_name"],
        "StatementID": entry["statement_id"]
    }
    try:
        dynamo_table.put_item(
            Item=item,
            ConditionExpression=Attr("TenantID").not_exists() & Attr("ContactID").not_exists(),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"Statement {entry["statement_id"]} already exists") from e
        raise

def upload_statement_to_s3(tenant_id: str, f, entry: Dict[str, str]):
    key = f"{tenant_id}/{entry["statement_id"]}"

    try:
        # Ensure stream is at start before upload
        f.stream.seek(0)
        s3.upload_fileobj(
            Fileobj=f.stream,
            Bucket="dexero-statement-processor-prod",
            Key=key,
            ExtraArgs={
                "ContentType": "application/pdf",
            },
        )
    except (BotoCoreError, ClientError) as e:
        print(f"Failed to upload '{key}' to S3: {e}")

# endregion Functions

# region Routes

@app.route('/favicon.ico')
def ignore_favicon():
    return ('', 204)  # Empty response, no content

@app.route('/contacts', methods=['GET', 'POST'])
def contacts():
    if "access_token" not in session or "xero_tenant_id" not in session:
        return redirect(url_for("index"))

    contacts = get_contacts()
    contacts = sorted(contacts, key=lambda c: c["name"].casefold())

    if request.method == 'POST':
        contact_name = request.form.get('contact_name')
        for contact in contacts:
            if contact["name"] == contact_name:
                return redirect(url_for("invoices", contact_id=contact["contact_id"]))

    return render_template("contacts.html", contacts=contacts)

@app.route("/invoices/<contact_id>")
def invoices(contact_id):
    if "access_token" not in session or "xero_tenant_id" not in session:
        return redirect(url_for("index"))

    invoices = get_invoices(contact_id)
    contact_name = invoices[0]["contact"]["name"] if invoices else None
    return render_template("invoices.html", invoices=invoices, contact_name=contact_name)

@app.route("/upload-statements", methods=['GET', 'POST'])
def upload_statements():
    if "access_token" not in session or "xero_tenant_id" not in session:
        return redirect(url_for("index"))

    errors = []
    mapping = []

    contacts = get_contacts()
    contacts = sorted(contacts, key=lambda c: c["name"].casefold())
    contact_lookup = {c["name"]: c["contact_id"] for c in contacts}

    if request.method == 'POST':
        files = [f for f in request.files.getlist('statements') if f and f.filename]
        names = [n for n in request.form.getlist('contact_names')]

        if not files:
            errors.append("Please add at least one statement (PDF).")
        elif len(files) != len(names):
            errors.append("Each statement must have a contact selected.")
        else:
            # Create statement identifier etc
            tenant_id = session.get("xero_tenant_id")

            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            for f, contact in zip(files, names):
                if not contact.strip():
                    errors.append(f"Missing contact for '{f.filename}'.")
                    continue
                if not is_allowed_pdf(f.filename, f.mimetype):
                    errors.append(f"Rejected '{f.filename}': only PDFs are allowed.")
                    continue

                fname = secure_filename(f.filename)
                save_path = UPLOAD_DIR / fname
                f.save(save_path)

                contact_id: str = contact_lookup.get(contact.strip())

                entry = {
                    "statement_id": str(uuid.uuid4()),
                    "statement": fname,
                    "contact_name": contact.strip(),
                    "contact_id": contact_id,
                }
                mapping.append(entry)
                print(entry)

                # Upload statement to S3
                upload_statement_to_s3(tenant_id, f, entry)

                # Upload statement to ddb
                add_statement_to_table(tenant_id, entry)

    return render_template(
        "upload_statements.html",
        contacts=contacts,
        mapping=mapping if mapping else None,
        errors=errors if errors else None
    )

@app.route("/")
def index():
    if "access_token" in session:
        return render_template("index.html")
    return '<a href="/login">Login with Xero</a>'

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

# endregion Routes

if __name__ == "__main__":
    app.run(port=8080, debug=True)
