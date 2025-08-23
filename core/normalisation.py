import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

from core.models import StatementItem, StatementMeta, SupplierStatement
from utils import (
    best_header_row,
    build_col_index,
    get_by_header,
    norm,
    row_is_opening_or_carried_forward,
    to_number_if_possible,
)


def select_relevant_tables_per_page(
    tables_with_pages: List[Dict[str, Any]],
    *,
    candidates: List[str],
    small_table_penalty: float = 2.5,
) -> List[Dict[str, Any]]:
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
            header_hits = sum(1 for h in header_norm if h in cand_set or any(c in h or h in c for c in cand_set))
            date_hits = sum(1 for r in data_rows[:10] if r and date_re.match((r[0] or "").strip()))
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
    config_dir: str = "./statement_configs",
) -> Dict[str, Any]:
    """Produce canonical dict (validated by Pydantic)."""
    stem = Path(key).stem
    cfg_path = Path(config_dir) / f"{stem}.json"

    with open(cfg_path, "r", encoding="utf-8") as f:
        map_cfg: Dict[str, Any] = json.load(f)

    meta_cfg = map_cfg.get("statement_meta", {}) or {}
    items_template = deepcopy((map_cfg.get("statement_items") or [{}])[0])

    # build mapping config
    simple_map: Dict[str, str] = {}
    raw_map: Dict[str, str] = {}
    date_value_header, date_format = None, None
    for field, value in items_template.items():
        if field == "transaction_date" and isinstance(value, dict):
            date_value_header = value.get("value")
            date_format = value.get("format")
        elif field == "raw" and isinstance(value, dict):
            raw_map = value
        elif isinstance(value, str) and value.strip():
            simple_map[field] = value

    candidates = list(simple_map.values())
    if date_value_header:
        candidates.append(date_value_header)
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

            # transaction_date
            td_val = get_by_header(r, col_index, date_value_header or "")
            row_obj["transaction_date"] = {
                "value": td_val,
                "format": date_format or (row_obj.get("transaction_date") or {}).get("format", "DD/MM/YY"),
            }

            # simple fields
            for field, header_name in simple_map.items():
                cell = get_by_header(r, col_index, header_name)
                if field in {"debit", "credit", "invoice_balance", "balance"}:
                    row_obj[field] = to_number_if_possible(cell)
                else:
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
    meta = StatementMeta(**{**meta_cfg, "source_filename": Path(key).name})
    statement = SupplierStatement(statement_meta=meta, statement_items=items)
    return statement.model_dump()
