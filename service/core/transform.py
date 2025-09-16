import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from core.date_utils import parse_with_format
from core.get_contact_config import get_contact_config, set_contact_config
from core.models import StatementItem, SupplierStatement

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")
_SUMMARY_KEYWORDS = (
    "balance",
    "closing",
    "outstanding",
    "subtotal",
    "total",
    "amount due",
    "due",
    "statement total",
)


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


def _looks_money(s: str) -> bool:
    """Heuristic: does the string look like a money amount?

    Supports:
    - Optional currency symbols (e.g., $, £, €)
    - Optional thousand separators (commas/spaces)
    - Negative with '-' or parentheses (e.g., (123.45))
    - Optional trailing CR/DR
    """
    if s is None:
        return False
    t = str(s).strip()
    if not t:
        return False
    # Normalize common variations
    t = t.replace("−", "-")  # unicode minus to ascii
    t = t.replace(",", "")
    t = t.replace(" ", "")
    # Strip leading currency symbols
    t = re.sub(r"^[\$£€]\s*", "", t)
    # Strip trailing CR/DR
    t = re.sub(r"(?i)(cr|dr)$", "", t)
    # Parentheses indicate negative
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t))


def _is_forward_label(text: str) -> bool:
    t = re.sub(r"[^a-z0-9 ]+", "", (norm(text) or ""))
    if not t:
        return False
    keywords = (
        "brought forward",
        "carried forward",
        "opening balance",
        "opening bal",
        "previous balance",
        "balance forward",
        "balance bf",
        "balance b f",
        "bal bf",
        "bal b f",
    )
    short_forms = {"bf", "b f", "bfwd", "b fwd", "cf", "c f", "cfwd", "c fwd"}
    return t in short_forms or any(k in t for k in keywords)


def row_is_opening_or_carried_forward(raw_row: List[str], mapped_item: Dict[str, Any]) -> bool:
    """Detect opening/brought-forward rows using raw text only.

    We consider a row as a carried-forward/opening row if any cell contains
    typical forward labels (e.g., "brought forward", "balance b/f").
    Additionally, treat very sparse rows with little or no currency-like values
    as non-transactional headers.
    """
    raw = mapped_item.get("raw") or {}
    if isinstance(raw, dict) and any(_is_forward_label(v) for v in raw.values() if v):
        return True
    non_empty = sum(1 for c in raw_row if (c or "").strip())
    money_count = sum(1 for c in raw_row if _looks_money(c))
    # Heuristic: only skip very sparse, non-numeric rows (likely headers/separators)
    return money_count == 0 and non_empty <= 2


def _has_summary_keyword(text: str) -> bool:
    if not text:
        return False
    t = norm(text)
    if not t:
        return False
    for kw in _SUMMARY_KEYWORDS:
        if " " in kw:
            if kw in t:
                return True
        else:
            if re.search(rf"\b{re.escape(kw)}\b", t):
                return True
    return False


def row_is_summary_like(raw_row: List[str], mapped_item: Dict[str, Any]) -> bool:
    """Detect balance/summary rows that should be excluded from transactions.

    We look for rows dominated by balance/total keywords where core identifying
    fields (date/number/reference) are empty and the amount columns are textual
    instead of numeric. Negative amounts are treated as normal monetary values.
    """
    raw_cells = [str(c or "").strip() for c in raw_row]
    text_cells = [c for c in raw_cells if c]
    if not text_cells:
        return False

    keyword_hits = sum(1 for c in text_cells if _has_summary_keyword(c))
    if keyword_hits < 2:
        return False

    has_number = bool(str((mapped_item or {}).get("number") or "").strip())
    has_date = bool(str((mapped_item or {}).get("date") or "").strip())
    has_reference = bool(str((mapped_item or {}).get("reference") or "").strip())
    missing_identifiers = sum(1 for flag in (has_number, has_date, has_reference) if not flag)

    money_count = sum(1 for cell in raw_cells if _looks_money(cell))

    def _iter_amount_like_values(values: Any) -> List[str]:
        if isinstance(values, (list, tuple)):
            return [str(v) for v in values if str(v or "").strip()]
        if str(values or "").strip():
            return [str(values)]
        return []

    amount_like_values: List[str] = []
    for field_name in ("amount_due", "total", "amount_paid", "amount_credited"):
        amount_like_values.extend(_iter_amount_like_values((mapped_item or {}).get(field_name)))

    numeric_amounts = sum(1 for v in amount_like_values if _looks_money(v))
    textual_amounts = len(amount_like_values) - numeric_amounts

    if missing_identifiers >= 2 and (money_count <= 1 or (numeric_amounts == 0 and textual_amounts > 0)):
        return True

    if missing_identifiers == 3 and numeric_amounts <= 1:
        return True

    return False


def select_relevant_tables_per_page(tables_with_pages: List[Dict[str, Any]], candidates: List[str], small_table_penalty: float = 2.5) -> List[Dict[str, Any]]:
    if not tables_with_pages:
        return []
    cand_set = {c.strip().lower() for c in candidates if c}
    date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    by_page: Dict[int, List[List[List[str]]]] = {}
    for t in tables_with_pages:
        by_page.setdefault(int(t["page"]), []).append(t["grid"])
    selected: List[Dict[str, Any]] = []
    for page, grids in sorted(by_page.items()):
        best_grid, best_score = None, float("-inf")
        for grid in grids:
            if not grid:
                continue
            hdr_idx, header_row = best_header_row(grid, list(cand_set))
            data_rows = grid[hdr_idx + 1 :]
            header_norm = [norm(h) for h in header_row]
            header_hits = sum(
                1
                for h in header_norm
                if h in cand_set or any(c in h or h in c for c in cand_set)
            )
            date_hits = sum(
                1 for r in data_rows[:10] if r and date_re.match((r[0] or "").strip())
            )
            size_bonus = len(grid) * (len(grid[0]) if grid and grid[0] else 0)
            penalty = small_table_penalty if len(data_rows) <= 1 else 0.0
            score = header_hits * 10 + date_hits * 2 + size_bonus * 0.001 - penalty
            if score > best_score:
                best_score, best_grid = score, grid
        if best_grid is None:
            best_grid = max(grids, key=lambda g: (len(g), len(g[0]) if g else 0))
        selected.append({"page": page, "grid": best_grid})
    return selected


def table_to_json(key: str, tables_with_pages: List[Dict[str, Any]], tenant_id: str, contact_id: str) -> Dict[str, Any]:
    """Produce canonical dict (validated by Pydantic)."""
    map_cfg: Dict[str, Any] = get_contact_config(tenant_id=tenant_id, contact_id=contact_id)

    # Support both nested (statement_items dict), legacy (list with one dict),
    # and flattened (template keys at root) config shapes
    if isinstance(map_cfg, dict):
        si = map_cfg.get("statement_items")
        if isinstance(si, dict):
            items_template = deepcopy(si)
        elif isinstance(si, list) and si:
            items_template = deepcopy(si[0]) if isinstance(si[0], dict) else {}
        else:
            # Flattened/root form
            items_template = deepcopy(map_cfg)
    else:
        items_template = {}

    # build mapping config
    simple_map: Dict[str, str] = {}
    raw_map: Dict[str, str] = {}
    date_format = None
    # Use explicit statement_date_format from config (preferred)
    date_format = map_cfg.get("statement_date_format") if isinstance(map_cfg, dict) else None
    if not date_format:
        raise ValueError("statement_date_format must be configured for this contact")
    # Limit simple fields to those supported by StatementItem (exclude special handled keys)
    # Exclude 'statement_date_format' so it is never treated as a header mapping.
    allowed_fields = set(StatementItem.model_fields.keys()) - {"raw", "statement_date_format"}
    for field, value in items_template.items():
        if field == "raw" and isinstance(value, dict):
            raw_map = value
        elif field in allowed_fields and isinstance(value, str) and value.strip():
            simple_map[field] = value

    # Special support: amount_due can be a list of candidate headers
    amount_due_candidates: List[str] = []
    amt_cfg = items_template.get("amount_due")
    if isinstance(amt_cfg, list):
        amount_due_candidates = [str(x).strip() for x in amt_cfg if isinstance(x, str) and x.strip()]
    elif isinstance(amt_cfg, str) and amt_cfg.strip():
        amount_due_candidates = [amt_cfg.strip()]

    candidates = list(simple_map.values())
    candidates.extend(list(raw_map.keys()))
    candidates.extend([v for v in raw_map.values() if v])

    # select one table per page
    selected = select_relevant_tables_per_page(tables_with_pages, candidates=candidates)

    items: List[StatementItem] = []
    for entry in selected:
        grid = entry["grid"]
        hdr_idx, header_row = best_header_row(grid, candidates)
        data_rows = grid[hdr_idx + 1 :]
        col_index = build_col_index(header_row)

        # Ensure the contact's config table has a 'raw' mapping for discovered headers.
        # We add missing raw keys as identity mappings (lower-cased): {"header": "header"}.
        try:
            cfg_existing: Dict[str, Any] = {}
            try:
                cfg_existing = get_contact_config(tenant_id, contact_id)
            except Exception:
                cfg_existing = {}

            updated = False
            # Root-level raw only
            root_raw = cfg_existing.get("raw") if isinstance(cfg_existing.get("raw"), dict) else {}
            root_raw = dict(root_raw)
            for h in header_row:
                hh = str(h or "").strip()
                if not hh:
                    continue
                kl = hh.lower()
                if kl not in root_raw:
                    root_raw[kl] = kl
                    updated = True
            if updated:
                new_cfg = dict(cfg_existing)
                new_cfg["raw"] = root_raw
                set_contact_config(tenant_id, contact_id, new_cfg)
        except Exception as e:
            print(f"[table_to_json] failed to persist raw headers: {e}")

        # Resolve candidate column indices for amount_due (handle duplicate headers like 'AMOUNT')
        amount_cols: List[int] = []
        if amount_due_candidates:
            cand_norm = {norm(h) for h in amount_due_candidates if h}
            for i, h in enumerate(header_row):
                if norm(h) in cand_norm:
                    amount_cols.append(i)
        # Fallback: try common synonyms if none configured or none matched
        if not amount_cols:
            common_amt = {"amount", "debit", "credit", "debitamount", "creditamount", "value", "total"}
            for i, h in enumerate(header_row):
                hn = norm(h).replace(" ", "")
                if hn in common_amt:
                    amount_cols.append(i)

        for i_row, r in enumerate(data_rows, start=1):
            if not any((c or "").strip() for c in r):
                continue

            # start from template dict (but we’ll build StatementItem)
            row_obj: Dict[str, Any] = deepcopy(items_template)

            # Surface the statement date format explicitly for downstream consumers
            row_obj["statement_date_format"] = date_format

            # simple fields
            extracted_simple: Dict[str, Any] = {}
            for field, header_name in simple_map.items():
                cell = get_by_header(r, col_index, header_name)
                value: Any = cell
                if field in {"date", "due_date"}:
                    try:
                        parsed = parse_with_format(cell, date_format)
                    except ValueError as err:
                        raise ValueError(
                            f"Failed to parse '{cell}' using format '{date_format}'"
                        ) from err
                    if parsed is not None:
                        value = parsed.strftime("%Y-%m-%d")
                row_obj[field] = value
                extracted_simple[field] = {"header": header_name, "value": value}

            # raw fields with auto-fill using the header label (preserve original case in extracted JSON)
            raw_obj: Dict[str, Any] = {}
            extracted_raw: Dict[str, Any] = {}
            for raw_key, raw_src_header in (raw_map or {}).items():
                chosen_header = raw_src_header or raw_key
                val = get_by_header(r, col_index, chosen_header)
                raw_obj[str(chosen_header)] = val
                extracted_raw[str(chosen_header)] = {"header": str(chosen_header), "value": val}
            row_obj["raw"] = raw_obj

            # Map amount_due when configured as multiple candidate headers or if we found amount columns
            picked_amount: Optional[str] = None
            if amount_cols:
                # Prefer the rightmost numeric-looking value among candidate columns
                vals = [(i, (r[i] if i < len(r) else "")) for i in amount_cols]
                # First pass: numeric-looking values
                numeric_vals = [(i, v) for i, v in vals if _looks_money(v)]
                if numeric_vals:
                    picked_amount = numeric_vals[-1][1]
                else:
                    # Fallback: last non-empty among candidates
                    non_empty = [(i, v) for i, v in vals if str(v or "").strip()]
                    if non_empty:
                        picked_amount = non_empty[-1][1]
            # If we picked an amount, record it in amount_due as a single-entry list
            if picked_amount is not None and str(picked_amount).strip():
                row_obj["amount_due"] = [picked_amount]
                extracted_simple["amount_due"] = {"header": "|".join(str(i) for i in amount_cols) or "", "value": picked_amount}
                # Also surface into raw if not present and we can name the column
                try:
                    if "raw" in row_obj and isinstance(row_obj["raw"], dict):
                        # Pick the header label for the picked column
                        if amount_cols:
                            # Map to the header text of the picked column
                            idx = amount_cols[-1] if _looks_money(picked_amount) else amount_cols[-1]
                            hdr_label = header_row[idx] if idx < len(header_row) else "amount"
                            row_obj["raw"].setdefault(str(hdr_label), picked_amount)
                except Exception:
                    pass

            # Ensure raw contains the simple-mapped headers/values (preserve original case)
            try:
                for _fld, meta in extracted_simple.items():
                    hdr = meta.get("header") if isinstance(meta, dict) else None
                    val = meta.get("value") if isinstance(meta, dict) else None
                    if isinstance(hdr, str) and hdr.strip():
                        row_obj["raw"].setdefault(hdr, val)
            except Exception:
                pass

            # Keep raw key insertion order (do not reorder to PDF header order)

            # carried-forward skipper
            if row_is_opening_or_carried_forward(r, row_obj):
                continue

            if row_is_summary_like(r, row_obj):
                continue

            # validate to canonical StatementItem
            try:
                si = StatementItem(**row_obj)
                items.append(si)
                fields_logged = {k: getattr(si, k) for k in simple_map.keys()}
                if getattr(si, "amount_due", None):
                    fields_logged["amount_due"] = getattr(si, "amount_due")
            except Exception as e:
                print(f"[table_to_json] validation error on row #{i_row}: {e}")
                print("[table_to_json] row_obj=", row_obj)
                raise

    # meta with filename
    statement = SupplierStatement(statement_items=items)
    return statement.model_dump()

def _norm_number(x: Any) -> Optional[Decimal]:
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
    s = _NON_NUMERIC_RE.sub("", s).replace(",", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def equal(a: Any, b: Any) -> bool:
    """Numeric-aware equality; otherwise trimmed string equality."""
    da, db = _norm_number(a), _norm_number(b)
    if da is not None or db is not None:
        return da == db
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    return sa == sb
