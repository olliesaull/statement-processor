"""Module for comparing the pdf text extraction between pdfplumber and Amazon Textract"""

import difflib
import re
from itertools import zip_longest
from typing import List, Tuple

from configuration.config import S3_BUCKET_NAME
from core.extract_text_from_pdf import (
    count_pdf_pages,
    extract_text_from_pdf_bytes,
    extract_text_from_textract_s3,
)
from utils.aws import get_s3_object_bytes, get_statements_from_s3

WORD_RE = re.compile(r"\w+", flags=re.UNICODE)

def normalize_words(text: str) -> str:
    """
    Keep only word tokens (letters/digits/_), lowercased, joined by single spaces.
    This ignores punctuation, newlines, and spacing differences.
    """
    tokens = WORD_RE.findall(text.lower())
    return " ".join(tokens)

def _split_keep_original(text: str) -> Tuple[List[str], List[str]]:
    """
    Return (orig_lines, norm_lines) where norm_lines are word-normalized versions
    of orig_lines. Empty lines in orig are kept (as empty strings) so indexes align.
    """
    orig_lines = text.splitlines()
    norm_lines = [normalize_words(line) for line in orig_lines]
    return orig_lines, norm_lines

def readable_line_diff(
    a_raw: str,
    b_raw: str,
    label_a: str = "pdfplumber",
    label_b: str = "textract",
    max_mismatches: int = 50,
) -> str:
    """
    Produce a readable, paired diff using word-only comparison.
    Only lines that differ (after normalization) are shown, in aligned pairs.
    """
    a_lines, a_norm = _split_keep_original(a_raw)
    b_lines, b_norm = _split_keep_original(b_raw)

    sm = difflib.SequenceMatcher(None, a_norm, b_norm, autojunk=False)

    out: List[str] = []
    mismatches = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        if tag in ("replace", "delete", "insert"):
            # Pair up the differing slices; zip_longest to handle uneven lengths
            for i, j in zip_longest(range(i1, i2), range(j1, j2), fillvalue=None):
                if mismatches >= max_mismatches:
                    out.append("... (diff truncated)")
                    return "\n".join(out)

                a_line = a_lines[i] if i is not None else ""
                b_line = b_lines[j] if j is not None else ""

                # Skip pairs that are equal after normalization (can happen with empty lines)
                if normalize_words(a_line) == normalize_words(b_line):
                    continue

                out.append(f"{label_a:<10}: {a_line}")
                out.append(f"{label_b:<10}: {b_line}")
                out.append("")  # blank separator
                mismatches += 1

    if not out:
        return "(no differences after word-only normalization)"
    return "\n".join(out)

def compare_statement_texts_s3(bucket: str, prefix: str = "statements/") -> None:
    keys = get_statements_from_s3(bucket, prefix)

    total = 0
    skipped_for_scan = 0
    compared = 0
    matches = 0
    mismatches = 0
    errors = 0

    for key in sorted(keys):
        total += 1
        print(f"\n=== s3://{bucket}/{key} ===")

        try:
            obj_bytes = get_s3_object_bytes(bucket, key)

            # 1) pdfplumber
            pdf_text_raw = extract_text_from_pdf_bytes(obj_bytes)
            pdf_text_norm = normalize_words(pdf_text_raw or "")

            if not pdf_text_norm:
                skipped_for_scan += 1
                print("pdfplumber: (empty) — treating as scanned/empty, skipping Textract.")
                continue

            print(f"pdfplumber: {len(pdf_text_raw):,} chars (normalized: {len(pdf_text_norm):,})")

            print("*"*88)
            print(pdf_text_norm)
            print("*"*88)

            # 2) Textract:
            page_count = count_pdf_pages(obj_bytes)  # None if not a readable PDF (e.g., image upload)
            textract_raw = extract_text_from_textract_s3(bucket, key, page_count)
            textract_norm = normalize_words(textract_raw or "")
            print(f"textract  : {len(textract_raw):,} chars (normalized: {len(textract_norm):,})")

            print("*"*88)
            print(textract_norm)
            print("*"*88)

            compared += 1

            if pdf_text_norm == textract_norm:
                matches += 1
                print("✅ MATCH (after normalization)")
            else:
                mismatches += 1
                print("❌ MISMATCH (after normalization) — showing small diff:")
                print(readable_line_diff(pdf_text_raw, textract_raw, label_a="pdfplumber", label_b="textract", max_mismatches=80))

        except Exception as e:
            errors += 1
            print(f"⚠️ ERROR processing s3://{bucket}/{key}: {e}")

    print("\n===== Summary =====")
    print(f"Total PDFs found        : {total}")
    print(f"Skipped (scanned/empty) : {skipped_for_scan}")
    print(f"Compared (both ran)     : {compared}")
    print(f"Matches                 : {matches}")
    print(f"Mismatches              : {mismatches}")
    print(f"Errors                  : {errors}")

if __name__ == "__main__":
    compare_statement_texts_s3(S3_BUCKET_NAME, "statements/")
