# flag_outliers.py  (EXPLAINABLE IsolationForest)
import math
from collections import defaultdict, Counter
from typing import Any, Dict, List, Tuple

import numpy as np

try:
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import RobustScaler
except Exception as e:
    raise ImportError(
        "This outlier module requires scikit-learn. Install with `pip install scikit-learn`."
    ) from e

# ----------------- feature meta -----------------
FEATURE_NAMES = [
    "date_present",              # 0
    "doctype_present",           # 1
    "supplier_ref_present",      # 2
    "customer_ref_present",      # 3
    "description_present",       # 4
    "debit_present",             # 5
    "credit_present",            # 6
    "invbal_present",            # 7
    "balance_present",           # 8
    "day_of_month",              # 9
    "log_abs_debit",             # 10
    "log_abs_credit",            # 11
    "log_abs_balance",           # 12
    "sign_debit",                # 13  (-1,0,1)
    "sign_credit",               # 14
    "sign_balance",              # 15
    "len_description",           # 16
    "len_supplier_ref",          # 17
    "len_customer_ref",          # 18
    "debit_only",                # 19 (1 if debit and not credit)
    "credit_only",               # 20
    "both_amounts",              # 21
    "no_amounts",                # 22
]

HUMAN_LABEL = {
    "date_present": "Date present",
    "doctype_present": "Document type present",
    "supplier_ref_present": "Supplier ref present",
    "customer_ref_present": "Customer ref present",
    "description_present": "Description present",
    "debit_present": "Debit present",
    "credit_present": "Credit present",
    "invbal_present": "Invoice balance present",
    "balance_present": "Balance present",
    "day_of_month": "Day of month",
    "log_abs_debit": "Debit magnitude",
    "log_abs_credit": "Credit magnitude",
    "log_abs_balance": "Balance magnitude",
    "sign_debit": "Debit sign",
    "sign_credit": "Credit sign",
    "sign_balance": "Balance sign",
    "len_description": "Description length",
    "len_supplier_ref": "Supplier ref length",
    "len_customer_ref": "Customer ref length",
    "debit_only": "Debit-only row",
    "credit_only": "Credit-only row",
    "both_amounts": "Both debit & credit",
    "no_amounts": "No debit or credit",
}

# ----------------- tiny utils -----------------
def _has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return v.strip() != ""
    return True

def _get_num(v: Any) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.replace(",", "").replace(" ", "").strip()
        try:
            return float(t) if t != "" else 0.0
        except ValueError:
            return 0.0
    return 0.0

def _len_s(s: Any) -> int:
    return len(str(s).strip()) if _has_value(s) else 0

def _parse_day_of_month(date_str: Any) -> int:
    if not isinstance(date_str, str):
        return 0
    s = date_str.strip()
    parts = s.split("/")
    if len(parts) >= 1:
        try:
            d = int(parts[0])
            return d if 1 <= d <= 31 else 0
        except ValueError:
            return 0
    return 0

def _norm_doc_type(s: str) -> str:
    t = (s or "").strip().lower()
    if not t:
        return ""
    if any(k in t for k in ("inv", "invoice")):
        return "invoice"
    if any(k in t for k in ("pymt", "pmt", "paymnt", "pay", "receipt", "py")):
        return "payment"
    if any(k in t for k in ("credit note", "crn", "cn")):
        return "credit_note"
    if any(k in t for k in ("debit note", "dbn", "dn")):
        return "debit_note"
    if "adj" in t or "adjust" in t:
        return "adjustment"
    if "fee" in t or "charge" in t:
        return "charge"
    return t

# ----------------- features -----------------
def _build_feature_matrix(items: List[Dict[str, Any]]) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
    feats: List[List[float]] = []
    meta: List[Dict[str, Any]] = []

    for idx, it in enumerate(items):
        td = (it.get("transaction_date") or {}).get("value", "")
        doc_type_raw = it.get("document_type", "")
        doc_type = _norm_doc_type(doc_type_raw)

        debit = it.get("debit", "")
        credit = it.get("credit", "")
        invbal = it.get("invoice_balance", "")
        bal = it.get("balance", "")

        d = _get_num(debit)
        c = _get_num(credit)
        b = _get_num(invbal if _has_value(invbal) else bal)

        sign_d = 0 if d == 0 else (1 if d > 0 else -1)
        sign_c = 0 if c == 0 else (1 if c > 0 else -1)
        sign_b = 0 if b == 0 else (1 if b > 0 else -1)

        feat = [
            1.0 if _has_value(td) else 0.0,
            1.0 if _has_value(doc_type_raw) else 0.0,
            1.0 if _has_value(it.get("supplier_reference")) else 0.0,
            1.0 if _has_value(it.get("customer_reference")) else 0.0,
            1.0 if _has_value(it.get("description_details")) else 0.0,
            1.0 if _has_value(debit) else 0.0,
            1.0 if _has_value(credit) else 0.0,
            1.0 if _has_value(invbal) else 0.0,
            1.0 if _has_value(bal) else 0.0,
            float(_parse_day_of_month(td)),
            float(np.log1p(abs(d))),
            float(np.log1p(abs(c))),
            float(np.log1p(abs(b))),
            float(sign_d),
            float(sign_c),
            float(sign_b),
            float(_len_s(it.get("description_details"))),
            float(_len_s(it.get("supplier_reference"))),
            float(_len_s(it.get("customer_reference"))),
            1.0 if (_has_value(debit) and not _has_value(credit)) else 0.0,
            1.0 if (_has_value(credit) and not _has_value(debit)) else 0.0,
            1.0 if (_has_value(debit) and _has_value(credit)) else 0.0,
            1.0 if (not _has_value(debit) and not _has_value(credit)) else 0.0,
        ]

        feats.append(feat)
        meta.append({
            "index": idx,
            "doc_type_group": doc_type or "_global",
            "date": td,
            "document_type": it.get("document_type", ""),
            "supplier_reference": it.get("supplier_reference", ""),
            "customer_reference": it.get("customer_reference", ""),
            "debit": debit,
            "credit": credit,
            "balance": bal,
        })

    X = np.array(feats, dtype=np.float64)
    return X, meta

# ----------------- model & threshold -----------------
def _scores_isoforest(X: np.ndarray, random_state: int = 42) -> Tuple[np.ndarray, RobustScaler]:
    scaler = RobustScaler()
    Xs = scaler.fit_transform(X)
    clf = IsolationForest(
        n_estimators=200,
        max_samples="auto",
        bootstrap=False,
        contamination="auto",
        random_state=random_state,
        n_jobs=-1,
    )
    clf.fit(Xs)
    scores = -clf.score_samples(Xs)  # higher = more anomalous
    return scores, scaler

def _threshold(scores: np.ndarray, method: str = "iqr", *, iqr_k: float = 1.5, z: float = 3.0, pct: float = 0.98) -> float:
    if scores.size == 0:
        return float("inf")
    if method == "percentile":
        return float(np.quantile(scores, pct))
    if method == "zscore":
        mu = float(np.mean(scores))
        sd = float(np.std(scores)) or 1.0
        return mu + z * sd
    q1, q3 = np.quantile(scores, [0.25, 0.75])
    iqr = q3 - q1
    return q3 + iqr_k * iqr

# ----------------- explanations -----------------
def _robust_stats(X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    med = np.median(X, axis=0)
    q1 = np.quantile(X, 0.25, axis=0)
    q3 = np.quantile(X, 0.75, axis=0)
    iqr = q3 - q1
    iqr[iqr == 0] = 1e-9
    return med, iqr

def _row_explanations(x: np.ndarray, med: np.ndarray, iqr: np.ndarray, top_k: int = 5) -> List[Dict[str, Any]]:
    rz = (x - med) / iqr
    # top absolute deviations
    idxs = np.argsort(np.abs(rz))[::-1][:top_k]
    out = []
    for j in idxs:
        name = FEATURE_NAMES[j] if j < len(FEATURE_NAMES) else f"f{j}"
        out.append({
            "feature": name,
            "label": HUMAN_LABEL.get(name, name),
            "value": float(x[j]),
            "median": float(med[j]),
            "iqr": float(iqr[j]),
            "robust_z": float(rz[j]),
        })
    return out

def _human_reasons(x: np.ndarray, med: np.ndarray) -> List[str]:
    reasons = []
    # presence flips
    def _flip(name):
        j = FEATURE_NAMES.index(name)
        v, m = x[j], med[j]
        if m >= 0.5 and v < 0.5:
            reasons.append(f"Missing {HUMAN_LABEL[name]} while peers have it")
        if m < 0.5 and v >= 0.5:
            reasons.append(f"Has {HUMAN_LABEL[name]} while peers usually don't")

    for n in ["date_present", "supplier_ref_present", "customer_ref_present", "description_present",
              "debit_present", "credit_present"]:
        _flip(n)

    # orientation anomalies
    for n in ["both_amounts", "no_amounts"]:
        j = FEATURE_NAMES.index(n)
        if x[j] >= 0.5 and med[j] < 0.5:
            reasons.append(HUMAN_LABEL[n])

    # magnitude anomalies (compare to median)
    for n in ["log_abs_debit", "log_abs_credit", "log_abs_balance"]:
        j = FEATURE_NAMES.index(n)
        if x[j] > med[j] + 2.0:  # ~ large vs median (on log-scale)
            pretty = HUMAN_LABEL[n].replace("log_abs_", "").replace(" magnitude", "").capitalize()
            reasons.append(f"Unusually large {pretty}")
    return reasons

# ----------------- public API -----------------
def apply_outlier_flags(
    statement: Dict[str, Any],
    *,
    remove: bool = False,
    one_based_index: bool = False,
    group_by_doc_type: bool = True,
    min_group_size: int = 8,
    threshold_method: str = "iqr",
    percentile: float = 0.98,
    iqr_k: float = 1.5,
    zscore_z: float = 3.0,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    IsolationForest scoring + statistical threshold + EXPLANATIONS.
    Explanations list the top deviating features (robust z-scores) and readable reasons.
    """
    items = statement.get("statement_items", []) or []
    if not items:
        return statement, {"total": 0, "flagged": 0, "reasons": {}, "flagged_items": [], "profiles": {}}

    X_all, meta_all = _build_feature_matrix(items)

    # group by document type (or global)
    groups: Dict[str, List[int]] = defaultdict(list)
    for i, m in enumerate(meta_all):
        g = (m["doc_type_group"] if group_by_doc_type else "_global") or "_global"
        groups[g].append(i)

    # move tiny groups into _global
    for g in list(groups.keys()):
        if g != "_global" and len(groups[g]) < min_group_size:
            groups["_global"].extend(groups[g])
            del groups[g]
    groups.setdefault("_global", [])

    flagged_mask = np.zeros(len(items), dtype=bool)
    scores_all = np.zeros(len(items), dtype=float)
    explanations: Dict[int, Dict[str, Any]] = {}
    profile_summary = {}

    for g, idxs in groups.items():
        if not idxs:
            continue
        Xg = X_all[idxs]

        # train, score, threshold
        scores, _ = _scores_isoforest(Xg)
        thr = _threshold(scores, method=threshold_method, iqr_k=iqr_k, z=zscore_z, pct=percentile)
        group_flags = scores > thr

        flagged_mask[idxs] = group_flags
        scores_all[idxs] = scores

        # robust stats for explanations
        med, iqr = _robust_stats(Xg)

        for local_i, is_flagged in enumerate(group_flags):
            if not is_flagged:
                continue
            global_i = idxs[local_i]
            x = X_all[global_i]
            top_feats = _row_explanations(x, med, iqr, top_k=6)
            reasons = _human_reasons(x, med)
            explanations[global_i] = {
                "top_features": top_feats,
                "reasons": reasons,
            }

        profile_summary[g] = {
            "count": int(len(idxs)),
            "threshold_method": threshold_method,
            "threshold": float(thr),
            "score_min": float(scores.min()),
            "score_q50": float(np.quantile(scores, 0.5)),
            "score_q90": float(np.quantile(scores, 0.9)),
            "score_max": float(scores.max()),
            "flagged": int(np.count_nonzero(group_flags)),
        }

    # attach / remove & build summary
    flagged_items = []
    new_items = []
    for i, it in enumerate(items):
        if flagged_mask[i]:
            ex = explanations.get(i, {"top_features": [], "reasons": []})
            reasons = ["ml-outlier"] + ex["reasons"]
            detail_feats = ex["top_features"]

            if remove:
                pass
            else:
                it["_flags"] = reasons
                it["_explain"] = detail_feats
                new_items.append(it)

            flagged_items.append({
                "index": (i + 1) if one_based_index else i,
                "doc_type_group": meta_all[i]["doc_type_group"],
                "score": float(scores_all[i]),
                "reasons": reasons,
                "top_features": detail_feats,
                "date": meta_all[i]["date"],
                "document_type": meta_all[i]["document_type"],
                "supplier_reference": meta_all[i]["supplier_reference"],
                "customer_reference": meta_all[i]["customer_reference"],
                "debit": meta_all[i]["debit"],
                "credit": meta_all[i]["credit"],
                "balance": meta_all[i]["balance"],
            })
        else:
            new_items.append(it)

    if remove:
        statement["statement_items"] = new_items

    # reason counts (by human reason, excluding the generic "ml-outlier")
    reason_counts = Counter([r for fi in flagged_items for r in fi["reasons"] if r != "ml-outlier"])

    summary = {
        "total": len(items),
        "flagged": len(flagged_items),
        "reasons": dict(reason_counts),
        "flagged_items": flagged_items,
        "profiles": profile_summary,
    }
    return statement, summary
