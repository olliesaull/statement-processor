import json
import re
from typing import Dict, List, Set, Tuple

import pdfplumber


# -----------------------------
# Normalization
# -----------------------------
def normalise(s: str) -> str:
    """Uppercase and remove common separators so 'INV-001 23' == 'INV00123'."""
    s = (s or "").upper().strip()
    return re.sub(r"[\s\-_/\.]", "", s)

# -----------------------------
# Learn a generalized pattern from JSON refs
# -----------------------------
def _runs_by_type(s: str) -> List[Tuple[str, int]]:
    """Compress normalized string into runs of L(etter)/D(igit)/X(other)."""
    runs, prev, cnt = [], None, 0
    for ch in s:
        kind = "L" if ch.isalpha() else ("D" if ch.isdigit() else "X")
        if kind == prev:
            cnt += 1
        else:
            if prev is not None:
                runs.append((prev, cnt))
            prev, cnt = kind, 1
    if prev is not None:
        runs.append((prev, cnt))
    return runs

def make_family_regex_from_examples(refs: List[str], digit_prefix_len: int = 3, min_samples_for_prefixing: int = 3, coverage_threshold: float = 0.6) -> re.Pattern:
    """
    Learn a union of per-family patterns from normalized refs, but constrain by
    leading digits of the numeric tail to avoid overly broad \\d{m,n}.
    - digit_prefix_len: how many leading digits to anchor per cluster
    - min_samples_for_prefixing: need >= this many samples in a family to learn prefixes
    - coverage_threshold: if clustered prefixes cover >= this fraction, use them;
      otherwise fall back to un-prefixed family (e.g., PREFIX\\d{m,n}).
    """
    ex_norm = sorted({normalise(r) for r in refs if (r or "").strip()})
    if not ex_norm:
        return re.compile(r"$^")

    # Split into families: LETTERS + DIGITS
    families: Dict[str, List[str]] = {}  # prefix -> list of numeric tails
    leftovers: List[str] = []
    for s in ex_norm:
        m = re.fullmatch(r"([A-Z]*)(\d+)", s)
        if not m:
            leftovers.append(s)  # weird shapes, keep as strict alternation
            continue
        prefix, digits = m.group(1), m.group(2)
        families.setdefault(prefix, []).append(digits)

    parts: List[str] = []

    for prefix, tails in families.items():
        # length range for this family
        lens = [len(t) for t in tails]
        lo, hi = min(lens), max(lens)

        # If not enough samples or too short numeric tails, just do PREFIX\\d{lo,hi}
        if len(tails) < min_samples_for_prefixing or lo == 0:
            if prefix:
                parts.append(fr"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else fr"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(fr"\d{{{lo}}}" if lo == hi else fr"\d{{{lo},{hi}}}")
            continue

        # Build clusters by leading digit_prefix_len digits (or up to tail length)
        bucket_counts: Dict[str, int] = {}
        for t in tails:
            k = t[:min(digit_prefix_len, len(t))]
            bucket_counts[k] = bucket_counts.get(k, 0) + 1

        # Keep buckets that cover enough of the family
        total = len(tails)
        kept = [k for k, c in bucket_counts.items() if c / total >= (coverage_threshold / max(1, len(bucket_counts))) or c >= 2]
        # If nothing qualifies, pick top buckets by count until we cover threshold
        if not kept:
            for k, _ in sorted(bucket_counts.items(), key=lambda kv: kv[1], reverse=True):
                kept.append(k)
                if sum(bucket_counts[x] for x in kept) / total >= coverage_threshold:
                    break

        # If even after this the coverage is tiny (e.g., all unique), fall back
        covered = sum(bucket_counts.get(k, 0) for k in kept)
        if covered / total < coverage_threshold:
            if prefix:
                parts.append(fr"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else fr"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(fr"\d{{{lo}}}" if lo == hi else fr"\d{{{lo},{hi}}}")
            continue

        # Build alternation per kept bucket, using per-length ranges for that bucket
        for k in kept:
            lens_k = [len(t) for t in tails if t.startswith(k)]
            lo_k, hi_k = min(lens_k), max(lens_k)
            rem_lo = max(0, lo_k - len(k))
            rem_hi = max(0, hi_k - len(k))
            if rem_lo == rem_hi:
                pat = fr"{re.escape(prefix)}{re.escape(k)}\d{{{rem_lo}}}"
            else:
                pat = fr"{re.escape(prefix)}{re.escape(k)}\d{{{rem_lo},{rem_hi}}}"
            parts.append(pat)

    # Strict alternation for leftovers
    if leftovers:
        parts.append("(?:%s)" % "|".join(re.escape(s) for s in leftovers))

    return re.compile("(?:" + "|".join(parts) + ")")


# -----------------------------
# PDF helpers
# -----------------------------
def extract_normalized_pdf_text(pdf_path: str) -> str:
    """Concatenate all page text and normalize once for fast substring lookups."""
    chunks: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                chunks.append(t)
    return normalise("\n".join(chunks))

def extract_pdf_candidates_with_pattern(pdf_path: str, pattern: re.Pattern, ngram_max: int = 5, hard_seps: str = ":.") -> Set[str]:
    """
    Slide windows over alnum tokens, but DON'T join across hard separators like ':'.
    We rebuild the original substring from the first token start to last token end
    to inspect separators, then normalize only joinable seps before matching.
    """
    cands: Set[str] = set()
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or "")
            upper = text.upper()

            # Build (token_text, start, end) spans
            spans = []
            for m in re.finditer(r"[A-Z0-9]+", upper):
                spans.append((m.group(0), m.start(), m.end()))

            L = len(spans)
            up_to = min(ngram_max, L)
            for n in range(1, up_to + 1):
                for i in range(0, L - n + 1):
                    start = spans[i][1]
                    end = spans[i + n - 1][2]
                    original_segment = upper[start:end]

                    # Skip if any hard separator is present in the original segment
                    if any(h in original_segment for h in hard_seps):
                        continue

                    # Now normalize only the joinable separators (spaces, -, _, /, .)
                    normalized = re.sub(r"[\s\-_/\.]", "", original_segment)

                    if pattern.fullmatch(normalized):
                        cands.add(normalized)
    return cands

# -----------------------------
# Main validation (both directions)
# -----------------------------
def validate_references_roundtrip(pdf_path: str, statement_items: List[Dict], ref_field: str = "reference") -> Dict:
    """
    1) JSON -> PDF: every JSON ref should be present in PDF (after normalization).
    2) PDF -> JSON: learn pattern from JSON refs, find all matches in PDF, and ensure every PDF match exists in JSON.
    3) If the PDF has no extractable text (likely scanned), skip validation and flag it.
    """
    # Quick text layer check
    has_text = False
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                has_text = True
                break

    if not has_text:
        print(f"[WARNING] {pdf_path} appears to be image-only (scanned). Skipping validation.")

    # Collect JSON refs
    raw_refs = [(i, (it.get(ref_field) or "").strip()) for i, it in enumerate(statement_items)]
    raw_refs = [(i, r) for i, r in raw_refs if r]
    json_norm_set = {normalise(r) for _, r in raw_refs}

    # ---- Pass 1: JSON -> PDF
    norm_pdf_text = extract_normalized_pdf_text(pdf_path)
    found, not_found = [], []
    for i, raw in raw_refs:
        norm_ref = normalise(raw)
        (found if norm_ref in norm_pdf_text else not_found).append({"index": i, "reference": raw})

    # ---- Pass 2: PDF -> JSON
    learned_rx = make_family_regex_from_examples([r for _, r in raw_refs])
    pdf_candidates_norm = extract_pdf_candidates_with_pattern(pdf_path, learned_rx)
    pdf_only_norm = sorted(pdf_candidates_norm - json_norm_set)  # present in PDF but missing in JSON

    # Print summary
    total_checked = len(found) + len(not_found)
    print(f"JSON -> PDF | Total checked: {total_checked} | Found: {len(found)} | Not found: {len(not_found)}")
    print(f"PDF -> JSON | PDF candidates: {len(pdf_candidates_norm)} | PDF-only (missing in JSON): {len(pdf_only_norm)}")

    summary = {
        "summary": {
            "json_to_pdf_total_checked": total_checked,
            "json_to_pdf_found": len(found),
            "json_to_pdf_not_found": len(not_found),
            "pdf_to_json_candidates": len(pdf_candidates_norm),
            "pdf_to_json_pdf_only": len(pdf_only_norm),
        },
        "json_to_pdf": {
            "found": found,
            "not_found": not_found,
        },
        "pdf_to_json": {
            "pdf_candidates_normalized": sorted(pdf_candidates_norm),
            "pdf_only_normalized": pdf_only_norm,
        },
    }

    if len(not_found) > 0 or len(pdf_only_norm) > 0:
        print(json.dumps(summary, indent=2))

    return summary
