import re
from typing import Any, Dict, List, Tuple


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
