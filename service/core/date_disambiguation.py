"""Post-processing to disambiguate DD/MM vs MM/DD date formats.

The LLM proposes a date format from sample values. This module scans all
date values to confirm or reject that proposal. If any value has a
component > 12 in the position that would be the day, it disambiguates
the entire document. If all values are ambiguous, returns empty string
so the user is prompted.
"""

import re

from logger import logger

# Matches date strings where the first two numeric components could be day/month.
# E.g. "15/03/2025", "03-15-2025", "07.08.25"
_NUMERIC_DATE_RE = re.compile(r"^(\d{1,2})\s*[/\-\.]\s*(\d{1,2})\s*[/\-\.]\s*(\d{2,4})$")


def disambiguate_date_format(date_values: list[str], llm_suggested_format: str) -> str:
    """Confirm or reject an LLM-suggested date format by scanning actual values.

    Scans ``date_values`` for numeric date strings. If any value has a
    component > 12 in first or second position, that disambiguates
    DD/MM vs MM/DD for the whole document.

    Args:
        date_values: Raw date strings extracted from the statement.
        llm_suggested_format: The SDF format string proposed by the LLM.

    Returns:
        The confirmed format string, or empty string if ambiguous.
    """
    if not date_values:
        return ""

    # If the format uses month names (MMM/MMMM), there's no DD/MM ambiguity.
    if "MMM" in llm_suggested_format:
        return llm_suggested_format

    first_positions: list[int] = []
    second_positions: list[int] = []

    for raw in date_values:
        match = _NUMERIC_DATE_RE.match(raw.strip())
        if not match:
            continue
        first_positions.append(int(match.group(1)))
        second_positions.append(int(match.group(2)))

    if not first_positions:
        # No parseable numeric dates found.
        return ""

    first_has_gt12 = any(v > 12 for v in first_positions)
    second_has_gt12 = any(v > 12 for v in second_positions)

    if first_has_gt12 and not second_has_gt12:
        # First position must be day (DD/MM). Correct the LLM format if it
        # suggested MM/DD by swapping the day/month tokens.
        corrected = _ensure_dd_mm(llm_suggested_format)
        logger.info("Date format disambiguated as DD/MM", sample_count=len(first_positions), max_first=max(first_positions), corrected_format=corrected)
        return corrected

    if second_has_gt12 and not first_has_gt12:
        # Second position must be day (MM/DD). Correct the LLM format if it
        # suggested DD/MM by swapping the day/month tokens.
        corrected = _ensure_mm_dd(llm_suggested_format)
        logger.info("Date format disambiguated as MM/DD", sample_count=len(second_positions), max_second=max(second_positions), corrected_format=corrected)
        return corrected

    # Both <= 12 everywhere: genuinely ambiguous.
    logger.info("Date format is ambiguous — all values have day and month <= 12", sample_count=len(first_positions))
    return ""


def _ensure_dd_mm(fmt: str) -> str:
    """If format has MM before DD, swap them so day comes first."""
    # Match leading M-token followed by separator then D-token.
    pattern = re.compile(r"^(M{1,2})([\s/\-\.]+)(D{1,2}|Do)")
    match = pattern.match(fmt)
    if match:
        return fmt[: match.start()] + match.group(3) + match.group(2) + match.group(1) + fmt[match.end() :]
    return fmt


def _ensure_mm_dd(fmt: str) -> str:
    """If format has DD before MM, swap them so month comes first."""
    pattern = re.compile(r"^(D{1,2}|Do)([\s/\-\.]+)(M{1,2})")
    match = pattern.match(fmt)
    if match:
        return fmt[: match.start()] + match.group(3) + match.group(2) + match.group(1) + fmt[match.end() :]
    return fmt
