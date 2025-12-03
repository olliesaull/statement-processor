import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from config import logger
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


def _generate_statement_item_id(statement_id: Optional[str], sequence: int) -> str:
    if statement_id:
        return f"{statement_id}#item-{sequence:04d}"
    return f"stmt-item-{uuid4().hex[:12]}-{sequence:04d}"


def _normalize_table_cell(cell: Any) -> str:
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
    if s is None:
        return False
    t = str(s).strip()
    if not t:
        return False
    t = t.replace("−", "-")
    t = t.replace(",", "")
    t = t.replace(" ", "")
    t = re.sub(r"^[\$£€]\s*", "", t)
    t = re.sub(r"(?i)(cr|dr)$", "", t)
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
        "balance cf",
        "balance c f",
        "bal cf",
        "bal c f",
    )
    short_forms = {"bf", "b f", "bfwd", "b fwd", "cf", "c f", "cfwd", "c fwd"}
    return t in short_forms or any(k in t for k in keywords)


def row_is_opening_or_carried_forward(raw_row: List[str], mapped_item: Dict[str, Any]) -> bool:
    raw = mapped_item.get("raw") or {}
    if isinstance(raw, dict) and any(_is_forward_label(v) for v in raw.values() if v):
        return True
    non_empty = sum(1 for c in raw_row if (c or "").strip())
    money_count = sum(1 for c in raw_row if _looks_money(c))
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
    contact_cfg: Dict[str, Any] = get_contact_config(tenant_id=tenant_id, contact_id=contact_id)

    template_date_format: Optional[str] = None
    if isinstance(contact_cfg, dict):
        si = contact_cfg.get("statement_items")
        if isinstance(si, dict):
            items_template = deepcopy(si)
        elif isinstance(si, list) and si:
            items_template = deepcopy(si[0]) if isinstance(si[0], dict) else {}
        else:
            items_template = deepcopy(contact_cfg)
    else:
        items_template = {}

    template_date_format = items_template.get("date_format") if isinstance(items_template, dict) else None

    items_template.pop("date_format", None)
    items_template.pop("decimal_separator", None)
    items_template.pop("thousands_separator", None)

    simple_map: Dict[str, str] = {}
    raw_map: Dict[str, str] = {}
    date_format = None
    if isinstance(contact_cfg, dict):
        date_format = contact_cfg.get("date_format")
        if not date_format:
            date_format = template_date_format
    if not date_format:
        raise ValueError("date_format must be configured for this contact")
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

    total_candidates: List[str] = []
    total_cfg = items_template.get("total")
    if isinstance(total_cfg, list):
        total_candidates = [str(x).strip() for x in total_cfg if isinstance(x, str) and x.strip()]
    elif isinstance(total_cfg, str) and total_cfg.strip():
        total_candidates = [total_cfg.strip()]

    candidates = list(simple_map.values())
    candidates.extend(list(raw_map.keys()))
    candidates.extend([v for v in raw_map.values() if v])

    selected = select_relevant_tables_per_page(tables_with_pages, candidates=candidates)

    items: List[StatementItem] = []
    item_flags: List[List[str]] = []
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

        if header_detected:
            try:
                cfg_existing: Dict[str, Any] = {}
                try:
                    cfg_existing = get_contact_config(tenant_id, contact_id)
                except Exception:
                    cfg_existing = {}

                updated = False
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
                logger.info("[table_to_json] failed to persist raw headers", error=e)

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
            flags: List[str] = []

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
                    elif field == "date" and str(cell or "").strip():
                        flags.append("invalid-date")
                row_obj[field] = value
                canonical_header = str(actual_header or header_name)
                extracted_simple[field] = {"header": canonical_header, "value": value}

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

            total_entries: Dict[str, Any] = {}
            for header_idx, header_label in configured_amount_headers:
                if header_idx is None or header_idx >= len(r):
                    continue
                cell_value = r[header_idx]
                if cell_value is None:
                    continue
                clean = _clean_currency(cell_value)
                if not clean:
                    continue
                num = _to_number(clean)
                if num is None:
                    continue
                total_entries[header_label] = num
            row_obj["total"] = total_entries

            item_counter += 1
            row_obj["statement_item_id"] = _generate_statement_item_id(statement_id, item_counter)
            stmt_item = StatementItem(**row_obj)

            earliest, latest = _update_date_range(stmt_item.date, stmt_item.due_date)
            items.append(stmt_item)

            raw_extracted = extracted_raw or {}
            if full_raw:
                raw_extracted = {**{k: {"header": k, "value": v} for k, v in full_raw.items()}, **raw_extracted}
            item_flags.append(
                [
                    {
                        "page": entry["page"],
                        "row": i_row,
                        "extracted": {"simple": extracted_simple, "raw": raw_extracted},
                        "flags": flags,
                    }
                ]
            )

    combined_flags: List[Dict[str, Any]] = []
    for flist in item_flags:
        combined_flags.extend(flist)

    earliest_date, latest_date = _derive_date_range(items)
    statement = SupplierStatement(
        statement_items=items,
        earliest_item_date=earliest_date,
        latest_item_date=latest_date,
    )

    output = statement.model_dump()
    if combined_flags:
        output["_flags"] = combined_flags

    return output


def _derive_date_range(items: List[StatementItem]) -> Tuple[Optional[str], Optional[str]]:
    dates = []
    for item in items:
        if item.date:
            dates.append(item.date)
    if not dates:
        return None, None
    dates_sorted = sorted(dates)
    return dates_sorted[0], dates_sorted[-1]


def _update_date_range(date_val: Optional[str], due_date: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    earliest_date: Optional[str] = None
    latest_date: Optional[str] = None
    for d in (date_val, due_date):
        if d:
            if earliest_date is None or d < earliest_date:
                earliest_date = d
            if latest_date is None or d > latest_date:
                latest_date = d
    return earliest_date, latest_date


def _clean_currency(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.replace("−", "-")
    text = text.replace(",", "")
    text = text.replace(" ", "")
    text = re.sub(r"^[\$£€]\s*", "", text)
    text = re.sub(r"(?i)(cr|dr)$", "", text)
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    return text


def _to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first_nonempty_row_index(grid: List[List[str]]) -> int:
    for idx, row in enumerate(grid):
        if any((c or "").strip() for c in row):
            return idx
    return 0


def _rows_match_header(row: List[str], header: List[str]) -> bool:
    if not row or not header:
        return False
    header_norm = [norm(h) for h in header]
    row_norm = [norm(c) for c in row]
    overlap = sum(1 for c in row_norm if c and c in header_norm)
    return overlap >= max(1, len(header_norm) // 2)
