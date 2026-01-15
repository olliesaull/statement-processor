"""
Best-effort validation of statement items.

This module is used as a sanity-check step after we build statement JSON:
it compares text extracted from a statement by Textract (now JSON) with text extracted
via pdfplumber and tries to answer two questions:

1) JSON -> PDF: Do the reference values we extracted (e.g. invoice numbers) actually
   appear somewhere in the PDF text?
2) PDF -> JSON: Does the PDF contain additional "reference-like" tokens that match
   the same family/pattern, but which are missing from our extracted JSON?

To make comparisons resilient to formatting differences, we normalize strings by:
- uppercasing
- removing whitespace and common separators (`- _ / .`)

If a mismatch is detected, `validate_references_roundtrip` raises
`ItemCountDisagreementError` with a summary that callers can log.

Reference is what Xero call the 'Number' of an item (e.g. invoice number).
"""

import contextlib
import io
import re
from typing import Any

import pdfplumber

from config import logger
from exceptions import ItemCountDisagreementError

PdfInput = str | bytes | bytearray | io.BytesIO | Any


def _normalise(s: str) -> str:
    """
    Normalize a reference string for tolerant comparisons.

    This removes common formatting differences (spaces, hyphens, slashes, etc.)
    so values like "INV-123", "inv 123", and "INV/123" become comparable.
    """
    s = (s or "").upper().strip()
    return re.sub(r"[\s\-_/\.]", "", s)


def make_family_regex_from_examples(
    refs: list[str],
    digit_prefix_len: int = 3,
    min_samples_for_prefixing: int = 3,
    coverage_threshold: float = 0.6,
) -> re.Pattern:  # pylint: disable=too-many-locals,too-many-branches
    """
    Learn a regex that matches the "family" of reference tokens seen in JSON.

    We normalize and deduplicate the examples, then try to build a pattern that:
    - Captures a common alphabetic prefix + digits (e.g. "INV12345")
    - Optionally narrows by the first `digit_prefix_len` digits when we have enough samples (this reduces false positives when scanning the PDF)
    - Falls back to a simple "prefix + digit-length range" pattern when samples are sparse

    If no usable examples are provided, returns a regex that matches nothing.
    """
    ex_norm = sorted({_normalise(r) for r in refs if (r or "").strip()})
    if not ex_norm:
        return re.compile(r"$^")

    # Split references into (letters prefix, digits tail) families; keep non-matching refs as literals.
    families: dict[str, list[str]] = {}
    leftovers: list[str] = []
    for s in ex_norm:
        m = re.fullmatch(r"([A-Z]*)(\d+)", s)
        if not m:
            leftovers.append(s)
            continue
        prefix, digits = m.group(1), m.group(2)
        families.setdefault(prefix, []).append(digits)

    parts: list[str] = []

    for prefix, tails in families.items():
        # Digit-length range within this prefix family (e.g. INV + 5..7 digits).
        lens = [len(t) for t in tails]
        lo, hi = min(lens), max(lens)

        if len(tails) < min_samples_for_prefixing or lo == 0:
            if prefix:
                parts.append(rf"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else rf"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(rf"\d{{{lo}}}" if lo == hi else rf"\d{{{lo},{hi}}}")
            continue

        # If we have enough samples, bucket by the first N digits to reduce false positives.
        bucket_counts: dict[str, int] = {}
        for t in tails:
            k = t[: min(digit_prefix_len, len(t))]
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
                parts.append(rf"{re.escape(prefix)}\d{{{lo}}}" if lo == hi else rf"{re.escape(prefix)}\d{{{lo},{hi}}}")
            else:
                parts.append(rf"\d{{{lo}}}" if lo == hi else rf"\d{{{lo},{hi}}}")
            continue

        # Emit one pattern per kept digit-prefix bucket.
        for k in kept:
            lens_k = [len(t) for t in tails if t.startswith(k)]
            lo_k, hi_k = min(lens_k), max(lens_k)
            rem_lo = max(0, lo_k - len(k))
            rem_hi = max(0, hi_k - len(k))
            pat = rf"{re.escape(prefix)}{re.escape(k)}\d{{{rem_lo}}}" if rem_lo == rem_hi else rf"{re.escape(prefix)}{re.escape(k)}\d{{{rem_lo},{rem_hi}}}"
            parts.append(pat)

    if leftovers:
        parts.append(f"(?:{'|'.join(re.escape(s) for s in leftovers)})")

    return re.compile("(?:" + "|".join(parts) + ")")


def _to_pdf_open_arg(pdf_input: PdfInput) -> str | io.BytesIO | Any:
    """
    Convert various PDF input types into a value that `pdfplumber.open` accepts.

    Supports:
    - file paths (str)
    - bytes / bytearray (wrapped in BytesIO)
    - file-like objects (anything with `.read()`)
    """
    if isinstance(pdf_input, str):
        return pdf_input

    if isinstance(pdf_input, (bytes, bytearray)):
        return io.BytesIO(pdf_input)

    if hasattr(pdf_input, "read"):
        with contextlib.suppress(AttributeError, OSError, ValueError):
            pdf_input.seek(0)
        return pdf_input

    raise TypeError("Unsupported pdf_input type for pdfplumber.open")


def extract_normalized_pdf_text(pdf_input: PdfInput) -> str:
    """
    Extract full PDF text via `pdfplumber` and normalize it for comparisons.

    Note: scanned/image-only PDFs often return little or no text from `page.extract_text()`.
    """
    chunks: list[str] = []
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                chunks.append(t)
    return _normalise("\n".join(chunks))


def extract_pdf_candidates_with_pattern(pdf_input: PdfInput, pattern: re.Pattern, ngram_max: int = 5, hard_seps: str = ":.") -> set[str]:  # pylint: disable=too-many-locals
    """
    Scan PDF text for tokens that match a learned reference regex.

    We first extract page text, then find spans of `[A-Z0-9]+` tokens. To handle
    cases where a reference might be split by whitespace (e.g. "INV 123"), we
    consider n-grams of up to `ngram_max` consecutive tokens, take the original
    substring spanning those tokens, normalize it, and check it against `pattern`.

    `hard_seps` are characters that cause us to skip a span entirely (e.g. ":" or ".")
    to reduce false positives from things like timestamps, section labels, etc.
    """
    cands: set[str] = set()
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            upper = text.upper()

            # Build token spans so we can reconstruct contiguous substrings later.
            spans = []
            for m in re.finditer(r"[A-Z0-9]+", upper):
                spans.append((m.group(0), m.start(), m.end()))

            span_count = len(spans)
            up_to = min(ngram_max, span_count)
            for n in range(1, up_to + 1):
                for i in range(0, span_count - n + 1):
                    start = spans[i][1]
                    end = spans[i + n - 1][2]
                    original_segment = upper[start:end]

                    if any(h in original_segment for h in hard_seps):
                        continue

                    normalized = re.sub(r"[\s\-_/\.]", "", original_segment)

                    if pattern.fullmatch(normalized):
                        cands.add(normalized)
    return cands


def validate_references_roundtrip(pdf_input: PdfInput, statement_items: list[dict], ref_field: str = "reference") -> dict:  # pylint: disable=too-many-locals
    """
    Cross-check extracted reference values against the source PDF text.

    - JSON -> PDF: each non-empty `ref_field` value in `statement_items` should be findable in the normalized PDF text.
    - PDF -> JSON: we learn a reference-family regex from the JSON examples and scan the PDF for other matching candidates;
      any candidates not present in the JSON set are treated as suspicious/missing.

    If mismatches are detected, raises `ItemCountDisagreementError` with a summary. Returns a small summary dict on success.
    """
    has_text = False
    with pdfplumber.open(_to_pdf_open_arg(pdf_input)) as pdf:
        for page in pdf.pages:
            if page.extract_text():
                has_text = True
                break

    if not has_text:
        # Without extractable text, pdfplumber-based validation will generate false mismatches.
        logger.warning("PDF appears to be image-only (scanned). Skipping reference validation.")
        return {"checked": 0, "pdf_candidates": 0}

    logger.debug(
        "Reference validation start",
        total_items=len(statement_items),
        ref_field=ref_field,
    )

    # Collect non-empty references from the extracted JSON.
    raw_refs = [(i, (it.get(ref_field) or "").strip()) for i, it in enumerate(statement_items)]
    raw_refs = [(i, r) for i, r in raw_refs if r]
    json_norm_set = {_normalise(r) for _, r in raw_refs}
    logger.debug(
        "Collected JSON references",
        total_refs=len(raw_refs),
        unique_refs=len(json_norm_set),
    )

    # JSON -> PDF: check each extracted reference appears somewhere in the PDF text.
    norm_pdf_text = extract_normalized_pdf_text(pdf_input)
    logger.debug("Extracted normalized PDF text", chars=len(norm_pdf_text))
    found: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    for i, raw in raw_refs:
        norm_ref = _normalise(raw)
        (found if norm_ref in norm_pdf_text else not_found).append({"index": i, "reference": raw})

    # PDF -> JSON: learn a regex from the JSON examples and scan the PDF for other candidates.
    learned_rx = make_family_regex_from_examples([r for _, r in raw_refs])
    logger.debug("Learned reference family regex", pattern_len=len(learned_rx.pattern))
    pdf_candidates_norm = extract_pdf_candidates_with_pattern(pdf_input, learned_rx)
    pdf_only_norm = sorted(pdf_candidates_norm - json_norm_set)

    total_checked = len(found) + len(not_found)
    logger.info(
        "JSON -> PDF summary",
        total_checked=total_checked,
        found=len(found),
        not_found=len(not_found),
    )
    logger.info(
        "PDF -> JSON summary",
        pdf_candidates=len(pdf_candidates_norm),
        pdf_only=len(pdf_only_norm),
    )

    if not_found or pdf_only_norm:
        summary = {
            "json_refs_checked": total_checked,
            "json_refs_found": len(found),
            "json_refs_missing": len(not_found),
            "pdf_candidates": len(pdf_candidates_norm),
            "pdf_only_refs": pdf_only_norm,
        }
        raise ItemCountDisagreementError(len(found), len(json_norm_set), summary=summary)

    return {"checked": total_checked, "pdf_candidates": len(pdf_candidates_norm)}
