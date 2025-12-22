"""
Transform Textract table grids into structured statement JSON.

This module is the core mapping layer for statement extraction. It:
- Chooses the most relevant table per page
- Identifies header rows and maps columns to configured fields
- Normalizes dates and numeric values
- Emits `StatementItem` models and aggregates statement-level metadata

The main entry point is `table_to_json`.
"""

import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from config import logger
from core.date_utils import parse_with_format
from core.extraction import TableOnPage
from core.get_contact_config import get_contact_config, set_contact_config
from core.models import StatementItem, SupplierStatement


def _generate_statement_item_id(statement_id: Optional[str], sequence: int) -> str:
    """Build a stable per-row identifier, namespaced under the statement id when provided."""
    if statement_id:
        return f"{statement_id}#item-{sequence:04d}"
    return f"stmt-item-{uuid4().hex[:12]}-{sequence:04d}"


def _norm(s: str) -> str:
    """Normalize header/cell text for fuzzy comparisons."""
    return " ".join((s or "").split()).strip().lower()


def _normalize_table_cell(cell: Any) -> str:
    """
    Normalize a table cell to a clean string for comparisons/headers.

    This strips currency symbols, separators, and parenthetical negatives, then
    tries to coerce numeric values into a canonical string representation.
    """
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
    """Remove duplicate columns (identical header + values) to avoid double-counting fields."""
    if not grid or not grid[0]:
        return grid
    seen: Dict[Tuple[str, Tuple[str, ...]], int] = {}
    keep: List[int] = []
    for idx in range(len(grid[0])):
        header = _norm(grid[0][idx]) if idx < len(grid[0]) else ""
        column_values = tuple(_normalize_table_cell(row[idx] if idx < len(row) else "") for row in grid[1:])
        signature = (header, column_values)
        if signature in seen:
            continue
        seen[signature] = idx
        keep.append(idx)
    if len(keep) == len(grid[0]):
        return grid
    return [[row[i] if i < len(row) else "" for i in keep] for row in grid]


def best_header_row(grid: List[List[str]], candidate_headers: List[str], lookahead: int = 5) -> Tuple[int, List[str]]:
    """
    Pick the most likely header row by matching configured candidates.

    We scan up to `lookahead` rows and score based on candidate header matches.
    """
    cand = set(_norm(h) for h in candidate_headers if h)
    if not cand:
        for idx, row in enumerate(grid):
            if any(c.strip() for c in row):
                logger.debug("Header row selected (no candidates)", selected_index=idx, lookahead_rows=len(grid))
                return idx, row
        logger.debug("Header row defaulted to first row (no candidates)", selected_index=0)
        return 0, grid[0] if grid else []
    best_idx, best_score = 0, -1
    lookahead_rows = min(lookahead, len(grid))
    for i in range(lookahead_rows):
        row = grid[i]
        score = 0
        for cell in row:
            cn = _norm(cell)
            if cn and (cn in cand or any(c in cn or cn in c for c in cand)):
                score += 1
        if score > best_score:
            best_score, best_idx = score, i
    logger.debug("Header row selected", selected_index=best_idx, best_score=best_score, lookahead_rows=lookahead_rows, candidate_count=len(cand))
    return best_idx, grid[best_idx]


def build_col_index(header_row: List[str]) -> Dict[str, int]:
    """Map normalized header labels to their column indices for lookup later."""
    col_index: Dict[str, int] = {}
    for i, h in enumerate(header_row):
        hn = _norm(h)
        if hn and hn not in col_index:
            col_index[hn] = i
    return col_index


def get_by_header(row: List[str], col_index: Dict[str, int], header_label: str) -> str:
    """Safely fetch a cell value by header name; returns empty string if missing/out of range."""
    if not header_label:
        return ""
    idx = col_index.get(_norm(header_label))
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _load_contact_mapping(tenant_id: str, contact_id: str) -> Tuple[Dict[str, Any], Dict[str, str], Dict[str, str], List[str], str]:  # pylint: disable=too-many-branches
    """
    Load contact-specific mapping config and normalize it for table extraction.

    Returns:
    - items_template: base template used to seed each row
    - simple_map: field -> header mapping (e.g. "date" -> "Invoice Date")
    - raw_map: optional raw header mapping for passthrough fields
    - total_candidates: header labels that should be interpreted as totals
    - date_format: required parsing format for dates
    """
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

    allowed_fields = set(StatementItem.model_fields.keys()) - {"raw", "statement_item_id", "item_type"}
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

    return items_template, simple_map, raw_map, total_candidates, date_format


def _prepare_header_context(
    grid: List[List[str]], candidates: List[str], primary_header_row: Optional[List[str]], primary_col_index: Optional[Dict[str, int]],
    ) -> Tuple[List[str], Dict[str, int], List[List[str]], bool, List[str], Dict[str, int]]:
    """
    Determine header row/columns and data rows for a table grid.

    For the first page we detect the header row; subsequent pages reuse the
    primary header unless the current grid appears to repeat it.
    """
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

    return header_row, col_index, data_rows, header_detected, primary_header_row, primary_col_index or {}


def _persist_raw_headers(tenant_id: str, contact_id: str, header_row: List[str]) -> None:
    """Persist newly-seen raw headers to contact config for future mapping."""
    try:
        cfg_existing: Dict[str, Any] = {}
        try:
            cfg_existing = get_contact_config(tenant_id, contact_id)
        except Exception:  # pylint: disable=broad-exception-caught
            cfg_existing = {}

        updated = False
        raw_val = cfg_existing.get("raw")
        root_raw: Dict[str, Any] = dict(raw_val) if isinstance(raw_val, dict) else {}
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
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.info("[table_to_json] failed to persist raw headers", error=e)


def _map_row_to_item(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
    row: List[str], header_row: List[str], col_index: Dict[str, int], items_template: Dict[str, Any],simple_map: Dict[str, str],
    raw_map: Dict[str, str], configured_amount_headers: List[Tuple[Optional[int], str]], date_format: str, statement_id: Optional[str], item_counter: int,
) -> Tuple[StatementItem, List[str], Dict[str, Any], Dict[str, Any]]:
    """
    Map a single data row into a `StatementItem` plus extraction metadata.

    Returns the item, flags, extracted simple fields, and extracted raw fields
    (for debug/audit output in `_flags`).
    """
    # Build a raw row map keyed by the header text.
    full_raw: Dict[str, Any] = {}
    for col_idx, header_value in enumerate(header_row):
        label = str(header_value or "").strip() or f"column_{col_idx}"
        cell_value = row[col_idx] if col_idx < len(row) else ""
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
        idx = col_index.get(_norm(header_name))
        actual_header = header_row[idx] if idx is not None and idx < len(header_row) else header_name
        cell = get_by_header(row, col_index, header_name)
        value: Any = cell
        if field in {"date", "due_date"}:
            try:
                parsed = parse_with_format(cell, date_format)
            except ValueError as err:
                raise ValueError(f"Failed to parse '{cell}' using format '{date_format}'") from err
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
        idx = col_index.get(_norm(chosen_header))
        actual_header = header_row[idx] if idx is not None and idx < len(header_row) else chosen_header
        canonical_header = str(actual_header or chosen_header)
        val = get_by_header(row, col_index, chosen_header)
        raw_obj[canonical_header] = val
        extracted_raw[canonical_header] = {"header": canonical_header, "value": val}
    # Always store the raw row; fall back to full row if no raw mapping provided.
    row_obj["raw"] = raw_obj if raw_obj else full_raw

    total_entries: Dict[str, Any] = {}
    for header_idx, header_label in configured_amount_headers:
        if header_idx is None or header_idx >= len(row):
            continue
        cell_value = row[header_idx]
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

    row_obj["statement_item_id"] = _generate_statement_item_id(statement_id, item_counter)
    stmt_item = StatementItem(**row_obj)

    raw_extracted = extracted_raw or {}
    if full_raw:
        raw_extracted = {**{k: {"header": k, "value": v} for k, v in full_raw.items()}, **raw_extracted}

    return stmt_item, flags, extracted_simple, raw_extracted


def select_relevant_tables_per_page(tables_with_pages: List["TableOnPage"], candidates: List[str], small_table_penalty: float = 2.5) -> List["TableOnPage"]:  # pylint: disable=too-many-locals
    """
    Choose the best table per page when multiple candidates exist.

    Tables are scored using:
    - header candidate matches
    - how many early rows look like dates
    - overall table size
    """
    if not tables_with_pages:
        return []
    cand_set = {c.strip().lower() for c in candidates if c}
    date_re = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")
    by_page: Dict[int, List[List[List[str]]]] = {}
    for t in tables_with_pages:
        by_page.setdefault(int(t["page"]), []).append(t["grid"])
    selected: List["TableOnPage"] = []
    for page, grids in sorted(by_page.items()):
        logger.debug("Evaluating tables for page", page=page, table_count=len(grids))
        best_grid, best_score = None, float("-inf")
        for grid_idx, grid in enumerate(grids):
            if not grid:
                continue
            hdr_idx, header_row = best_header_row(grid, list(cand_set))
            data_rows = grid[hdr_idx + 1 :]
            header_norm = [_norm(h) for h in header_row]
            header_hits = sum(
                1
                for h in header_norm
                if h in cand_set or any(c in h or h in c for c in cand_set)
            )
            date_hits = sum(
                1 for r in data_rows[:10] if r and date_re.match((r[0] or "").strip())
            )
            rows = len(grid)
            cols = len(grid[0]) if grid and grid[0] else 0
            size_bonus = rows * cols
            penalty = small_table_penalty if len(data_rows) <= 1 else 0.0
            score = header_hits * 10 + date_hits * 2 + size_bonus * 0.001 - penalty
            logger.debug("Table score", page=page, table_index=grid_idx, rows=rows, cols=cols, header_hits=header_hits, date_hits=date_hits,penalty=penalty, score=score)
            if score > best_score:
                best_score, best_grid = score, grid
        if best_grid is None:
            best_grid = max(grids, key=lambda g: (len(g), len(g[0]) if g else 0))
        selected.append({"page": page, "grid": best_grid})
        logger.debug("Selected table for page", page=page, rows=len(best_grid), cols=len(best_grid[0]) if best_grid and best_grid[0] else 0, best_score=best_score)
    return selected


def table_to_json(tables_with_pages: List["TableOnPage"], tenant_id: str, contact_id: str,statement_id: Optional[str] = None) -> Dict[str, Any]:  # pylint: disable=too-many-locals,too-many-statements
    """
    Convert Textract table grids into structured statement JSON.

    Uses contact-specific config to map headers to fields, parse dates, and
    extract totals. Returns a dict that matches `SupplierStatement.model_dump()`,
    with optional `_flags` metadata describing extraction issues.
    """
    # Main transformation: map Textract tables into structured statement items using contact-specific config
    items_template, simple_map, raw_map, total_candidates, date_format = _load_contact_mapping(tenant_id=tenant_id, contact_id=contact_id)

    candidates = list(simple_map.values())
    candidates.extend(list(raw_map.keys()))
    candidates.extend([v for v in raw_map.values() if v])
    logger.debug(
        "Table mapping config", tenant_id=tenant_id, contact_id=contact_id, statement_id=statement_id, candidate_headers=len(candidates),
        simple_map_fields=len(simple_map), raw_map_fields=len(raw_map), total_candidates=len(total_candidates),
    )

    # Pick the most relevant table per page before mapping rows to fields.
    selected = select_relevant_tables_per_page(tables_with_pages, candidates=candidates)
    logger.debug("Selected tables", count=len(selected))

    items: List[StatementItem] = []
    per_item_flags: List[List[str]] = []
    item_flags: List[List[Dict[str, Any]]] = []
    item_counter = 0
    primary_header_row: Optional[List[str]] = None
    primary_col_index: Optional[Dict[str, int]] = None
    for _page_number, entry in enumerate(selected, start=1):
        grid = entry["grid"]
        grid = _dedupe_grid_columns(grid)
        logger.debug("Processing table grid", page=entry["page"],rows=len(grid), cols=len(grid[0]) if grid and grid[0] else 0)

        header_row: List[str]
        col_index: Dict[str, int]
        data_rows: List[List[str]]
        header_detected = False

        header_row, col_index, data_rows, header_detected, primary_header_row, primary_col_index = _prepare_header_context(grid, candidates, primary_header_row, primary_col_index)

        logger.debug("Header detection result", page=entry["page"], header_detected=header_detected,header_len=len(header_row), data_rows=len(data_rows))

        if header_detected:
            _persist_raw_headers(tenant_id, contact_id, header_row)

        # Pre-compute which columns should be interpreted as "total" values.
        configured_amount_headers: List[Tuple[Optional[int], str]] = []
        for cand in total_candidates:
            clean = cand.strip()
            if not clean:
                continue
            idx = col_index.get(_norm(clean))
            configured_amount_headers.append((idx, clean))

        for i_row, r in enumerate(data_rows, start=1):
            if not any((c or "").strip() for c in r):
                continue

            item_counter += 1
            stmt_item, flags, extracted_simple, raw_extracted = _map_row_to_item(
                row=r, header_row=header_row, col_index=col_index, items_template=items_template, simple_map=simple_map, raw_map=raw_map,
                configured_amount_headers=configured_amount_headers, date_format=date_format, statement_id=statement_id, item_counter=item_counter,
            )
            items.append(stmt_item)
            per_item_flags.append(flags)

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

    # Derive earliest/latest dates across all items.
    earliest_date, latest_date = _derive_date_range(items)
    statement = SupplierStatement(statement_items=items, earliest_item_date=earliest_date, latest_item_date=latest_date)

    output = statement.model_dump()

    # Attach per-item flags for UI compatibility (the UI reads `item["_flags"]`).
    # Keep flags as a simple list of strings, preserving insertion order and removing duplicates.
    out_items = output.get("statement_items") or []
    if isinstance(out_items, list):
        for idx, flags in enumerate(per_item_flags):
            if idx >= len(out_items) or not flags:
                continue
            if isinstance(out_items[idx], dict):
                out_items[idx]["_flags"] = list(dict.fromkeys(flags))

    if combined_flags:
        output["_flags"] = combined_flags

    return output


def _derive_date_range(items: List[StatementItem]) -> Tuple[Optional[str], Optional[str]]:
    """Compute min/max dates across all statement items."""
    dates = []
    for item in items:
        if item.date:
            dates.append(item.date)
    if not dates:
        return None, None
    dates_sorted = sorted(dates)
    return dates_sorted[0], dates_sorted[-1]


def _clean_currency(value: Any) -> str:
    """Strip currency adornments and whitespace, leaving a numeric-like string."""
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
    """Convert cleaned numeric string to float when possible."""
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
    """Find the first row with any non-empty cell."""
    for idx, row in enumerate(grid):
        if any((c or "").strip() for c in row):
            return idx
    return 0


def _rows_match_header(row: List[str], header: List[str]) -> bool:
    """Check whether a row resembles the header (used when carrying header across pages)."""
    if not row or not header:
        return False
    header_norm = [_norm(h) for h in header]
    row_norm = [_norm(c) for c in row]
    overlap = sum(1 for c in row_norm if c and c in header_norm)
    return overlap >= max(1, len(header_norm) // 2)
