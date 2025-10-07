from typing import Any, Callable, Dict, List, Optional, Tuple, TypedDict

import numpy as np

FLAG_LABEL = "ml-outlier"
DEFAULT_Z_THRESHOLD = 3.5
MIN_SAMPLE_SIZE = 5


def _parse_number(value: Any) -> Optional[float]:
    """Convert common numeric representations to float, returning None when blank or invalid."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value)
    except Exception:
        return None
    cleaned = (
        text.replace(",", "")
        .replace(" ", "")
        .replace("£", "")
        .strip()
    )
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = f"-{cleaned[1:-1]}"
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _sum_amount_due(value: Any) -> Optional[float]:
    """Amount due can come back as a dict of buckets, list entries, or a single value."""
    if value is None:
        return None
    totals: List[float] = []
    if isinstance(value, dict):
        for v in value.values():
            parsed = _parse_number(v)
            if parsed is not None:
                totals.append(parsed)
    elif isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict):
                parsed = _parse_number(entry.get("value"))
                if parsed is not None:
                    totals.append(parsed)
    else:
        parsed = _parse_number(value)
        if parsed is not None:
            totals.append(parsed)
    if not totals:
        return None
    return float(sum(totals))


def _string_length(value: Any) -> Optional[float]:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return None
    stripped = value.strip()
    if not stripped:
        return None
    return float(len(stripped))


class FieldSpec(TypedDict):
    extractor: Callable[[Dict[str, Any]], Optional[float]]
    raw_accessor: Callable[[Dict[str, Any]], Any]
    metric: str


FIELD_SPECS: Dict[str, FieldSpec] = {
    "total": {
        "extractor": lambda it: _parse_number(it.get("total")),
        "raw_accessor": lambda it: it.get("total"),
        "metric": "value",
    },
    "amount_paid": {
        "extractor": lambda it: _parse_number(it.get("amount_paid")),
        "raw_accessor": lambda it: it.get("amount_paid"),
        "metric": "value",
    },
    "amount_due": {
        "extractor": lambda it: _sum_amount_due(it.get("amount_due")),
        "raw_accessor": lambda it: it.get("amount_due"),
        "metric": "value",
    },
    "number": {
        "extractor": lambda it: _string_length(it.get("number")),
        "raw_accessor": lambda it: it.get("number"),
        "metric": "length",
    },
    "reference": {
        "extractor": lambda it: _string_length(it.get("reference")),
        "raw_accessor": lambda it: it.get("reference"),
        "metric": "length",
    },
}


def _collect_field_data(items: List[Dict[str, Any]], extractor) -> Tuple[List[Optional[float]], List[float]]:
    per_item: List[Optional[float]] = []
    observed: List[float] = []
    for item in items:
        value = extractor(item)
        per_item.append(value)
        if value is not None:
            observed.append(float(value))
    return per_item, observed


def _robust_center_scale(values: List[float]) -> Tuple[float, Optional[float], str]:
    """Return (median, scale, method). Scale can be None if the data are constant."""
    arr = np.array(values, dtype=float)
    median = float(np.median(arr))
    q1 = float(np.percentile(arr, 25))
    q3 = float(np.percentile(arr, 75))
    iqr = q3 - q1
    if iqr > 1e-9:
        return median, float(iqr), "iqr"
    mad = float(np.median(np.abs(arr - median)))
    scale = mad * 1.4826
    if scale > 1e-9:
        return median, float(scale), "mad"
    unique_vals = np.unique(arr)
    if len(unique_vals) > 1:
        return median, 1.0, "unit_fallback"
    return median, None, "constant"


def apply_outlier_flags(
    statement: Dict[str, Any],
    *,
    remove: bool = False,
    one_based_index: bool = False,
    threshold_method: str = "iqr",  # retained for backwards compatibility
    percentile: float = 0.98,  # unused (legacy)
    iqr_k: float = 1.5,  # unused (legacy)
    zscore_z: float = 3.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Flag rows that sit far outside the bulk of the data for key numeric fields using robust z-scores.

    The previous IsolationForest approach has been replaced with a simpler, deterministic strategy.
    Only `threshold_method="zscore"` affects behaviour (via `zscore_z`). Other parameters are kept
    for call-site compatibility but are otherwise ignored.
    """
    _ = (percentile, iqr_k)  # legacy parameters are intentionally ignored

    items = statement.get("statement_items", []) or []
    if not items:
        return statement, {"total": 0, "flagged": 0, "flagged_items": [], "field_stats": {}}

    threshold = float(zscore_z) if threshold_method == "zscore" else DEFAULT_Z_THRESHOLD

    field_values: Dict[str, List[Optional[float]]] = {}
    field_stats: Dict[str, Dict[str, Any]] = {}
    active_fields: Dict[str, Dict[str, float]] = {}

    for field, spec in FIELD_SPECS.items():
        extractor = spec["extractor"]
        per_item, observed = _collect_field_data(items, extractor)
        field_values[field] = per_item
        summary: Dict[str, Any] = {"count": len(observed), "metric": spec["metric"]}
        if len(observed) >= MIN_SAMPLE_SIZE:
            median, scale, method = _robust_center_scale(observed)
            summary.update({"median": median, "scale": scale, "scale_method": method})
            if scale is not None:
                active_fields[field] = {"median": median, "scale": scale}
        field_stats[field] = summary

    flagged_items: List[Dict[str, Any]] = []
    flagged_positions: List[int] = []
    keep_mask = [True] * len(items)

    for idx, item in enumerate(items):
        field_details: List[Dict[str, Any]] = []
        score_components: List[float] = []

        for field, stats in active_fields.items():
            value = field_values[field][idx]
            if value is None:
                continue
            z = (value - stats["median"]) / stats["scale"]
            if abs(z) >= threshold:
                spec = FIELD_SPECS[field]
                field_details.append(
                    {
                        "field": field,
                        "value": float(value),
                        "median": float(stats["median"]),
                        "scale": float(stats["scale"]),
                        "z_score": float(z),
                        "metric": spec["metric"],
                        "raw_value": spec["raw_accessor"](item),
                    }
                )
                score_components.append(abs(z))

        if field_details:
            flagged_positions.append(idx)
            if remove:
                keep_mask[idx] = False
            score = max(score_components)
            flagged_items.append(
                {
                    "index": (idx + 1) if one_based_index else idx,
                    "score": float(score),
                    "reasons": [FLAG_LABEL],
                    "details": field_details,
                }
            )

    if remove:
        statement["statement_items"] = [it for i, it in enumerate(items) if keep_mask[i]]
    else:
        for idx in flagged_positions:
            flags = items[idx].setdefault("_flags", [])
            if FLAG_LABEL not in flags:
                flags.append(FLAG_LABEL)

    flagged_items.sort(key=lambda entry: entry["score"], reverse=True)

    summary = {
        "total": len(items),
        "flagged": len(flagged_items),
        "flagged_items": flagged_items,
        "field_stats": field_stats,
    }

    print("*"*88)
    print(summary)
    print("*"*88)

    return statement, summary
