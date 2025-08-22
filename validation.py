from typing import Any, Dict, List, Tuple
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

FEATURES = [
    "date_present","doctype_present","supplier_ref_present","customer_ref_present","description_present",
    "debit_present","credit_present","invbal_present","balance_present",
    "day_of_month","log_abs_debit","log_abs_credit","log_abs_balance",
    "sign_debit","sign_credit","sign_balance",
    "len_description","len_supplier_ref","len_customer_ref",
    "debit_only","credit_only","both_amounts","no_amounts",
]

def _has(x): return not (x is None or (isinstance(x,str) and x.strip()==""))
def _num(x):
    if isinstance(x,(int,float)): return float(x)
    if isinstance(x,str):
        t = x.replace(",","").replace(" ","").strip()
        try: return float(t) if t else 0.0
        except: return 0.0
    return 0.0

def _day(s):
    if not isinstance(s,str): return 0
    try: d=int(s.split("/")[0]); return d if 1<=d<=31 else 0
    except: return 0

def _norm_doctype(s:str)->str:
    t=(s or "").lower()
    if "inv" in t or "invoice" in t: return "invoice"
    if any(k in t for k in ["pymt","pmt","pay","receipt","py"]): return "payment"
    if "credit" in t and "note" in t or " cr" in t: return "credit_note"
    if "debit" in t and "note" in t or " dn" in t: return "debit_note"
    if "adj" in t: return "adjustment"
    if "fee" in t or "charge" in t: return "charge"
    return t or "_global"

def _build_df(items: List[Dict[str,Any]]) -> pd.DataFrame:
    rows=[]
    for i,it in enumerate(items):
        td=(it.get("transaction_date") or {}).get("value","")
        d=_num(it.get("debit","")); c=_num(it.get("credit",""))
        b=_num(it.get("invoice_balance","") or it.get("balance",""))
        row={
            "idx":i,
            "doctype_group": _norm_doctype(it.get("document_type","")),
            "date_present": 1.0 if _has(td) else 0.0,
            "doctype_present": 1.0 if _has(it.get("document_type")) else 0.0,
            "supplier_ref_present": 1.0 if _has(it.get("supplier_reference")) else 0.0,
            "customer_ref_present": 1.0 if _has(it.get("customer_reference")) else 0.0,
            "description_present": 1.0 if _has(it.get("description_details")) else 0.0,
            "debit_present": 1.0 if _has(it.get("debit")) else 0.0,
            "credit_present": 1.0 if _has(it.get("credit")) else 0.0,
            "invbal_present": 1.0 if _has(it.get("invoice_balance")) else 0.0,
            "balance_present": 1.0 if _has(it.get("balance")) else 0.0,
            "day_of_month": float(_day(td)),
            "log_abs_debit": float(np.log1p(abs(d))),
            "log_abs_credit": float(np.log1p(abs(c))),
            "log_abs_balance": float(np.log1p(abs(b))),
            "sign_debit": 0.0 if d==0 else (1.0 if d>0 else -1.0),
            "sign_credit": 0.0 if c==0 else (1.0 if c>0 else -1.0),
            "sign_balance": 0.0 if b==0 else (1.0 if b>0 else -1.0),
            "len_description": float(len(str(it.get("description_details","")).strip())),
            "len_supplier_ref": float(len(str(it.get("supplier_reference","")).strip())),
            "len_customer_ref": float(len(str(it.get("customer_reference","")).strip())),
            "debit_only": 1.0 if _has(it.get("debit")) and not _has(it.get("credit")) else 0.0,
            "credit_only": 1.0 if _has(it.get("credit")) and not _has(it.get("debit")) else 0.0,
            "both_amounts": 1.0 if _has(it.get("debit")) and _has(it.get("credit")) else 0.0,
            "no_amounts": 1.0 if (not _has(it.get("debit")) and not _has(it.get("credit"))) else 0.0,
        }
        rows.append(row)
    return pd.DataFrame(rows)

def _robust_stats(X: np.ndarray):
    med = np.median(X, axis=0)
    q1 = np.quantile(X, 0.25, axis=0)
    q3 = np.quantile(X, 0.75, axis=0)
    iqr = q3 - q1
    iqr[iqr == 0] = 1e-9
    return med, iqr

def _explain_row(x: np.ndarray, med: np.ndarray, iqr: np.ndarray, top_k:int=6):
    rz = (x - med) / iqr
    idxs = np.argsort(np.abs(rz))[::-1][:top_k]
    out=[]
    for j in idxs:
        name = FEATURES[j] if j < len(FEATURES) else f"f{j}"
        out.append({"feature": name, "value": float(x[j]), "median": float(med[j]), "iqr": float(iqr[j]), "robust_z": float(rz[j])})
    return out

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

    df = _build_df(items)
    profiles = {}
    flagged = []
    keep_mask = [True]*len(items)

    for group, gdf in df.groupby("doctype_group"):
        X = gdf[FEATURES].to_numpy(dtype=float)
        if len(gdf) < 8:
            # tiny groups: score together with global
            group = "_global"
        profiles.setdefault(group, []).append(gdf.index.tolist())

    # build global index list per group
    idx_groups = {g: sorted({i for sub in idxs for i in sub}) for g, idxs in profiles.items()}

    for g, idxs in idx_groups.items():
        X = df.loc[idxs, FEATURES].to_numpy(dtype=float)
        scaler = RobustScaler()
        Xs = scaler.fit_transform(X)
        clf = IsolationForest(n_estimators=200, contamination="auto", random_state=42, n_jobs=-1)
        clf.fit(Xs)
        scores = -clf.score_samples(Xs)  # higher = more anomalous

        # threshold
        if threshold_method == "percentile":
            thr = float(np.quantile(scores, percentile))
        elif threshold_method == "zscore":
            mu, sd = float(np.mean(scores)), float(np.std(scores) or 1.0)
            thr = mu + zscore_z * sd
        else:
            q1, q3 = np.quantile(scores, [0.25, 0.75])
            iqr = q3 - q1
            thr = q3 + iqr_k * iqr

        med, iqr = _robust_stats(X)

        for row_idx, score, x in zip(idxs, scores, X):
            if score > thr:
                keep_mask[row_idx] = not remove
                flagged.append({
                    "index": (row_idx + 1) if one_based_index else row_idx,
                    "doc_type_group": df.loc[row_idx, "doctype_group"],
                    "score": float(score),
                    "reasons": ["ml-outlier"],
                    "top_features": _explain_row(x, med, iqr),
                })

    if remove:
        statement["statement_items"] = [it for i, it in enumerate(items) if keep_mask[i]]
    else:
        for i, it in enumerate(items):
            if not keep_mask[i]:  # flagged
                it.setdefault("_flags", []).append("ml-outlier")

    summary = {
        "total": len(items),
        "flagged": len(flagged),
        "flagged_items": flagged,
        "profiles": {g: {"count": len(idxs)} for g, idxs in idx_groups.items()},
    }
    return statement, summary
