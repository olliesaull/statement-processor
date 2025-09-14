import io
import json
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import difflib
from decimal import Decimal, InvalidOperation
import re

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
from core.transform import equal
from core.date_utils import (
    format_iso_to_template,
    parse_date_with_template,
    ensure_abbrev_month,
)
from configuration.resources import (
    s3_client,
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
                # Exclude deleted invoices explicitly via Status filter
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
                    statuses=["DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"],
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

    except AccountingBadRequestException as e:
        print(f"Exception occured: {e}")
        return {}
    except Exception as e:
        print(f"Exception occured: {e}")
        return {}


def get_invoices_by_contact(contact_id):
    tenant_id = session["xero_tenant_id"]

    try:
        # Restrict to the contact and exclude deleted invoices
        result = api.get_invoices(
            tenant_id,
            where=f'Contact.ContactID==Guid("{contact_id}") AND Status!="DELETED"',
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

    except AccountingBadRequestException as e:
        print(f"Exception occured: {e}")
        return []
    except Exception as e:
        print(f"Exception occured: {e}")
        return []


def get_credit_notes_by_contact(contact_id):
    tenant_id = session["xero_tenant_id"]

    try:
        result = api.get_credit_notes(
            tenant_id,
            where=f'Contact.ContactID==Guid("{contact_id}")',
            order="CreditNoteNumber ASC",
            page=1,
            unitdp=2,
            page_size=50,
        )

        credit_notes = []
        for cn in (result.credit_notes or []):
            c = getattr(cn, "contact", None)
            contact = {
                "contact_id": getattr(c, "contact_id", None),
                "name": getattr(c, "name", None),
                "email": getattr(c, "email_address", None),
                "is_customer": getattr(c, "is_customer", None),
                "is_supplier": getattr(c, "is_supplier", None),
                "status": getattr(c, "contact_status", None),
            } if c else None

            total = getattr(cn, "total", None)
            amount_paid = getattr(cn, "amount_paid", None)
            amount_credited = getattr(cn, "amount_credited", None)
            remaining_credit = getattr(cn, "remaining_credit", None)

            credit_notes.append({
                "credit_note_id": getattr(cn, "credit_note_id", None),
                "number": getattr(cn, "credit_note_number", None),
                "type": getattr(cn, "type", None),                 # e.g., ACCRECCREDIT
                "status": getattr(cn, "status", None),

                "date": _fmt_date(getattr(cn, "date", None)),
                "due_date": _fmt_date(getattr(cn, "due_date", None)),

                "reference": getattr(cn, "reference", None),

                "subtotal": getattr(cn, "sub_total", None),
                "total_tax": getattr(cn, "total_tax", None),
                "total": total,

                "amount_paid": amount_paid,
                "amount_credited": amount_credited,
                # For credit notes, carry remaining_credit as amount_due analogue if present
                "amount_due": remaining_credit,
                "remaining_credit": remaining_credit,

                "contact": contact,
            })

        return credit_notes

    except AccountingBadRequestException as e:
        print(f"Exception occured: {e}")
        return []
    except Exception as e:
        print(f"Exception occured: {e}")
        return []


def get_contacts():
    tenant_id = session["xero_tenant_id"]

    try:
        # Explicitly exclude archived contacts (treat as deleted/hidden)
        result = api.get_contacts(
            xero_tenant_id=tenant_id,
            page=1,
            include_archived=False,
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

        # Backfill statement_date_format for existing JSON if missing
        try:
            from core.get_contact_config import get_contact_config  # local import to avoid cycles
            cfg = get_contact_config(tenant_id, contact_id) if contact_id else {}
            fmt = cfg.get("statement_date_format") if isinstance(cfg, dict) else None
        except Exception:
            fmt = None

        mutated = False
        if fmt:
            items = data.get("statement_items") or []
            for it in items:
                if isinstance(it, dict) and not it.get("statement_date_format"):
                    it["statement_date_format"] = fmt
                    mutated = True
        if mutated:
            # Persist back to S3 and rebuild JSON bytes
            json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            upload_statement_to_s3(io.BytesIO(json_bytes), json_key)

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


# -----------------------------
# Helpers for statement view
# -----------------------------

def _to_decimal(x: Any) -> Optional[Decimal]:
    if x is None or x == "":
        return None
    if isinstance(x, (int, float, Decimal)):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return None
    s = str(x).strip()
    if not s:
        return None
    # strip currency symbols/letters; keep digits . , -
    s = re.compile(r"[^\d\-\.,]").sub("", s).replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def format_money(x: Any) -> str:
    """Format a number with thousands separators and 2 decimals.

    Returns empty string for empty input; returns original string if not numeric.
    """
    d = _to_decimal(x)
    if d is None:
        return "" if x in (None, "") else str(x)
    return f"{d:,.2f}"

def get_items_template_from_config(contact_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return the items template mapping from a contact config.

    Supports three shapes for backward/forward compatibility:
      - Legacy:   contact_config["statement_items"] is a 1-item list of dict.
      - Nested:   contact_config["statement_items"] is a dict.
      - Flattened: the template keys live directly at the root of contact_config.
    """
    if not isinstance(contact_config, dict):
        return {}

    cfg = contact_config.get("statement_items")
    if isinstance(cfg, dict):
        return cfg
    if isinstance(cfg, list) and cfg:
        first = cfg[0]
        return first if isinstance(first, dict) else {}

    # Flattened/root form: assume the root dict itself is the template mapping
    return contact_config


def prepare_display_mappings(items: List[Dict], contact_config: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, str]], Dict[str, str], Optional[str]]:
    """
    Build the display headers, filtered left rows, header->invoice_field map,
    and detect which header corresponds to the invoice "number".

    Returns: (display_headers, rows_by_header, header_to_field, item_number_header)
    """
    # Derive raw headers from the JSON statement (order preserved)
    raw_headers = list(items[0].get("raw", {}).keys()) if items else []

    # Invert the mapping: statement header -> invoice field (e.g., "total" -> "total")
    items_template = get_items_template_from_config(contact_config)
    header_to_field: Dict[str, str] = {}
    # Simple (string) mappings first
    for canonical_field, mapped in (items_template or {}).items():
        if canonical_field in {"raw", "statement_date_format"}:
            continue
        if isinstance(mapped, str) and mapped.strip():
            header_to_field[mapped.strip()] = canonical_field
    # Special case: amount_due can be a list of possible headers; include all candidates.
    # At render time we will populate only the header that has a left-side value for that row.
    mapped_amount_due = (items_template or {}).get("amount_due")
    if isinstance(mapped_amount_due, list) and mapped_amount_due:
        for h in mapped_amount_due:
            if isinstance(h, str) and h.strip():
                header_to_field[h.strip()] = "amount_due"

    # Only display headers present in the mapping
    display_headers = [h for h in raw_headers if h in header_to_field]

    # Convert raw rows into dicts filtered by display headers, normalizing date fields for display
    rows_by_header: List[Dict[str, str]] = []
    stmt_date_fmt: Optional[str] = None
    if isinstance(contact_config, dict):
        # Prefer explicit statement_date_format stored at root
        stmt_date_fmt = contact_config.get("statement_date_format")
    # Prefer abbreviated month form for display to save space
    display_date_fmt = ensure_abbrev_month(stmt_date_fmt)
    numeric_fields = {"total", "amount_paid", "amount_due"}
    for it in items:
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        row: Dict[str, str] = {}
        for h in display_headers:
            v = raw.get(h, "")
            # If this header maps to a canonical date field, normalize to the configured format
            canon = header_to_field.get(h)
            if canon in {"date", "due_date"} and display_date_fmt:
                dt = parse_date_with_template(v, display_date_fmt)
                if dt is not None:
                    v = format_iso_to_template(dt, display_date_fmt)
            elif canon in numeric_fields:
                v = format_money(v)
            row[h] = v
        rows_by_header.append(row)

    # Identify which header maps to the canonical "number" field
    item_number_header: Optional[str] = None
    for canonical_field, mapped in (items_template or {}).items():
        if canonical_field == "number" and isinstance(mapped, str) and mapped in display_headers:
            item_number_header = mapped
            break

    return display_headers, rows_by_header, header_to_field, item_number_header


def match_invoices_to_statement_items(items: List[Dict], rows_by_header: List[Dict[str, str]], item_number_header: Optional[str], invoices: List[Dict]) -> Dict[str, Dict]:
    """
    Build mapping from statement invoice number -> { invoice, statement_item, match_type, match_score, matched_invoice_number }.
    Performs an exact pass first, then fuzzy-matches any remaining numbers against the
    provided invoice list.
    """
    matched: Dict[str, Dict] = {}
    if not item_number_header:
        return matched

    # Build fast lookup for statement items by their displayed invoice number
    stmt_by_number: Dict[str, Dict] = {}
    for it in items:
        raw = it.get("raw", {}) if isinstance(it, dict) else {}
        num = raw.get(item_number_header, "")
        if not num:
            continue
        key = str(num).strip()
        if key:
            stmt_by_number[key] = it

    # 1) Exact matches
    used_invoice_ids: set = set()
    used_invoice_numbers: set = set()
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        key = str(inv_no).strip()
        if not key:
            continue
        stmt_item = stmt_by_number.get(key)
        if stmt_item is not None and key not in matched:
            matched[key] = {
                "invoice": inv,
                "statement_item": stmt_item,
                "match_type": "exact",
                "match_score": 1.0,
                "matched_invoice_number": key,
            }
            print(f"Exact match: statement number '{key}' -> invoice '{key}'")
            # Track used invoice to exclude from fuzzy matching pool
            inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
            if inv_id:
                used_invoice_ids.add(inv_id)
            used_invoice_numbers.add(key)

    # 2) Fuzzy matches for any unmatched numbers
    def _norm_num(s: str) -> str:
        s = str(s or "").upper().strip()
        return "".join(ch for ch in s if ch.isalnum())

    candidates = []
    for inv in invoices or []:
        inv_no = inv.get("number") if isinstance(inv, dict) else None
        if not inv_no:
            continue
        inv_no_str = str(inv_no).strip()
        if not inv_no_str:
            continue
        # Exclude invoices already matched exactly
        if (inv.get("invoice_id") if isinstance(inv, dict) else None) in used_invoice_ids:
            continue
        if inv_no_str in used_invoice_numbers:
            continue
        candidates.append((inv_no_str, inv, _norm_num(inv_no_str)))

    numbers_in_rows = [
        (r.get(item_number_header) or "").strip() for r in rows_by_header if r.get(item_number_header)
    ]
    missing = [n for n in numbers_in_rows if n and n not in matched]

    for key in missing:
        stmt_item = stmt_by_number.get(key)
        if stmt_item is None:
            continue
        target_norm = _norm_num(key)
        best = None
        best_ratio = -1.0
        for cand_no, inv, cand_norm in candidates:
            # Skip candidates already used by previous matches (exact or fuzzy)
            inv_id = inv.get("invoice_id") if isinstance(inv, dict) else None
            if inv_id in used_invoice_ids or cand_no in used_invoice_numbers:
                continue
            if target_norm and cand_norm and (target_norm == cand_norm):
                ratio = 1.0
            elif target_norm and cand_norm and (target_norm in cand_norm or cand_norm in target_norm):
                ratio = 0.95
            else:
                ratio = difflib.SequenceMatcher(None, target_norm, cand_norm).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = (cand_no, inv)

        if best and best_ratio >= 0.75:
            inv_no_best, inv_obj = best
            matched[key] = {
                "invoice": inv_obj,
                "statement_item": stmt_item,
                "match_type": "fuzzy" if best_ratio < 1.0 else "exact",
                "match_score": round(best_ratio, 3),
                "matched_invoice_number": inv_no_best,
            }
            kind = "Exact" if best_ratio == 1.0 else "Fuzzy"
            print(f"{kind} match: statement number '{key}' -> invoice '{inv_no_best}' (score {best_ratio:.3f})")
            # Mark this invoice as used to prevent reuse in subsequent fuzzy matches
            inv_id = inv_obj.get("invoice_id") if isinstance(inv_obj, dict) else None
            if inv_id:
                used_invoice_ids.add(inv_id)
            used_invoice_numbers.add(inv_no_best)
        else:
            print(f"No match for statement number '{key}'")

    return matched


def build_right_rows(
    rows_by_header: List[Dict[str, str]],
    display_headers: List[str],
    header_to_field: Dict[str, str],
    matched_map: Dict[str, Dict],
    item_number_header: Optional[str],
    statement_date_format: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Using the matched map, build the right-hand table rows with values from
    the invoice, aligned to the same display headers and row order as the left.
    """
    right_rows = []
    # Prefer abbreviated month for display
    display_date_fmt = ensure_abbrev_month(statement_date_format)
    numeric_fields = {"total", "amount_paid", "amount_due"}

    for r in rows_by_header:
        inv_no = (r.get(item_number_header) or "").strip() if item_number_header else ""
        rec = (matched_map.get(inv_no, {}) or {})
        inv = rec.get("invoice", {}) if isinstance(rec, dict) else {}

        # Prefer amount sources by semantic: totals for debit/credit columns, amount_due for balances
        inv_total = inv.get("total")
        inv_due = inv.get("amount_due")

        row_right = {}
        for h in display_headers:
            invoice_field = header_to_field.get(h)
            if not invoice_field:
                row_right[h] = ""
                continue

            if invoice_field == "amount_due":
                # Per-row: only populate the header that has a value on the left side
                left_val = r.get(h)
                if left_val is not None and str(left_val).strip():
                    hn = str(h or "").strip().lower()
                    # If this header looks like 'Total', align to Xero total; otherwise align to amount_due
                    if "total" in hn and "due" not in hn and "balance" not in hn:
                        row_right[h] = format_money(inv_total) if inv_total is not None else ""
                    else:
                        row_right[h] = format_money(inv_due) if inv_due is not None else ""
                else:
                    row_right[h] = ""
            elif invoice_field in {"due_date", "date"}:
                v = inv.get(invoice_field)
                row_right[h] = (
                    format_iso_to_template(v, display_date_fmt) if v is not None else ""
                )
            else:
                val = inv.get(invoice_field, "")
                if invoice_field in numeric_fields:
                    row_right[h] = format_money(val)
                else:
                    row_right[h] = val

        right_rows.append(row_right)

    return right_rows


def build_row_matches(left_rows: List[Dict[str, str]], right_rows: List[Dict[str, str]], display_headers: List[str]) -> List[bool]:
    """
    Compare each left/right row cell-wise using numeric-aware equality and
    return a list of row match booleans for UI highlighting.
    """
    row_matches: List[bool] = []
    for left, right in zip(left_rows, right_rows):
        ok = True
        for h in display_headers:
            if not equal(left.get(h), right.get(h)):
                ok = False
                break
        row_matches.append(ok)
    return row_matches


# ---------------------------------
# Type inference
# ---------------------------------

def guess_statement_item_type(raw_row: Dict[str, Any]) -> str:
    """
    Guess the document type for a statement row. Do a fuzzy match against a finite set of allowed types (invoice, credit_note, sales_order),
    using the contact's mapping config to find which header contains the document-type label. If that label is empty, fall back to the invoice number header.

    Returns the canonical type string. Also prints what it inferred for now.
    """
    # Use the entire raw row text as the label context
    label = " ".join(str(v) for v in (raw_row or {}).values() if v)

    # Canonical types and some common synonyms/aliases
    TYPE_SYNONYMS: Dict[str, List[str]] = {
        "invoice": ["invoice", "inv", "tax invoice", "taxinvoice"],
        "credit_note": ["credit note", "creditnote", "credit", "crn", "cn"],
        "sales_order": ["sales order", "salesorder", "order", "so", "s/o"],
    }

    def _norm(s: str) -> str:
        s = str(s or "").upper().strip()
        return "".join(ch for ch in s if ch.isalnum())

    label_norm = _norm(label)

    best_type = "invoice"  # default
    best_score = -1.0

    for t, syns in TYPE_SYNONYMS.items():
        for syn in syns:
            syn_norm = _norm(syn)
            if not label_norm and syn_norm:
                score = 0.0
            elif label_norm == syn_norm:
                score = 1.0
            elif label_norm and syn_norm and (label_norm in syn_norm or syn_norm in label_norm):
                score = 0.95
            else:
                score = difflib.SequenceMatcher(None, label_norm, syn_norm).ratio()
            if score > best_score:
                best_score = score
                best_type = t

    # Temporary: print the guess for visibility
    return best_type
