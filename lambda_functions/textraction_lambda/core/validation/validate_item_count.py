import io
import re
from typing import Any, Dict, List, Set, Union

import pdfplumber

from config import logger
from exceptions import ItemCountDisagreementError

PdfInput = Union[str, bytes, bytearray, io.BytesIO, Any]

def normalise(s: str) -> str:
    s = (s or "").upper().strip()
    return re.sub(r"[\s\-_/\.]", "", s)


def make_family_regex_from_examples(refs: List[str], digit_prefix_len: int = 3, min_samples_for_prefixing: int = 3, coverage_threshold: float = 0.6) -> re.Pattern:
    ex_norm = sorted({normalise(r) for r in refs if (r or "").strip()})
    if not ex_norm:
        return re.compile(r"$^")

    families: Dict[str, List[str]] = {}
    leftovers: List[str] = []
    for s in ex_norm:
        m = re.fullmatch(r"([A-Z]*)(\d+)", s)
        if not m:
            leftovers.append(s)
            continue
        prefix, digits = m.group(1), m.group(2)
        families.setdefault(prefix, []).append(digits)

    parts: List[str] = []

    for prefix, tails in families.items():
        lens = [len(t) for t in tails]
        lo, hi = min(lens), max(lens)

        if len(tails) < min_samples_for_prefixing or lo == 0:
            if prefix:
                parts.append(fr"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else fr"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(fr"\d{{{lo}}}" if lo == hi else fr"\d{{{lo},{hi}}}")
            continue

        bucket_counts: Dict[str, int] = {}
        for t in tails:
            k = t[:min(digit_prefix_len, len(t))]
            bucket_counts[k] = bucket_counts.get(k, 0) + 1

        total = len(tails)
        kept = [k for k, c in bucket_counts.items() if c / total >= (coverage_threshold / max(1, len(bucket_counts))) or c >= 2]
        if not kept:
            for k, _ in sorted(bucket_counts.items(), key=lambda kv: kv[1], reverse=True):
                kept.append(k)
                if sum(bucket_counts[x] for x in kept) / total >= coverage_threshold:
                    break

        covered = sum(bucket_counts.get(k, 0) for k in kept)
        if covered / total < coverage_threshold:
            if prefix:
                parts.append(fr"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else fr"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(fr"\d{{{lo}}}" if lo == hi else fr"\d{{{lo},{hi}}}")
            continue

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

    if leftovers:
        parts.append("(?:%s)" % "|".join(re.escape(s) for s in leftovers))

    return re.compile("(?:" + "|".join(parts) + ")")


def _to_pdf_open_arg(pdf_input: PdfInput) -> Union[str, io.BytesIO, Any]:
    if isinstance(pdf_input, str):
        return pdf_input

    if isinstance(pdf_input, (bytes, bytearray)):
        return io.BytesIO(pdf_input)

    if hasattr(pdf_input, "read"):
        try:
            pdf_input.seek(0)
        except Exception:
            pass
        return pdf_input

    raise TypeError("Unsupported pdf_input type for pdfplumber.open")


def extract_normalized_pdf_text(pdf_input: PdfInput) -> str:
    chunks: List[str] = []
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                chunks.append(t)
    return normalise("\n".join(chunks))


def extract_pdf_candidates_with_pattern(pdf_input: PdfInput, pattern: re.Pattern, ngram_max: int = 5, hard_seps: str = ":.") -> Set[str]:
    cands: Set[str] = set()
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or "")
            upper = text.upper()

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

                    if any(h in original_segment for h in hard_seps):
                        continue

                    normalized = re.sub(r"[\s\-_/\.]", "", original_segment)

                    if pattern.fullmatch(normalized):
                        cands.add(normalized)
    return cands


def validate_references_roundtrip(pdf_input: PdfInput, statement_items: List[Dict], ref_field: str = "reference") -> Dict:
    has_text = False
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                has_text = True
                break

    if not has_text:
        logger.warning("PDF appears to be image-only (scanned). Skipping validation.")

    raw_refs = [(i, (it.get(ref_field) or "").strip()) for i, it in enumerate(statement_items)]
    raw_refs = [(i, r) for i, r in raw_refs if r]
    json_norm_set = {normalise(r) for _, r in raw_refs}

    norm_pdf_text = extract_normalized_pdf_text(pdf_input)
    found, not_found = [], []
    for i, raw in raw_refs:
        norm_ref = normalise(raw)
        (found if norm_ref in norm_pdf_text else not_found).append({"index": i, "reference": raw})

    learned_rx = make_family_regex_from_examples([r for _, r in raw_refs])
    pdf_candidates_norm = extract_pdf_candidates_with_pattern(pdf_input, learned_rx)
    pdf_only_norm = sorted(pdf_candidates_norm - json_norm_set)

    total_checked = len(found) + len(not_found)
    logger.info("JSON -> PDF summary", total_checked=total_checked, found=len(found), not_found=len(not_found))
    logger.info("PDF -> JSON summary", pdf_candidates=len(pdf_candidates_norm), pdf_only=len(pdf_only_norm))

    if not_found or pdf_only_norm:
        summary = {
            "json_refs_checked": total_checked,
            "json_refs_found": len(found),
            "json_refs_missing": len(not_found),
            "pdf_candidates": len(pdf_candidates_norm),
            "pdf_only_refs": pdf_only_norm,
        }
        raise ItemCountDisagreementError(len(found), len(json_norm_set), summary=summary)

    return {
        "checked": total_checked,
        "pdf_candidates": len(pdf_candidates_norm),
    }
