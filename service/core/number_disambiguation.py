"""Post-processing to disambiguate decimal vs thousands separators.

The LLM proposes separator characters from sample values. This module
scans actual monetary values from the total column(s) to confirm or
correct the proposal. Monetary values have at most 2 decimal places,
so the digit count after the last separator reliably distinguishes
the two roles.
"""

import re

from logger import logger

# Matches currency symbols, whitespace, and parenthetical negatives
# so we can strip them before analysing separator characters.
_STRIP_RE = re.compile(r"[^\d.,'\s-]")
# Characters that can act as thousands/decimal separators in practice.
_SEPARATOR_CHARS = {".", ",", "'", " "}


def extract_monetary_values(headers: list[str], rows: list[list[str]], total_columns: list[str], exclude_columns: list[str] | None = None) -> list[str]:
    """Collect raw monetary cell values from the total column(s).

    First tries to match ``total_columns`` against ``headers`` by name.
    If no columns match (e.g. Textract picked up a title row instead of
    real headers), falls back to scanning all cells for monetary-looking
    values — skipping columns that match ``exclude_columns`` (typically
    date columns whose separators would pollute the analysis).

    Args:
        headers: Column headers from the statement table.
        rows: Data rows (list of cell values per row).
        total_columns: Header name(s) the LLM identified as totals.
        exclude_columns: Column names to skip in the fallback scan
            (e.g. date, due_date columns).

    Returns:
        List of raw monetary value strings.
    """
    # Build a case-insensitive lookup for header positions.
    header_lower = [h.lower().strip() for h in headers]
    indices: list[int] = []
    for col_name in total_columns:
        needle = col_name.lower().strip()
        for idx, h in enumerate(header_lower):
            if h == needle:
                indices.append(idx)
                break

    values: list[str] = []
    for row in rows:
        for idx in indices:
            if idx < len(row) and row[idx].strip():
                values.append(row[idx].strip())

    if values:
        return values

    # Fallback: column names didn't match headers. Scan all cells for
    # monetary-looking values, skipping excluded columns (dates).
    exclude_indices: set[int] = set()
    if exclude_columns:
        for col_name in exclude_columns:
            if not col_name:
                continue
            needle = col_name.lower().strip()
            for idx, h in enumerate(header_lower):
                if h == needle:
                    exclude_indices.add(idx)
                    break

    for row in rows:
        for idx, cell in enumerate(row):
            if idx in exclude_indices:
                continue
            stripped = cell.strip()
            if stripped and _looks_monetary(stripped):
                values.append(stripped)

    return values


# Date-like pattern: digits separated by "/", "-", or "."
# (e.g. "03/07/2023", "2023-07-03", "03.07.2023").
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{1,4}$")


def _looks_monetary(value: str) -> bool:
    """Check if a cell value looks like a monetary amount.

    A monetary value contains digits and at least one separator character
    (period, comma, apostrophe, or space between digits). Plain integers
    and date-like strings are excluded.

    Args:
        value: Stripped cell value.

    Returns:
        True if the value looks monetary.
    """
    # Must contain at least one digit.
    if not any(c.isdigit() for c in value):
        return False

    # Exclude date-like patterns (e.g. 03/07/2023, 2023-07-03).
    if _DATE_LIKE_RE.match(value):
        return False

    # Must contain at least one separator character among digits.
    has_separator = any(c in _SEPARATOR_CHARS for c in value)
    return has_separator


def disambiguate_number_separators(monetary_values: list[str], llm_decimal: str, llm_thousands: str) -> tuple[str, str]:
    """Confirm or correct LLM-suggested number separators from actual values.

    Analyses monetary values to determine which character is the decimal
    separator and which is the thousands separator. Monetary amounts
    have at most 2 decimal places, so:

    - Separator followed by 1-2 digits at end → decimal
    - Separator followed by 3 digits at end → thousands
    - Separator appearing multiple times → thousands
    - Two different separators → last one is decimal

    Args:
        monetary_values: Raw monetary strings from total column(s).
        llm_decimal: LLM-proposed decimal separator.
        llm_thousands: LLM-proposed thousands separator.

    Returns:
        Tuple of (decimal_separator, thousands_separator).
    """
    if not monetary_values:
        return llm_decimal, llm_thousands

    # Track evidence: character → set of roles observed.
    # Roles: "decimal" or "thousands".
    evidence: dict[str, set[str]] = {}

    for raw in monetary_values:
        _analyse_value(raw, evidence)

    if not evidence:
        # No separators found in any value — keep LLM suggestion.
        return llm_decimal, llm_thousands

    return _resolve(evidence, llm_decimal, llm_thousands)


def _clean_value(raw: str) -> str:
    """Strip currency symbols and outer whitespace, keep digits and separators.

    Handles parenthetical negatives like ``(1,234.56)`` by removing
    parentheses. Keeps ``-`` only at the start.
    """
    cleaned = raw.strip()
    # Remove parenthetical negative notation.
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    # Remove currency symbols and letters (e.g. EUR, $, £).
    cleaned = _STRIP_RE.sub("", cleaned)
    # Remove leading/trailing whitespace left behind.
    return cleaned.strip()


def _analyse_value(raw: str, evidence: dict[str, set[str]]) -> None:
    """Analyse a single monetary value and record separator evidence.

    Args:
        raw: Raw monetary string.
        evidence: Mutable dict accumulating role evidence per character.
    """
    cleaned = _clean_value(raw)
    if not cleaned:
        return

    # Remove leading/trailing sign characters so they don't interfere.
    if cleaned.startswith("-"):
        cleaned = cleaned[1:]
    if cleaned.endswith(("-", "+")):
        cleaned = cleaned[:-1]

    # Find all separator characters present in the value.
    separators_found: list[str] = []
    for ch in cleaned:
        if ch in _SEPARATOR_CHARS and ch not in separators_found:
            separators_found.append(ch)

    if not separators_found:
        return

    for sep in separators_found:
        # Count occurrences of this separator.
        count = cleaned.count(sep)

        # Rule: separator appearing multiple times → thousands.
        if count > 1:
            evidence.setdefault(sep, set()).add("thousands")
            continue

        # Single occurrence — check digit count after the separator.
        last_idx = cleaned.rfind(sep)
        digits_after = cleaned[last_idx + 1 :]

        # Verify what follows is all digits (no other separators after).
        if not digits_after.isdigit():
            # Another separator follows — this one is thousands.
            evidence.setdefault(sep, set()).add("thousands")
            continue

        if len(digits_after) <= 2:
            # 1-2 digits after → decimal separator.
            evidence.setdefault(sep, set()).add("decimal")
        else:
            # 3+ digits after → thousands separator.
            evidence.setdefault(sep, set()).add("thousands")

    # Rule: two different separators in one value → last one is decimal.
    if len(separators_found) >= 2:
        # Reinforce: last separator is decimal, others are thousands.
        last_sep_idx = -1
        last_sep_char = ""
        for sep in separators_found:
            idx = cleaned.rfind(sep)
            if idx > last_sep_idx:
                last_sep_idx = idx
                last_sep_char = sep
        evidence.setdefault(last_sep_char, set()).add("decimal")
        for sep in separators_found:
            if sep != last_sep_char:
                evidence.setdefault(sep, set()).add("thousands")


def _resolve(evidence: dict[str, set[str]], llm_decimal: str, llm_thousands: str) -> tuple[str, str]:
    """Resolve evidence into a final (decimal, thousands) pair.

    Args:
        evidence: Character → set of observed roles.
        llm_decimal: LLM-proposed decimal separator.
        llm_thousands: LLM-proposed thousands separator.

    Returns:
        Tuple of (decimal_separator, thousands_separator).
    """
    # Characters with unambiguous evidence.
    definite_decimal: str | None = None
    definite_thousands: str | None = None

    for char, roles in evidence.items():
        if roles == {"decimal"}:
            definite_decimal = char
        elif roles == {"thousands"}:
            definite_thousands = char

    # If we have clear evidence for both, use it.
    if definite_decimal and definite_thousands:
        logger.info("Number separators disambiguated", decimal=definite_decimal, thousands=definite_thousands, llm_decimal=llm_decimal, llm_thousands=llm_thousands)
        return definite_decimal, definite_thousands

    # If we only have evidence for one, infer the other.
    if definite_decimal and not definite_thousands:
        # Keep LLM's thousands unless it conflicts with our decimal.
        resolved_thousands = llm_thousands if llm_thousands != definite_decimal else llm_decimal
        logger.info("Number separators partially disambiguated (decimal only)", decimal=definite_decimal, thousands=resolved_thousands)
        return definite_decimal, resolved_thousands

    if definite_thousands and not definite_decimal:
        # Keep LLM's decimal unless it conflicts with our thousands.
        resolved_decimal = llm_decimal if llm_decimal != definite_thousands else llm_thousands
        logger.info("Number separators partially disambiguated (thousands only)", decimal=resolved_decimal, thousands=definite_thousands)
        return resolved_decimal, definite_thousands

    # Ambiguous evidence — keep LLM suggestion.
    logger.info("Number separator evidence ambiguous, keeping LLM suggestion", evidence={k: list(v) for k, v in evidence.items()})
    return llm_decimal, llm_thousands
