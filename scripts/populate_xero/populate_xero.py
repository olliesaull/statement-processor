#!/usr/bin/env python3
# ruff: noqa: E402
"""
Populate Xero from a supplier statement JSON already in S3.

Configuration: set environment variables to match the service app.
  - AWS_PROFILE, AWS_REGION, S3_BUCKET_NAME
  - TENANT_CONTACTS_CONFIG_TABLE_NAME, TENANT_STATEMENTS_TABLE_NAME
  - XERO_CLIENT_ID, XERO_CLIENT_SECRET
  - XERO_TOKEN_PATH (default: ~/.xero_token.json)
  - XERO_TENANT_ID (optional; auto-discovered if missing)
  - XERO_ACCOUNT_CODE_EXPENSE (for bills, e.g. an expense/purchases account code)
  - XERO_ACCOUNT_CODE_REVENUE (for sales invoices, e.g. a revenue account code)

Defaults below are set for the specific request but can be overridden with env vars:
  - TENANT_ID, STATEMENT_ID, CONTACT_ID

Notes:
  - A valid Xero OAuth token with offline_access must be present at XERO_TOKEN_PATH.
    You can obtain this by authenticating once via the service UI and then saving the
    resulting token dict to a file, or by any other mechanism you prefer. The script
    will refresh and persist tokens automatically via the SDK once seeded.
  - The script is idempotent using Invoice/CreditNote Reference = "stmt:{STATEMENT_ID}:row:{index}".
    Re-running will skip rows already created.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.parse
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import requests
from dotenv import load_dotenv

# Allow importing helpers from the service directory
THIS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = (THIS_DIR.parent.parent / "service").resolve()
SERVICE_ENV_PATH = SERVICE_DIR / ".env"
LOCAL_ENV_PATH = THIS_DIR / ".env"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

# Load shared service environment first, then apply local overrides if present.
load_dotenv(SERVICE_ENV_PATH)
load_dotenv(LOCAL_ENV_PATH, override=True)

from config import (  # type: ignore
    AWS_PROFILE,
    AWS_REGION,
    S3_BUCKET_NAME,
    CLIENT_ID as CONFIG_XERO_CLIENT_ID,
    CLIENT_SECRET as CONFIG_XERO_CLIENT_SECRET,
)
from core.get_contact_config import get_contact_config  # type: ignore
from utils import (  # type: ignore
    guess_statement_item_type,
    get_items_template_from_config,
)
from xero_python.accounting import (  # type: ignore
    AccountingApi,
    CreditNote,
    CreditNotes,
    Invoice,
    Invoices,
    LineItem,
    LineAmountTypes,
)
from xero_python.accounting import (
    Contact as XeroContact,
)
from xero_python.api_client import ApiClient  # type: ignore
from xero_python.api_client.configuration import Configuration  # type: ignore
from xero_python.api_client.oauth2 import OAuth2Token  # type: ignore
from xero_python.exceptions import AccountingBadRequestException  # type: ignore

# ---------------------
# Global settings
# ---------------------

TENANT_ID = os.getenv("TENANT_ID", "80985eea-b0fe-4b9e-a6f8-787e0b017ce9")
# ButtaNut
# STATEMENT_ID = os.getenv("STATEMENT_ID", "0de99b0d-6b5e-4f64-b548-8b9a4b477e21")
# CONTACT_ID = os.getenv("CONTACT_ID", "d187a088-978e-46c3-9bb9-26f3c3961e51")

# Geotina
# STATEMENT_ID = os.getenv("STATEMENT_ID", "4a62b4c1-9bd3-45d2-a94f-3437dee55feb")
# CONTACT_ID = os.getenv("CONTACT_ID", "d67b0b6f-ed25-4591-8b84-e3b76a390d2a")

# Sapuma
# STATEMENT_ID = os.getenv("STATEMENT_ID", "eae79b39-f527-4c39-abd8-5fe39c7c8a1c")
# CONTACT_ID = os.getenv("CONTACT_ID", "a9dcc903-5ecb-45b0-b02f-818244d531d2")

# SimplePay
STATEMENT_ID = os.getenv("STATEMENT_ID", "a12917e0-3253-4d4d-9a3f-dabbb5ff92f9")
CONTACT_ID = os.getenv("CONTACT_ID", "94975100-c0c5-4330-af86-20f8f180e8f4")

XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID") or CONFIG_XERO_CLIENT_ID
XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET") or CONFIG_XERO_CLIENT_SECRET
XERO_TOKEN_PATH = Path(os.getenv("XERO_TOKEN_PATH", str(Path.home() / ".xero_token.json")))
XERO_TENANT_ID = os.getenv("XERO_TENANT_ID")  # optional; discover if missing
XERO_REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", "http://localhost:8080/callback")

# Optional account code overrides
XERO_ACCOUNT_CODE_EXPENSE = os.getenv("XERO_ACCOUNT_CODE_EXPENSE")
XERO_ACCOUNT_CODE_REVENUE = os.getenv("XERO_ACCOUNT_CODE_REVENUE")

# Behavior flags
STATUS = os.getenv("XERO_DOC_STATUS", "DRAFT").upper()  # DRAFT or SUBMITTED or AUTHORISED
LINE_AMOUNT_TYPES = os.getenv("XERO_LINE_AMOUNT_TYPES", "Inclusive")  # NoTax/Exclusive/Inclusive
TAX_TYPE = os.getenv("XERO_TAX_TYPE", "NONE")
FORCE_CREATE = os.getenv("XERO_FORCE_CREATE", "").strip().lower() in {"1", "true", "yes"}
SKIP_NUMBER = os.getenv("XERO_SKIP_NUMBER", "").strip().lower() in {"1", "true", "yes"}
# Choose document side (sale vs purchase) via env or auto-detect from contact
DOC_SIDE_ENV = os.getenv("XERO_DOC_SIDE", "sale").strip().lower()  # 'sale' or 'purchase'
IS_SALE_FLAG = os.getenv("XERO_IS_SALE", "").strip().lower() in {"1", "true", "yes"}

# Xero OAuth endpoints and scopes (align with service)
AUTH_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
SCOPES = [
    "offline_access", "openid", "profile", "email", "accounting.transactions", "accounting.reports.read", "accounting.journals.read",
    "accounting.settings", "accounting.contacts", "accounting.attachments", "assets", "projects", "files.read",
]

ALLOWED_TOKEN_KEYS = {
    "access_token",
    "refresh_token",
    "expires_in",
    "expires_at",
    "token_type",
    "scope",
    "id_token",
}


# ---------------------
# Utilities
# ---------------------

def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def clean_number_str(s: Any) -> str:
    t = "" if s is None else str(s)
    return t.replace(",", "").replace(" ", "").strip()


def to_number(s: Any) -> Optional[Decimal]:
    t = clean_number_str(s)
    if not t:
        return None
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_date_tokenized(value: str, template: Optional[str]) -> Optional[date]:
    if not value:
        return None
    if not template:
        # Try ISO first
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except Exception:
            pass
        # Try a few simple fallbacks
        for f in ("%d/%m/%y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(value, f).date()
            except Exception:
                continue
        return None

    # Normalize tokens to uppercase to support configs like 'dd/mm/yy'
    fmt = (template or "").upper()
    # Map tokens to strptime
    mappings = (
        ("YYYY", "%Y"),
        ("YY", "%y"),
        ("MMMM", "%B"),
        ("MMM", "%b"),
        ("MM", "%m"),  # %m accepts 1-2 digits
        ("M", "%m"),
        ("DD", "%d"),  # %d accepts 1-2 digits
        ("D", "%d"),
    )
    for k, v in mappings:
        fmt = fmt.replace(k, v)
    # Try main format first
    try:
        return datetime.strptime(value, fmt).date()
    except Exception:
        pass
    # Fallback: if template used full month but value is abbreviated, try %b
    try:
        tmpl_upper = (template or "").upper()
        if "MMMM" in tmpl_upper and "MMM" not in tmpl_upper:
            alt = tmpl_upper.replace("MMMM", "MMM")
            alt_fmt = alt
            for k, v in mappings:
                alt_fmt = alt_fmt.replace(k, v)
            return datetime.strptime(value, alt_fmt).date()
    except Exception:
        pass
    # Last resort: loose parse "D Mon YYYY" or "D Month YYYY" with English months
    try:
        import re
        m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\s*$", value)
        if m:
            d = int(m.group(1))
            mon = m.group(2).strip().lower()
            y = int(m.group(3))
            months = {
                "jan": 1, "january": 1,
                "feb": 2, "february": 2,
                "mar": 3, "march": 3,
                "apr": 4, "april": 4,
                "may": 5,
                "jun": 6, "june": 6,
                "jul": 7, "july": 7,
                "aug": 8, "august": 8,
                "sep": 9, "sept": 9, "september": 9,
                "oct": 10, "october": 10,
                "nov": 11, "november": 11,
                "dec": 12, "december": 12,
            }
            mm = months.get(mon)
            if mm:
                return date(y, mm, d)
    except Exception:
        pass
    return None


def load_token() -> dict:
    if not XERO_TOKEN_PATH.exists():
        # try bootstrap
        print(f"No token at {XERO_TOKEN_PATH}. Starting one-time OAuth bootstrap…")
        bootstrap_auth()
    with XERO_TOKEN_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_token(tok: dict) -> None:
    XERO_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    with XERO_TOKEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(tok, f)


def build_xero_client() -> tuple[AccountingApi, ApiClient, str]:
    if not XERO_CLIENT_ID or not XERO_CLIENT_SECRET:
        die("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in env.")

    tokens = load_token()

    # Keep only OAuth fields for SDK consumption
    token_core = {k: v for k, v in (tokens or {}).items() if k in ALLOWED_TOKEN_KEYS}

    def _getter():
        return token_core

    def _saver(tok: dict):
        if isinstance(tok, dict):
            for k, v in tok.items():
                if k in ALLOWED_TOKEN_KEYS:
                    tokens[k] = v
                    token_core[k] = v
        save_token(tokens)

    api_client = ApiClient(
        Configuration(oauth2_token=OAuth2Token(client_id=XERO_CLIENT_ID, client_secret=XERO_CLIENT_SECRET)),
        pool_threads=1,
        oauth2_token_getter=_getter,
        oauth2_token_saver=_saver,
    )
    api_client.set_oauth2_token(token_core)

    api = AccountingApi(api_client)

    tenant_id = XERO_TENANT_ID or tokens.get("xero_tenant_id")
    if not tenant_id:
        at = tokens.get("access_token")
        if not at:
            die("Token file missing access_token.")
        resp = requests.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {at}"},
            timeout=20,
        )
        resp.raise_for_status()
        conns = resp.json() or []
        if not conns:
            die("No Xero connections found for this token.")
        tenant_id = conns[0]["tenantId"]
        # Store tenant id alongside but do not include it in OAuth token payload
        tokens["xero_tenant_id"] = tenant_id
        save_token(tokens)

    return api, api_client, tenant_id


def bootstrap_auth() -> None:
    if not XERO_CLIENT_ID or not XERO_CLIENT_SECRET:
        die("Set XERO_CLIENT_ID and XERO_CLIENT_SECRET in env for OAuth bootstrap.")
    # Build auth URL
    scope_str = " ".join(SCOPES)
    # A simple state for copy/paste flow (not validated here)
    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": XERO_CLIENT_ID,
        "redirect_uri": XERO_REDIRECT_URI,
        "scope": scope_str,
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("\n1) Open this URL in your browser and complete login:")
    print(url)
    print("\n2) After consenting, you will be redirected to:")
    print(XERO_REDIRECT_URI)
    print("\n3) Copy the full redirected URL and paste it below.\n")
    redirected = input("Paste the full redirect URL: ").strip()
    if "?" not in redirected:
        die("That does not look like a valid redirect URL containing a code.")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(redirected).query)
    code = (q.get("code") or [None])[0]
    if not code:
        die("No authorization code found in the URL.")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": XERO_REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    resp = requests.post(TOKEN_URL, data=data, headers=headers, auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET))
    if resp.status_code != 200:
        die(f"Token exchange failed: {resp.status_code} {resp.text}")
    tokens = resp.json()
    save_token(tokens)
    print(f"Saved token to {XERO_TOKEN_PATH}\n")


def discover_account_code(api: AccountingApi, tenant_id: str, for_expense: bool) -> Optional[str]:
    # Honor explicit env overrides first
    if for_expense and XERO_ACCOUNT_CODE_EXPENSE:
        return XERO_ACCOUNT_CODE_EXPENSE
    if (not for_expense) and XERO_ACCOUNT_CODE_REVENUE:
        return XERO_ACCOUNT_CODE_REVENUE

    # Try to pick a sensible active account
    try:
        if for_expense:
            where = 'Type=="EXPENSE" && Status=="ACTIVE"'
        else:
            where = 'Type=="REVENUE" && Status=="ACTIVE"'
        accounts = api.get_accounts(tenant_id, where=where)
        for acc in (accounts.accounts or []):
            code = getattr(acc, "code", None)
            if code:
                return code
    except Exception:
        pass
    return None


def _get_contact_role(api: AccountingApi, tenant_id: str, contact_id: str) -> tuple[Optional[bool], Optional[bool]]:
    """Return (is_customer, is_supplier) for the contact if available."""
    try:
        res = api.get_contacts(
            xero_tenant_id=tenant_id,
            where=f'ContactID==Guid("{contact_id}")',
            page_size=1,
        )
        c = (res.contacts or [None])[0]
        if c is None:
            return None, None
        return getattr(c, "is_customer", None), getattr(c, "is_supplier", None)
    except Exception:
        return None, None


def resolve_expense_side(api: AccountingApi, tenant_id: str, contact_id: str) -> bool:
    """Decide whether to create purchases (ACCPAY=True) or sales (ACCREC=False).

    Order of precedence:
      1) XERO_DOC_SIDE env: 'sale' or 'purchase'
      2) XERO_IS_SALE env: boolean
      3) Contact flags: is_customer/is_supplier
      4) Default to sales (invoice)
    """
    if DOC_SIDE_ENV in {"sale", "sales", "accrec"}:
        return False
    if DOC_SIDE_ENV in {"purchase", "purchases", "accpay", "bill", "bills"}:
        return True
    if IS_SALE_FLAG:
        return False
    # Try contact metadata
    is_customer, is_supplier = _get_contact_role(api, tenant_id, contact_id)
    if is_customer is True and (is_supplier is not True):
        return False
    if is_supplier is True and (is_customer is not True):
        return True
    # Default: create sales invoices
    return False


def get_statement_json(session: boto3.session.Session, bucket: str, tenant_id: str, statement_id: str) -> Dict[str, Any]:
    key = f"{tenant_id}/{statement_id}.json"
    s3 = session.client("s3")
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except Exception as e:
        die(f"Failed to fetch s3://{bucket}/{key}: {e}")
    body = obj["Body"].read()
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        die(f"Invalid JSON in s3://{bucket}/{key}: {e}")
    return data


def pick_amount(it: Dict[str, Any], doc_type: str) -> Optional[Decimal]:
    """Pick a sensible amount for the document from canonical fields.

    Priority:
      - total (numeric)
      - amount_due:
          * if numeric, use it
          * if string/list of strings, treat each as a raw header name
            (e.g. "debit", "credit") and use the first numeric value
            found in the item's raw payload under that header.
    Returns the Decimal found, preserving its sign.
    """
    total = to_number(it.get("total"))

    def _resolve_token(tok: Any, _seen: Optional[set[int]] = None) -> Optional[Decimal]:
        # direct numeric
        n = to_number(tok)
        if n is not None:
            return n
        if _seen is None:
            _seen = set()
        # Avoid infinite recursion on nested structures
        obj_id = id(tok)
        if obj_id in _seen:
            return None
        _seen.add(obj_id)

        # handle mapping containers (e.g. {"Debit": 123, "Credit": ""})
        if isinstance(tok, dict):
            # Prefer values first, then fall back to keys as header tokens
            for value in tok.values():
                num = _resolve_token(value, _seen)
                if num is not None and num != 0:
                    return num
            for key in tok.keys():
                num = _resolve_token(key, _seen)
                if num is not None and num != 0:
                    return num
            return None

        # handle iterables of potential tokens (lists/tuples/sets)
        if isinstance(tok, (list, tuple, set)):
            for item in tok:
                num = _resolve_token(item, _seen)
                if num is not None and num != 0:
                    return num
            return None
        # treat as header in raw
        if isinstance(tok, str) and tok.strip():
            try:
                val = _get_from_raw(it, tok)
            except Exception:
                val = None
            if val is not None:
                return to_number(val)
        return None

    def _first_due() -> Optional[Decimal]:
        ad = it.get("amount_due")
        if isinstance(ad, list):
            for tok in ad:
                num = _resolve_token(tok)
                if num is not None and num != 0:
                    return num
            return None
        return _resolve_token(ad)

    amt = total if total is not None else _first_due()
    if amt is None:
        return None
    # Preserve original sign; do not force absolute/negative values.
    return amt


# reference and numbers are derived from config; pick_identifiers removed


def _get_from_raw(it: Dict[str, Any], header: str) -> Optional[str]:
    raw = it.get("raw") if isinstance(it.get("raw"), dict) else {}
    if not isinstance(header, str) or not header.strip() or not isinstance(raw, dict):
        return None
    # direct match
    val = raw.get(header)
    if isinstance(val, str) and val.strip():
        return val.strip()
    # case-insensitive fallback
    hlower = header.strip().lower()
    for k, v in raw.items():
        if isinstance(k, str) and k.strip().lower() == hlower:
            vs = str(v or "").strip()
            if vs:
                return vs
    # normalized fallback: ignore spaces/underscores/punctuation
    def _norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower().strip() if ch.isalnum())
    hnorm = _norm(header)
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        if _norm(k) == hnorm:
            vs = str(v or "").strip()
            if vs:
                return vs
    return None


def extract_invoice_number(it: Dict[str, Any], items_template: Dict[str, Any]) -> Optional[str]:
    # Prefer canonical field if present, else use config mapping to raw
    n = (it.get("number") or "").strip()
    if n:
        return n
    mapped = None
    try:
        header = items_template.get("number") if isinstance(items_template, dict) else None
        if isinstance(header, str) and header.strip():
            mapped = _get_from_raw(it, header)
    except Exception:
        mapped = None
    if mapped:
        return mapped
    return None


def extract_reference(it: Dict[str, Any], items_template: Dict[str, Any]) -> Optional[str]:
    """Return the Xero Reference value for the row, based on config.

    Priority:
      1) canonical 'reference' field on the row
      2) items_template['reference'] header from raw
    """
    r = (it.get("reference") or "").strip()
    if r:
        return r
    try:
        header = items_template.get("reference") if isinstance(items_template, dict) else None
        if isinstance(header, str) and header.strip():
            mapped = _get_from_raw(it, header)
            if mapped:
                return mapped
    except Exception:
        pass
    return None


def extract_date_from_raw(it: Dict[str, Any], items_template: Dict[str, Any], canonical_field: str, fmt: Optional[str]) -> Optional[date]:
    """Extract and parse a date from the row using contact config.

    Tries in order:
      1) Explicit items_template[canonical_field] header.
      2) items_template['raw'] mapping where raw-key looks like the canonical field or a close synonym.
      3) Directly scan the row's raw keys for a close synonym.
    """
    def _norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower().strip() if ch.isalnum())

    def _synonyms(cf: str) -> list[str]:
        cf = (_norm(cf) or "")
        if cf in ("duedate", "due"):
            return ["duedate", "due", "datedue"]
        if cf in ("date", "transactiondate"):
            return ["date", "transactiondate", "docdate"]
        return [cf]

    # 1) Explicit header mapping
    header = None
    if isinstance(items_template, dict):
        h = items_template.get(canonical_field)
        if isinstance(h, str) and h.strip():
            header = h
    if header:
        s = _get_from_raw(it, header)
        if s:
            dt = parse_date_tokenized(s, fmt)
            if dt:
                return dt

    # 2) Look into raw mapping
    raw_map = items_template.get("raw") if isinstance(items_template, dict) else None
    if isinstance(raw_map, dict):
        syns = set(_synonyms(canonical_field))
        for rk, mapped_header in raw_map.items():
            if not isinstance(rk, str):
                continue
            rk_norm = _norm(rk)
            if rk_norm in syns or any(w in rk_norm for w in syns):
                cand = mapped_header if isinstance(mapped_header, str) and mapped_header.strip() else rk
                s = _get_from_raw(it, cand)
                if s:
                    dt = parse_date_tokenized(s, fmt)
                    if dt:
                        return dt

    # 3) Scan row raw keys directly
    raw = it.get("raw") if isinstance(it.get("raw"), dict) else {}
    if isinstance(raw, dict):
        syns = set(_synonyms(canonical_field))
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            k_norm = _norm(k)
            if k_norm in syns or any(w in k_norm for w in syns):
                s = str(v or "").strip()
                if s:
                    dt = parse_date_tokenized(s, fmt)
                    if dt:
                        return dt
    return None


def to_plain_str(v: Any) -> str:
    return "" if v is None else str(v)


def build_line_description(it: Dict[str, Any]) -> str:
    parts = []
    for k in ("document_type", "description_details", "supplier_reference", "customer_reference"):
        val = (to_plain_str(it.get(k))).strip()
        if val:
            parts.append(f"{k.replace('_', ' ').title()}: {val}")
    # include a brief from raw if present
    raw = it.get("raw") or {}
    if isinstance(raw, dict):
        for k in ("description", "activity", "reference"):
            val = (to_plain_str(raw.get(k))).strip()
            if val and all(val not in p for p in parts):
                parts.append(val)
    return " | ".join(parts)[:4000]


def parse_line_amount_types(s: str) -> LineAmountTypes:
    key = (s or "").strip().lower()
    if key in ("exclusive",):
        return LineAmountTypes.EXCLUSIVE
    if key in ("inclusive",):
        return LineAmountTypes.INCLUSIVE
    # Fallback
    return LineAmountTypes.INCLUSIVE


def parse_invoice_status(s: str) -> str:
    # Invoice status accepted by API: DRAFT, SUBMITTED, AUTHORISED
    key = (s or "DRAFT").strip().upper()
    if key not in {"DRAFT", "SUBMITTED", "AUTHORISED"}:
        return "DRAFT"
    return key


def parse_credit_note_status(s: str) -> str:
    key = (s or "DRAFT").strip().upper()
    if key not in {"DRAFT", "SUBMITTED", "AUTHORISED"}:
        return "DRAFT"
    return key


def ensure_not_exists(
    api: AccountingApi,
    tenant_id: str,
    contact_id: str,
    kind: str,
    invoice_number: Optional[str] = None,
    reference: Optional[str] = None,
) -> bool:
    """Return True if safe to create a new doc.

    Prefer number uniqueness (scoped to contact); else fall back to Reference.
    If active duplicates exist, delete/void them and allow recreate.
    Ignores already DELETED/VOIDED hits. Bypass with FORCE_CREATE.
    """
    try:
        if FORCE_CREATE:
            return True

        conds = [f'Contact.ContactID==Guid("{contact_id}")']
        items = []

        if invoice_number and not SKIP_NUMBER:
            if kind == "credit_note":
                conds.append(f'CreditNoteNumber=="{invoice_number}"')
                where = " && ".join(conds)
                res = api.get_credit_notes(tenant_id, where=where, page_size=50)
                items = list(res.credit_notes or [])
            else:
                conds.append(f'InvoiceNumber=="{invoice_number}"')
                where = " && ".join(conds)
                res = api.get_invoices(tenant_id, where=where, page_size=50)
                items = list(res.invoices or [])
        elif reference:
            ref_esc = reference.replace('"', '""')
            conds.append(f'Reference=="{ref_esc}"')
            where = " && ".join(conds)
            if kind == "credit_note":
                res = api.get_credit_notes(tenant_id, where=where, page_size=50)
                items = list(res.credit_notes or [])
            else:
                res = api.get_invoices(tenant_id, where=where, page_size=50)
                items = list(res.invoices or [])
        else:
            return True

        def _status(x):
            try:
                return str(getattr(x, "status", "") or "").strip().upper()
            except Exception:
                return ""

        active = [x for x in items if _status(x) not in {"DELETED", "VOIDED"}]
        if not active:
            return True

        # If we matched existing docs, delete/void them so we can recreate
        print(f"Found {len(active)} existing Xero {kind}(s) matching number/reference; deleting/voiding…")

        def _delete_or_void_invoice(inv) -> bool:
            st = _status(inv)
            inv_id = getattr(inv, "invoice_id", None)
            if not inv_id:
                return False
            # DRAFT/SUBMITTED -> DELETED, AUTHORISED/PAID -> VOIDED
            # Xero won't delete paid/authorised with allocations; catch errors.
            try:
                from xero_python.accounting import Invoice as _Inv, Invoices as _Invs
                target_status = "DELETED" if st in {"DRAFT", "SUBMITTED"} else "VOIDED"
                payload = _Invs(invoices=[_Inv(status=target_status)])
                api.update_invoice(tenant_id, inv_id, payload)
                print(f"  - Invoice {inv_id} -> {target_status}")
                return True
            except Exception as e:
                print(f"  ! Failed to update invoice {inv_id} ({st}): {e}")
                return False

        def _delete_or_void_credit(cn) -> bool:
            st = _status(cn)
            cn_id = getattr(cn, "credit_note_id", None)
            if not cn_id:
                return False
            try:
                from xero_python.accounting import CreditNote as _Cn, CreditNotes as _Cns
                target_status = "DELETED" if st in {"DRAFT", "SUBMITTED"} else "VOIDED"
                payload = _Cns(credit_notes=[_Cn(status=target_status)])
                api.update_credit_note(tenant_id, cn_id, payload)
                print(f"  - CreditNote {cn_id} -> {target_status}")
                return True
            except Exception as e:
                print(f"  ! Failed to update credit note {cn_id} ({st}): {e}")
                return False

        ok_all = True
        if kind == "credit_note":
            for cn in active:
                ok_all = _delete_or_void_credit(cn) and ok_all
        else:
            for inv in active:
                ok_all = _delete_or_void_invoice(inv) and ok_all

        if not ok_all:
            print("One or more existing documents could not be removed; skipping recreate for safety.")
            return False

        return True
    except Exception as e:
        print(f"Failed to check/delete existing docs: {e}")
        return True


def create_invoice(api: AccountingApi, tenant_id: str, contact_id: str, it: Dict[str, Any], amt: Decimal, dt: Optional[date], due_dt: Optional[date], invoice_number: Optional[str], reference: Optional[str], expense: bool, account_code: Optional[str]) -> str:
    line = LineItem(
        description=build_line_description(it) or f"Statement item {reference}",
        quantity=1.0,
        unit_amount=float(amt),
        account_code=account_code,
        tax_type=TAX_TYPE,
    )
    inv = Invoice(
        type="ACCPAY" if expense else "ACCREC",
        contact=XeroContact(contact_id=contact_id),
        date=dt or date.today(),
        due_date=due_dt,
        line_items=[line],
        reference=reference,
        line_amount_types=parse_line_amount_types(LINE_AMOUNT_TYPES),
        status=parse_invoice_status(STATUS),
    )
    if invoice_number and not SKIP_NUMBER:
        inv.invoice_number = invoice_number

    res = api.create_invoices(tenant_id, invoices=Invoices(invoices=[inv]))
    created = (res.invoices or [None])[0]
    return getattr(created, "invoice_id", "") or ""


def create_credit_note(api: AccountingApi, tenant_id: str, contact_id: str, it: Dict[str, Any], amt: Decimal, dt: Optional[date], due_dt: Optional[date], credit_note_number: Optional[str], reference: Optional[str], expense: bool, account_code: Optional[str]) -> str:
    line = LineItem(
        description=build_line_description(it) or f"Statement item {reference}",
        quantity=1.0,
        unit_amount=float(amt),
        account_code=account_code,
        tax_type=TAX_TYPE,
    )
    cn = CreditNote(
        type="ACCPAYCREDIT" if expense else "ACCRECCREDIT",
        contact=XeroContact(contact_id=contact_id),
        date=dt or date.today(),
        due_date=due_dt,
        line_items=[line],
        reference=reference,
        line_amount_types=parse_line_amount_types(LINE_AMOUNT_TYPES),
        status=parse_credit_note_status(STATUS),
    )
    if credit_note_number and not SKIP_NUMBER:
        cn.credit_note_number = credit_note_number

    res = api.create_credit_notes(tenant_id, credit_notes=CreditNotes(credit_notes=[cn]))
    created = (res.credit_notes or [None])[0]
    return getattr(created, "credit_note_id", "") or ""


def main():
    if not AWS_PROFILE or not AWS_REGION:
        die("Set AWS_PROFILE and AWS_REGION in env (see service configuration).")
    if not S3_BUCKET_NAME:
        die("Set S3_BUCKET_NAME in env.")

    print("Building AWS session…")
    session = boto3.session.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)

    print("Fetching contact mapping config…")
    contact_cfg = get_contact_config(TENANT_ID, CONTACT_ID)
    stmt_date_fmt = contact_cfg.get("statement_date_format") if isinstance(contact_cfg, dict) else None

    # Derive items template mapping for field->header
    items_template = get_items_template_from_config(contact_cfg)
    if os.getenv("DEBUG_CONFIG"):
        try:
            keys = sorted(list(items_template.keys()) if isinstance(items_template, dict) else [])
        except Exception:
            keys = []
        print("Items template keys:", keys)
        print("Number header:", (items_template.get("number") if isinstance(items_template, dict) else None))

    print("Loading statement JSON from S3…")
    statement = get_statement_json(session, S3_BUCKET_NAME, TENANT_ID, STATEMENT_ID)
    items = (statement.get("statement_items") or []) if isinstance(statement, dict) else []
    if not items:
        die("No statement_items found in the JSON.")

    print("Initialising Xero client…")
    api, _api_client, tenant_id = build_xero_client()

    # Decide sales vs purchases side (ACCREC vs ACCPAY)
    expense = resolve_expense_side(api, tenant_id, CONTACT_ID)
    side_label = "ACCPAY (bill)" if expense else "ACCREC (invoice)"
    print(f"Document side: {side_label}")
    account_code = discover_account_code(api, tenant_id, for_expense=expense)
    if not account_code:
        print("Warning: could not auto-discover account code; Xero may require one.")

    created = 0
    skipped = 0
    errors: List[str] = []

    print(f"Processing {len(items)} rows…")
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue

        # Show the full row JSON before any processing
        print("-" * 80)
        print(f"Row {idx}: original item JSON")
        try:
            print(json.dumps(it, indent=2, default=str))
        except Exception:
            print(str(it))

        raw = it.get("raw", {}) if isinstance(it.get("raw"), dict) else {}
        doc_type = guess_statement_item_type(raw)
        amt = pick_amount(it, doc_type)
        # Skip only if amount is missing or exactly zero; allow negatives.
        if amt is None or amt == 0:
            skipped += 1
            print(f"- Row {idx}: skip (no amount or zero)")
            continue

        # Dates from canonical item fields using statement_date_format, with config fallback
        fmt = (it.get("statement_date_format") or stmt_date_fmt)
        dt_str = str((it.get("date") or "")).strip()
        dt = parse_date_tokenized(dt_str, fmt)
        if not dt:
            # Fallback to config-mapped raw header if canonical is missing
            dt = extract_date_from_raw(it, items_template, canonical_field="date", fmt=fmt)
        if not dt:
            print(f"- Row {idx}: skip (missing 'date' per config)")
            skipped += 1
            continue
        due_str = str((it.get("due_date") or "")).strip()
        due_dt = parse_date_tokenized(due_str, fmt) or extract_date_from_raw(it, items_template, canonical_field="due_date", fmt=fmt)

        # Determine invoice/credit number and reference from config mapping first
        inv_no = extract_invoice_number(it, items_template) or None
        ref_value = extract_reference(it, items_template)

        # Log parsed values right before attempting to create in Xero
        print(
            f"Row {idx}: parsed -> date={dt}, due_date={due_dt}, number={inv_no}, reference={ref_value}, amount={amt}, doc_type={doc_type}"
        )

        try:
            if not ensure_not_exists(
                api,
                tenant_id,
                CONTACT_ID,
                kind=("credit_note" if doc_type == "credit_note" else "invoice"),
                invoice_number=inv_no,
                reference=ref_value,
            ):
                skipped += 1
                print(f"- Row {idx}: existing could not be removed; skipped")
                continue

            if doc_type == "credit_note":
                cn_id = create_credit_note(
                    api=api,
                    tenant_id=tenant_id,
                    contact_id=CONTACT_ID,
                    it=it,
                    amt=amt,
                    dt=dt,
                    due_dt=due_dt,
                    credit_note_number=inv_no,
                    reference=ref_value,
                    expense=expense,
                    account_code=account_code,
                )
                created += 1
                print(f"+ Row {idx}: credit note created {cn_id}")
            else:
                inv_id = create_invoice(
                    api=api,
                    tenant_id=tenant_id,
                    contact_id=CONTACT_ID,
                    it=it,
                    amt=amt,
                    dt=dt,
                    due_dt=due_dt,
                    invoice_number=inv_no,
                    reference=ref_value,
                    expense=expense,
                    account_code=account_code,
                )
                created += 1
                print(f"+ Row {idx}: invoice created {inv_id}")
        except AccountingBadRequestException as e:
            msg = f"Row {idx}: Xero 400 error: {e}"
            print("! " + msg)
            errors.append(msg)
        except Exception as e:
            msg = f"Row {idx}: error: {e}"
            print("! " + msg)
            errors.append(msg)

    print(f"Done. Created: {created}, Skipped: {skipped}, Errors: {len(errors)}")
    if errors:
        print("Some errors occurred: ")
        for m in errors:
            print("- " + m)
        sys.exit(2)


if __name__ == "__main__":
    main()
