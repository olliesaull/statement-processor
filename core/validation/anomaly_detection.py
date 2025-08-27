from typing import Any, Dict, List, Tuple, Type, get_args, get_origin, Union
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from core.models import StatementItem

# ---------------- helpers ----------------
def _has(x): return not (x is None or (isinstance(x, str) and x.strip() == ""))
def _num(x):
    if isinstance(x, (int, float)): return float(x)
    if isinstance(x, str):
        t = x.replace(",", "").replace(" ", "").strip()
        try: return float(t) if t else 0.0
        except: return 0.0
    return 0.0

def _num_with_flag(x) -> Tuple[float, float]:
    """
    Returns (value, parse_error_flag).
    parse_error_flag is 1.0 if a numeric string failed to parse, else 0.0.
    """
    if isinstance(x, (int, float)):
        return float(x), 0.0
    if isinstance(x, str):
        t = x.replace(",", "").replace(" ", "").strip()
        if not t:
            return 0.0, 0.0
        try:
            return float(t), 0.0
        except Exception:
            return 0.0, 1.0
    return 0.0, 0.0

def _day(s):
    if not isinstance(s, str): return 0
    try:
        d = int(s.split("/")[0])
        return d if 1 <= d <= 31 else 0
    except:
        return 0

def _norm_doctype(s: str) -> str:
    t = (s or "").lower()
    if "inv" in t or "invoice" in t: return "invoice"
    if any(k in t for k in ["pymt", "pmt", "pay", "receipt", "py"]): return "payment"
    if ("credit" in t and "note" in t) or " cr" in t: return "credit_note"
    if ("debit" in t and "note" in t) or " dn" in t: return "debit_note"
    if "adj" in t: return "adjustment"
    if "fee" in t or "charge" in t: return "charge"
    return t or "_global"

def _flatten_union(ann):
    """Yield atomic types inside nested Unions/Optionals."""
    if get_origin(ann) is Union:
        for a in get_args(ann):
            yield from _flatten_union(a)
    else:
        yield ann

def _is_numeric_annotation(ann) -> bool:
    return any(a in (int, float) for a in _flatten_union(ann))

# ---------------- schema-driven feature builder ----------------
def build_df_from_schema(
    items: List[Dict[str, Any]],
    *,
    model: Type[StatementItem] = StatementItem,
    doc_type_field: str = "document_type",
    date_path: Tuple[str, str] = ("transaction_date", "value"),
    amount_pair: Tuple[str, str] = ("debit", "credit"),
) -> Tuple[pd.DataFrame, List[str]]:
    """
    Build a feature DataFrame from StatementItem schema.
    Returns (df, feature_columns).
    """
    # infer fields from Pydantic model
    numeric_fields = []
    string_fields = []
    for name, field in model.model_fields.items():  # Pydantic v2
        if name in {"raw"}:  # ignore raw dict
            continue
        ann = field.annotation
        if name == date_path[0]:  # nested date model handled separately
            continue
        if _is_numeric_annotation(ann):
            numeric_fields.append(name)
        elif ann is str:
            string_fields.append(name)
        # else: skip nested models (only date is expected)

    # rows
    rows = []
    for i, it in enumerate(items):
        row = {"idx": i, "doctype_group": _norm_doctype(it.get(doc_type_field, ""))}

        # date features
        td = ((it.get(date_path[0]) or {}) or {}).get(date_path[1], "")
        row["date_present"] = 1.0 if _has(td) else 0.0
        row["day_of_month"] = float(_day(td))

        # string features (presence + length)
        for f in string_fields:
            v = it.get(f, "")
            row[f"{f}_present"] = 1.0 if _has(v) else 0.0
            row[f"len_{f}"] = float(len(str(v).strip())) if _has(v) else 0.0

        # numeric features (presence + log-abs + sign)
        for f in numeric_fields:
            v = it.get(f, "")
            val = _num(v)
            row[f"{f}_present"] = 1.0 if _has(v) else 0.0
            row[f"log_abs_{f}"] = float(np.log1p(abs(val)))
            row[f"sign_{f}"] = 0.0 if val == 0 else (1.0 if val > 0 else -1.0)

        # orientation features if amount pair exists
        a1, a2 = amount_pair
        has_a1 = _has(it.get(a1))
        has_a2 = _has(it.get(a2))
        row["debit_only"]  = 1.0 if has_a1 and not has_a2 else 0.0
        row["credit_only"] = 1.0 if has_a2 and not has_a1 else 0.0
        row["both_amounts"] = 1.0 if has_a1 and has_a2 else 0.0
        row["no_amounts"]   = 1.0 if (not has_a1 and not has_a2) else 0.0

        rows.append(row)

    df = pd.DataFrame(rows)

    # assemble feature columns dynamically
    feature_cols = ["date_present", "day_of_month"]
    # string-derived
    for f in string_fields:
        feature_cols += [f"{f}_present", f"len_{f}"]
    # numeric-derived
    for f in numeric_fields:
        feature_cols += [f"{f}_present", f"log_abs_{f}", f"sign_{f}"]
    # orientation
    feature_cols += ["debit_only", "credit_only", "both_amounts", "no_amounts"]

    return df, feature_cols

# ---------------- robust stats for explanations ----------------
def _robust_stats(X: np.ndarray):
    med = np.median(X, axis=0)
    q1 = np.quantile(X, 0.25, axis=0)
    q3 = np.quantile(X, 0.75, axis=0)
    iqr = q3 - q1
    iqr[iqr == 0] = 1e-9
    return med, iqr

def _explain_row(x: np.ndarray, med: np.ndarray, iqr: np.ndarray, names: List[str], top_k: int = 6):
    rz = (x - med) / iqr
    idxs = np.argsort(np.abs(rz))[::-1][:top_k]
    return [
        {"feature": names[j], "value": float(x[j]), "median": float(med[j]),
         "iqr": float(iqr[j]), "robust_z": float(rz[j])}
        for j in idxs
    ]

# ---------------- main API ----------------
def apply_outlier_flags(
    statement: Dict[str, Any],
    *,
    remove: bool = False,
    one_based_index: bool = False,
    threshold_method: str = "iqr",  # or "percentile", "zscore"
    percentile: float = 0.98,
    iqr_k: float = 1.5,
    zscore_z: float = 3.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    items = statement.get("statement_items", []) or []
    if not items:
        return statement, {"total": 0, "flagged": 0, "flagged_items": [], "profiles": {}}

    # Build features from schema
    df, feature_cols = build_df_from_schema(items)  # <- dynamic
    # group by doc type with small-group fallback to global
    groups = {}
    for g, gdf in df.groupby("doctype_group"):
        groups[g] = gdf.index.tolist()
    # fold tiny groups into _global
    idx_global = set(groups.get("_global", []))
    for g, idxs in list(groups.items()):
        if g != "_global" and len(idxs) < 8:
            idx_global.update(idxs)
            del groups[g]
    groups["_global"] = sorted(idx_global)

    flagged_items = []
    keep_mask = np.ones(len(items), dtype=bool)

    for g, idxs in groups.items():
        if not idxs:
            continue
        X = df.loc[idxs, feature_cols].to_numpy(dtype=float)

        # model & scores
        scaler = RobustScaler()
        Xs = scaler.fit_transform(X)
        # CHANGED: set contamination low so only clearly different rows are flagged
        clf = IsolationForest(n_estimators=200, contamination=0.02, random_state=42, n_jobs=-1)  # CHANGED
        clf.fit(Xs)
        scores = -clf.score_samples(Xs)  # higher == more anomalous

        # threshold
        if threshold_method == "percentile":
            thr = float(np.quantile(scores, percentile))
        elif threshold_method == "zscore":
            mu, sd = float(np.mean(scores)), float(np.std(scores) or 1.0)
            thr = mu + zscore_z * sd
        else:
            q1, q3 = np.quantile(scores, [0.25, 0.75]); iqr = q3 - q1
            thr = q3 + iqr_k * iqr

        # explanations
        med, iqr = _robust_stats(X)

        for local_i, score in enumerate(scores):
            global_i = idxs[local_i]
            if score > thr:
                keep_mask[global_i] = not remove
                x = X[local_i]
                flagged_items.append({
                    "index": (global_i + 1) if one_based_index else global_i,
                    "doc_type_group": df.loc[global_i, "doctype_group"],
                    "score": float(score),
                    "reasons": ["ml-outlier"],
                    "top_features": _explain_row(x, med, iqr, feature_cols, top_k=6),
                })

    # attach/remove flags
    if remove:
        statement["statement_items"] = [it for i, it in enumerate(items) if keep_mask[i]]
    else:
        for i, it in enumerate(items):
            if not keep_mask[i]:
                it.setdefault("_flags", []).append("ml-outlier")

    summary = {
        "total": len(items),
        "flagged": len(flagged_items),
        "flagged_items": flagged_items,
        "profiles": {g: {"count": len(idxs)} for g, idxs in groups.items()},
        "feature_columns": feature_cols,  # for transparency/debug
    }
    return statement, summary
