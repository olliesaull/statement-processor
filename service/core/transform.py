import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from config import logger
from core.date_utils import parse_with_format
from core.item_classification import guess_statement_item_type
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


def _generate_statement_item_id(statement_id: Optional[str], sequence: int) -> str:
    if statement_id:
        return f"{statement_id}#item-{sequence:04d}"
    return f"stmt-item-{uuid4().hex[:12]}-{sequence:04d}"


def _normalize_table_cell(cell: Any) -> str:
    """Normalize a table cell for duplicate detection."""
    if cell is None:
        return ""
    text = str(cell).strip()
    if not text:
        return ""
    text = text.replace("−", "-")
    compact = re.sub(r"\s+", " ", text)
    candidate = re.sub(r"^[\$£€]\s*", "", compact)
    candidate = re.sub(r"(?i)(cr|dr)$", "", candidate)
    if candidate.startswith("(") and candidate.endswith(")"):
        candidate = f"-{candidate[1:-1]}"
    candidate = candidate.replace(",", "")
    try:
        value = str(Decimal(candidate))
        if value.endswith(".0"):
            value = value[:-2]
        return value
    except (InvalidOperation, ValueError):
        return compact.lower()


def _dedupe_grid_columns(grid: List[List[str]]) -> List[List[str]]:
    """Remove duplicate columns where header+data match exactly."""
    if not grid or not grid[0]:
        return grid
    seen: Dict[Tuple[str, Tuple[str, ...]], int] = {}
    keep: List[int] = []
    for idx in range(len(grid[0])):
        header = norm(grid[0][idx]) if idx < len(grid[0]) else ""
        column_values = tuple(
            _normalize_table_cell(row[idx] if idx < len(row) else "")
            for row in grid[1:]
        )
        signature = (header, column_values)
        if signature in seen:
            continue
        seen[signature] = idx
        keep.append(idx)
    if len(keep) == len(grid[0]):
        return grid
    return [[row[i] if i < len(row) else "" for i in keep] for row in grid]


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
        results: List[str] = []
        if isinstance(values, (list, tuple)):
            for entry in values:
                if isinstance(entry, dict):
                    candidate = entry.get("value")
                elif hasattr(entry, "value"):
                    candidate = getattr(entry, "value")
                else:
                    candidate = entry
                if str(candidate or "").strip():
                    results.append(str(candidate))
            return results
        if isinstance(values, dict):
            for val in values.values():
                if str(val or "").strip():
                    results.append(str(val))
            return results
        if str(values or "").strip():
            results.append(str(values))
        return results

    amount_like_values: List[str] = []
    for field_name in ("total", "amount_credited"):
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


def table_to_json(
    key: str,
    tables_with_pages: List[Dict[str, Any]],
    tenant_id: str,
    contact_id: str,
    statement_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Produce canonical dict (validated by Pydantic)."""
    contact_cfg: Dict[str, Any] = get_contact_config(tenant_id=tenant_id, contact_id=contact_id)

    # Support both nested (statement_items dict), legacy (list with one dict),
    # and flattened (template keys at root) config shapes
    template_date_format: Optional[str] = None
    if isinstance(contact_cfg, dict):
        si = contact_cfg.get("statement_items")
        if isinstance(si, dict):
            items_template = deepcopy(si)
        elif isinstance(si, list) and si:
            items_template = deepcopy(si[0]) if isinstance(si[0], dict) else {}
        else:
            # Flattened/root form
            items_template = deepcopy(contact_cfg)
    else:
        items_template = {}

    template_date_format = items_template.get("date_format") if isinstance(items_template, dict) else None

    # Drop contact-level selectors so rows only carry line-item data.
    items_template.pop("date_format", None)
    items_template.pop("decimal_separator", None)
    items_template.pop("thousands_separator", None)

    # build mapping config
    simple_map: Dict[str, str] = {}
    raw_map: Dict[str, str] = {}
    date_format = None
    # Use explicit date_format from config (preferred)
    if isinstance(contact_cfg, dict):
        date_format = contact_cfg.get("date_format")
        if not date_format:
            date_format = template_date_format
    if not date_format:
        raise ValueError("date_format must be configured for this contact")
    # Limit simple fields to those supported by StatementItem (exclude special handled keys)
    # Exclude 'date_format' so it is never treated as a header mapping.
    allowed_fields = set(StatementItem.model_fields.keys()) - {
        "raw",
        "statement_item_id",
        "item_type",
    }
    for field, value in items_template.items():
        if field == "raw" and isinstance(value, dict):
            raw_map = value
        elif field in allowed_fields and isinstance(value, str) and value.strip():
            simple_map[field] = value

    # Special support: total can be a list of candidate headers
    total_candidates: List[str] = []
    total_cfg = items_template.get("total")
    if isinstance(total_cfg, list):
        total_candidates = [str(x).strip() for x in total_cfg if isinstance(x, str) and x.strip()]
    elif isinstance(total_cfg, str) and total_cfg.strip():
        total_candidates = [total_cfg.strip()]

    candidates = list(simple_map.values())
    candidates.extend(list(raw_map.keys()))
    candidates.extend([v for v in raw_map.values() if v])

    # select one table per page
    selected = select_relevant_tables_per_page(tables_with_pages, candidates=candidates)

    items: List[StatementItem] = []
    item_counter = 0
    primary_header_row: Optional[List[str]] = None
    primary_col_index: Optional[Dict[str, int]] = None
    for _page_number, entry in enumerate(selected, start=1):
        grid = entry["grid"]
        grid = _dedupe_grid_columns(grid)

        header_row: List[str]
        col_index: Dict[str, int]
        data_rows: List[List[str]]
        header_detected = False

        if primary_header_row is None:
            hdr_idx, detected_header = best_header_row(grid, candidates)
            header_row = list(detected_header)
            col_index = build_col_index(header_row)
            data_rows = grid[hdr_idx + 1 :]
            primary_header_row = list(header_row)
            primary_col_index = dict(col_index)
            header_detected = True
        else:
            header_row = list(primary_header_row)
            col_index = dict(primary_col_index or build_col_index(header_row))
            start_idx = _first_nonempty_row_index(grid)
            first_content_row = grid[start_idx] if start_idx < len(grid) else []
            if _rows_match_header(first_content_row, primary_header_row):
                data_rows = grid[start_idx + 1 :]
                header_detected = True
            else:
                data_rows = grid[start_idx:]

        # Ensure the contact's config table has a 'raw' mapping for discovered headers.
        # We add missing raw keys as identity mappings (lower-cased): {"header": "header"}.
        if header_detected:
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
                logger.info(
                    "[table_to_json] failed to persist raw headers",
                    error=e,
                )

        # Resolve total entries strictly from configured headers
        configured_amount_headers: List[Tuple[Optional[int], str]] = []
        for cand in total_candidates:
            clean = cand.strip()
            if not clean:
                continue
            idx = col_index.get(norm(clean))
            configured_amount_headers.append((idx, clean))

        for i_row, r in enumerate(data_rows, start=1):
            if not any((c or "").strip() for c in r):
                continue

            # start from template dict (but we’ll build StatementItem)
            full_raw: Dict[str, Any] = {}
            for col_idx, header_value in enumerate(header_row):
                label = str(header_value or "").strip() or f"column_{col_idx}"
                cell_value = r[col_idx] if col_idx < len(r) else ""
                if label in full_raw:
                    dedup_label = f"{label}_{col_idx}"
                    full_raw[dedup_label] = cell_value
                else:
                    full_raw[label] = cell_value

            row_obj: Dict[str, Any] = deepcopy(items_template)
            row_obj.pop("statement_item_id", None)

            # simple fields
            extracted_simple: Dict[str, Any] = {}
            for field, header_name in simple_map.items():
                idx = col_index.get(norm(header_name))
                actual_header = (
                    header_row[idx]
                    if idx is not None and idx < len(header_row)
                    else header_name
                )
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
                canonical_header = str(actual_header or header_name)
                extracted_simple[field] = {"header": canonical_header, "value": value}

            # raw fields with auto-fill using the header label (preserve original case in extracted JSON)
            raw_obj: Dict[str, Any] = {}
            extracted_raw: Dict[str, Any] = {}
            for raw_key, raw_src_header in (raw_map or {}).items():
                chosen_header = raw_src_header or raw_key
                idx = col_index.get(norm(chosen_header))
                actual_header = (
                    header_row[idx]
                    if idx is not None and idx < len(header_row)
                    else chosen_header
                )
                canonical_header = str(actual_header or chosen_header)
                val = get_by_header(r, col_index, chosen_header)
                raw_obj[canonical_header] = val
                extracted_raw[canonical_header] = {"header": canonical_header, "value": val}
            row_obj["raw"] = raw_obj

            # Map total using configured headers only (e.g., debit/credit columns)
            total_entries: Dict[str, Any] = {}

            def _resolve_header(idx: Optional[int], fallback: str) -> str:
                if idx is not None and idx < len(header_row):
                    candidate_header = header_row[idx] or ""
                else:
                    candidate_header = ""
                candidate_header = candidate_header or fallback or ""
                return candidate_header.strip()

            for idx, configured_label in configured_amount_headers:
                header_label = _resolve_header(idx, configured_label)
                value = get_by_header(r, col_index, configured_label)
                label_key = header_label or configured_label
                if label_key:
                    total_entries[label_key] = value

            row_obj["total"] = total_entries

            primary_entry_value: Optional[Any] = None
            primary_entry_label: str = ""
            for label, val in total_entries.items():
                if str(val or "").strip():
                    primary_entry_label = label
                    primary_entry_value = val
                    break
            if primary_entry_value is None and total_entries:
                primary_entry_label, primary_entry_value = next(iter(total_entries.items()))

            if primary_entry_label:
                extracted_simple["total"] = {
                    "header": primary_entry_label,
                    "value": primary_entry_value or "",
                }

            # Surface all amount entries into raw for completeness (without overwriting existing values)
            try:
                if "raw" in row_obj and isinstance(row_obj["raw"], dict):
                    for label, val in total_entries.items():
                        if isinstance(label, str) and label.strip():
                            row_obj["raw"].setdefault(label, val)
            except Exception as exc:
                logger.warning(
                    "[table_to_json] failed to merge amount entries into raw",
                    statement_id=statement_id,
                    row_number=i_row,
                    error=str(exc),
                    exc_info=True,
                )

            # Ensure raw contains the simple-mapped headers/values (preserve original case)
            try:
                for _fld, meta in extracted_simple.items():
                    hdr = meta.get("header") if isinstance(meta, dict) else None
                    val = meta.get("value") if isinstance(meta, dict) else None
                    if isinstance(hdr, str) and hdr.strip():
                        row_obj["raw"].setdefault(hdr, val)
            except Exception as exc:
                logger.warning(
                    "[table_to_json] failed to merge simple headers into raw",
                    statement_id=statement_id,
                    row_number=i_row,
                    error=str(exc),
                    exc_info=True,
                )

            # Keep raw key insertion order (do not reorder to PDF header order)

            try:
                row_obj["item_type"] = guess_statement_item_type(full_raw)
            except Exception as exc:
                logger.warning(
                    "[table_to_json] failed to classify item type",
                    statement_id=statement_id,
                    row_number=i_row,
                    error=str(exc),
                    exc_info=True,
                )
                row_obj["item_type"] = "invoice"

            # carried-forward skipper
            if row_is_opening_or_carried_forward(r, row_obj):
                continue

            if row_is_summary_like(r, row_obj):
                continue

            item_counter += 1
            row_obj["statement_item_id"] = _generate_statement_item_id(statement_id, item_counter)

            # validate to canonical StatementItem
            try:
                si = StatementItem(**row_obj)
                items.append(si)
                fields_logged = {k: getattr(si, k) for k in simple_map.keys()}
                amount_components = getattr(si, "total", None)
                if amount_components:
                    fields_logged["total"] = dict(amount_components)
            except Exception as e:
                logger.info(
                    "[table_to_json] validation error",
                    row_number=i_row,
                    error=e,
                )
                logger.info("[table_to_json] row_obj", row_obj=row_obj)
                raise

    # meta with filename
    statement = SupplierStatement(statement_items=items)
    return statement.model_dump()


def _first_nonempty_row_index(grid: List[List[str]]) -> int:
    for idx, row in enumerate(grid):
        if any((cell or "").strip() for cell in row):
            return idx
    return 0


def _rows_match_header(row: List[str], header: List[str]) -> bool:
    def _clean(values: List[str]) -> List[str]:
        cleaned = [str(v or "").strip().lower() for v in values]
        while cleaned and cleaned[-1] == "":
            cleaned.pop()
        return cleaned

    return _clean(row) == _clean(header)

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
    return sa.casefold() == sb.casefold()
