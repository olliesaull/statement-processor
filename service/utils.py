import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Tuple

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import BotoCoreError, ClientError
from flask import (
    redirect,
    session,
    url_for,
)
from werkzeug.datastructures import FileStorage
from xero_python.accounting import AccountingApi
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore
from xero_python.exceptions import AccountingBadRequestException

from configuration.config import CLIENT_ID, CLIENT_SECRET, S3_BUCKET_NAME
from configuration.resources import (
    s3_client,
    tenant_contacts_config_table,
    tenant_statements_table,
)
from core.textract_statement import run_textraction

ALLOWED_EXTENSIONS = {'.pdf', '.PDF'}
SCOPES = [
    "offline_access", "openid", "profile", "email", "accounting.transactions", "accounting.reports.read", "accounting.journals.read",
    "accounting.settings", "accounting.contacts", "accounting.attachments", "assets", "projects", "files.read",
]

def scope_str():
    return " ".join(SCOPES)

def get_xero_oauth2_token():
    # Return the dict the SDK expects, or None if not set
    return session.get("xero_oauth2_token")

def save_xero_oauth2_token(token: dict):
    # Persist the whole token dict in the session (or your DB)
    session["xero_oauth2_token"] = token

api_client = ApiClient(
    Configuration(
        # debug=app.config["DEBUG"],
        oauth2_token=OAuth2Token(
            client_id=CLIENT_ID, client_secret=CLIENT_SECRET
        ),
    ),
    pool_threads=1,
    oauth2_token_getter=get_xero_oauth2_token,
    oauth2_token_saver=save_xero_oauth2_token,
)
api = AccountingApi(api_client)

def xero_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "access_token" not in session or "xero_tenant_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

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

def get_invoices_by_numbers(invoice_numbers):
    """
    Fetch invoices for a list of invoice numbers.
    Returns a dict keyed by invoice number: { "INV-001": {...}, ... }

    - Batches requests to avoid URL length limits
    - Handles paging
    - Normalizes invoice numbers to strings and strips whitespace
    - If duplicates arrive from the API, the *last* one wins (simple + predictable)
    """
    tenant_id = session["xero_tenant_id"]
    if not invoice_numbers:
        return {}

    def _fmt(inv):
        c = getattr(inv, "contact", None)
        contact = {
            "contact_id": getattr(c, "contact_id", None),
            "name": getattr(c, "name", None),
            "email": getattr(c, "email_address", None),
            "is_customer": getattr(c, "is_customer", None),
            "is_supplier": getattr(c, "is_supplier", None),
            "status": getattr(c, "contact_status", None),
        } if c else None

        total = getattr(inv, "total", None)
        amount_paid = getattr(inv, "amount_paid", None)
        amount_credited = getattr(inv, "amount_credited", None)
        amount_due = getattr(inv, "amount_due", None)
        if amount_due is None and None not in (total, amount_paid, amount_credited):
            amount_due_calc = (total or 0) - (amount_paid or 0) - (amount_credited or 0)
        else:
            amount_due_calc = amount_due

        return {
            "invoice_id": getattr(inv, "invoice_id", None),
            "number": getattr(inv, "invoice_number", None),
            "type": getattr(inv, "type", None),
            "status": getattr(inv, "status", None),
            "date": _fmt_date(getattr(inv, "date", None)),
            "due_date": _fmt_date(getattr(inv, "due_date", None)),
            "reference": getattr(inv, "reference", None),
            "subtotal": getattr(inv, "sub_total", None),
            "total_tax": getattr(inv, "total_tax", None),
            "total": total,
            "amount_paid": amount_paid,
            "amount_credited": amount_credited,
            "amount_due": amount_due_calc,
            "contact": contact,
        }

    # normalize & de-dupe while preserving order (helps batching)
    normalized = []
    seen = set()
    for n in (str(x).strip() for x in invoice_numbers if str(x).strip()):
        if n not in seen:
            seen.add(n)
            normalized.append(n)

    by_number = {}
    BATCH = 40

    try:
        for i in range(0, len(normalized), BATCH):
            batch = normalized[i:i+BATCH]
            page = 1
            while True:
                result = api.get_invoices(
                    tenant_id,
                    invoice_numbers=batch,
                    order="InvoiceNumber ASC",
                    page=page,
                    include_archived=False,
                    created_by_my_app=False,
                    unitdp=2,
                    summary_only=False,
                    page_size=50,
                )
                invs = result.invoices or []
                for inv in invs:
                    rec = _fmt(inv)
                    n = rec.get("number")
                    if n:
                        # last one wins if duplicates appear
                        by_number[n] = rec

                if len(invs) < 50:
                    break
                page += 1

        return by_number

    except AccountingBadRequestException:
        return {}
    except Exception:
        return {}

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

def get_contact_for_statement(tenant_id: str, statement_id: str):
    """Get the contact ID for a given statement ID"""
    response = tenant_statements_table.get_item(
        Key={
            "TenantID": tenant_id,
            "StatementID": statement_id
        },
        ProjectionExpression="ContactID"  # fetch only the needed attribute
    )

    item = response.get("Item")
    if item:
        return item.get("ContactID")
    return None

def get_incomplete_statements() -> list[dict]:
    """
    Return all statements for the given tenant where `complete` is not True
    (i.e., either False or attribute missing).
    """
    tenant_id = session.get("xero_tenant_id")
    items: list[dict] = []
    kwargs = {
        "KeyConditionExpression": Key("TenantID").eq(tenant_id),
        "FilterExpression": Attr("complete").not_exists() | Attr("complete").eq(False),
    }

    while True:
        resp = tenant_statements_table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    return items

def add_statement_to_table(tenant_id: str, entry: Dict[str, str]):
    item = {
        "TenantID": tenant_id,
        "StatementID": entry["statement_id"],
        "OriginalStatementFilename": entry["statement_name"],
        "ContactID": entry["contact_id"],
        "ContactName": entry["contact_name"],
    }
    try:
        tenant_statements_table.put_item(
            Item=item,
            ConditionExpression=Attr("TenantID").not_exists() & Attr("ContactID").not_exists(),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError(f"Statement {entry["statement_name"]} already exists") from e
        raise

def upload_statement_to_s3(fs_like, key: str) -> bool:
    """
    Uploads a file-like object (PDF or JSON stream) to S3.
    """
    stream = getattr(fs_like, "stream", fs_like)

    # Always reset to start
    stream.seek(0)

    try:
        s3_client.upload_fileobj(
            Fileobj=stream,
            Bucket=S3_BUCKET_NAME,
            Key=key,
        )
        return True
    except (BotoCoreError, ClientError) as e:
        print(f"Failed to upload '{key}' to S3: {e}")
        return False

def get_or_create_json_statement(tenant_id: str, contact_id: str, bucket: str, pdf_key: str, json_key: str) -> tuple[dict, FileStorage]:
    """
    Look for JSON statement in S3. If it exists, download and return it.
    Otherwise, run Textract on the PDF, upload the JSON, and return it.

    Returns:
        (data_dict, FileStorage) where:
          - data_dict is the parsed JSON object
          - FileStorage is a file-like wrapper around the JSON (for reuse)
    """
    try:
        # Check if JSON already exists
        s3_client.head_object(Bucket=bucket, Key=json_key)
        print(f"Found existing JSON at {json_key}, downloading...")
        obj = s3_client.get_object(Bucket=bucket, Key=json_key)
        json_bytes = obj["Body"].read()
        data = json.loads(json_bytes.decode("utf-8"))
        fs = FileStorage(stream=io.BytesIO(json_bytes), filename=json_key.rsplit("/", 1)[-1])
        return data, fs
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise  # some other error, not "Not Found"

    # Not found → run Textract
    print(f"No JSON at {json_key}, running Textract for {pdf_key}...")
    json_fs = run_textraction(bucket=S3_BUCKET_NAME, keys=[pdf_key], tenant_id=tenant_id, contact_id=contact_id)
    json_fs.stream.seek(0)
    json_bytes = json_fs.stream.read()

    # Parse
    data = json.loads(json_bytes.decode("utf-8"))

    # Upload new JSON
    upload_statement_to_s3(io.BytesIO(json_bytes), json_key)

    # Return both
    fs = FileStorage(stream=io.BytesIO(json_bytes), filename=json_key.rsplit("/", 1)[-1])
    return data, fs

def get_contact_config(tenant_id: str, contact_id: str) -> Dict[str, Any]:
    """
    Fetch config from DynamoDB.

    :param tenant_id: TenantID partition key value
    :param contact_id: ContactID sort key value
    :return: Config dict
    """
    attr_name = "config"
    try:
        resp = tenant_contacts_config_table.get_item(
            Key={"TenantID": tenant_id, "ContactID": contact_id},
            ProjectionExpression="#cfg",
            ExpressionAttributeNames={"#cfg": attr_name},
        )
    except ClientError as e:
        raise RuntimeError(f"DynamoDB error fetching config: {e}")

    item = resp.get("Item")
    if not item or attr_name not in item:
        raise KeyError(f"Config not found for TenantID={tenant_id}, ContactID={contact_id}")

    cfg = item[attr_name]
    if not isinstance(cfg, dict):
        raise TypeError(f"Config attribute '{attr_name}' is not a dict: {type(cfg)}")

    return cfg

def _norm_number(x):
    """Return Decimal if x looks numeric (incl. currency/commas); else None."""
    if x is None:
        return None
    if isinstance(x, (int, float, Decimal)):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return None
    s = str(x).strip()
    if not s:
        return None
    # strip currency symbols/letters, keep digits . , -
    s = re.compile(r"[^\d\-\.,]").sub("", s).replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None

def equal(a, b):
    """Numeric-aware equality; otherwise trimmed string equality."""
    da, db = _norm_number(a), _norm_number(b)
    if da is not None or db is not None:
        return da == db
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    return sa == sb

def norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()

def clean_number_str(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).replace(",", "")

def to_number_if_possible(s: str):
    t = clean_number_str(s)
    if t == "":
        return ""
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return s.strip()

def best_header_row(grid: List[List[str]], candidate_headers: List[str], lookahead: int = 5) -> Tuple[int, List[str]]:
    cand = set(norm(h) for h in candidate_headers if h)
    if not cand:
        for idx, row in enumerate(grid):
            if any(c.strip() for c in row):
                return idx, row
        return 0, grid[0] if grid else []
    best_idx, best_score = 0, -1
    for i in range(min(lookahead, len(grid))):
        row = grid[i]
        score = 0
        for cell in row:
            cn = norm(cell)
            if cn and (cn in cand or any(c in cn or cn in c for c in cand)):
                score += 1
        if score > best_score:
            best_score, best_idx = score, i
    return best_idx, grid[best_idx]

def build_col_index(header_row: List[str]) -> Dict[str, int]:
    col_index: Dict[str, int] = {}
    for i, h in enumerate(header_row):
        hn = norm(h)
        if hn and hn not in col_index:
            col_index[hn] = i
    return col_index

def get_by_header(row: List[str], col_index: Dict[str, int], header_label: str) -> str:
    if not header_label:
        return ""
    idx = col_index.get(norm(header_label))
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()

# --- carried/brought-forward skipper ---
def _looks_money(s: str) -> bool:
    t = clean_number_str(s)
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t)) if t else False

def _is_forward_label(text: str) -> bool:
    t = re.sub(r"[^a-z0-9 ]+", "", (norm(text) or ""))
    if not t:
        return False
    keywords = (
        "brought forward", "carried forward", "opening balance", "opening bal",
        "previous balance", "balance forward", "balance bf", "balance b f", "bal bf", "bal b f",
    )
    short_forms = {"bf", "b f", "bfwd", "b fwd", "cf", "c f", "cfwd", "c fwd"}
    return t in short_forms or any(k in t for k in keywords)

def row_is_opening_or_carried_forward(raw_row: List[str], mapped_item: Dict[str, Any]) -> bool:
    """
    Heuristics:
      - Contains a forward-like label in document_type / description_details / any raw cell
      - Very sparse row (<= 3 non-empty cells) AND only one money value present
        AND no useful identifiers (doc/customer/supplier refs)
    """
    if _is_forward_label(mapped_item.get("document_type", "")) or _is_forward_label(mapped_item.get("description_details", "")):
        return True
    raw = mapped_item.get("raw") or {}
    if isinstance(raw, dict) and any(_is_forward_label(v) for v in raw.values() if v):
        return True
    non_empty = sum(1 for c in raw_row if (c or "").strip())
    money_count = sum(1 for c in raw_row if _looks_money(c))
    ids_empty = all(not (mapped_item.get(k) or "").strip() for k in ("supplier_reference", "customer_reference"))
    doc_like_empty = all(not (mapped_item.get(k) or "").strip() for k in ("document_type", "description_details"))
    return non_empty <= 3 and money_count <= 1 and ids_empty and doc_like_empty
