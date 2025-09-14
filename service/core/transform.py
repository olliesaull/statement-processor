import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from core.get_contact_config import get_contact_config
from core.models import StatementItem, SupplierStatement

_NON_NUMERIC_RE = re.compile(r"[^\d\-\.,]")


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
    t = clean_number_str(s)
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?", t)) if t else False


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
    return non_empty <= 3 and money_count <= 1


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
    # Limit simple fields to those supported by StatementItem (exclude special handled keys)
    # Exclude 'statement_date_format' so it is never treated as a header mapping.
    allowed_fields = set(StatementItem.model_fields.keys()) - {"raw", "statement_date_format"}
    for field, value in items_template.items():
        if field == "raw" and isinstance(value, dict):
            raw_map = value
        elif field in allowed_fields and isinstance(value, str) and value.strip():
            simple_map[field] = value

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

        for r in data_rows:
            if not any((c or "").strip() for c in r):
                continue

            # start from template dict (but we’ll build StatementItem)
            row_obj: Dict[str, Any] = deepcopy(items_template)

            # Surface the statement date format explicitly for downstream consumers
            row_obj["statement_date_format"] = date_format

            # simple fields
            for field, header_name in simple_map.items():
                cell = get_by_header(r, col_index, header_name)
                row_obj[field] = cell

            # raw fields with auto-fill
            raw_obj = {}
            for raw_key, raw_src_header in (raw_map or {}).items():
                chosen_header = raw_src_header or raw_key
                raw_obj[raw_key] = get_by_header(r, col_index, chosen_header)
            row_obj["raw"] = raw_obj

            # carried-forward skipper
            if row_is_opening_or_carried_forward(r, row_obj):
                continue

            # validate to canonical StatementItem
            items.append(StatementItem(**row_obj))

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
