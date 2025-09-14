from __future__ import annotations

from datetime import datetime, date
from typing import Any, Optional
import re


def _format_with_tokens(dt: datetime, template: str) -> str:
    """Render dt using a limited token set without strftime pitfalls.

    Supported tokens: YYYY, YY, MMMM, MMM, MM, M, DD, D
    """
    pattern = re.compile(r"YYYY|YY|MMMM|MMM|MM|M|DD|D|.", re.DOTALL)
    parts = []
    for tok in pattern.findall(template or ""):
        if tok == "YYYY":
            parts.append(f"{dt.year:04d}")
        elif tok == "YY":
            parts.append(f"{dt.year % 100:02d}")
        elif tok == "MMMM":
            parts.append(dt.strftime("%B"))
        elif tok == "MMM":
            parts.append(dt.strftime("%b"))
        elif tok == "MM":
            parts.append(f"{dt.month:02d}")
        elif tok == "M":
            parts.append(str(dt.month))
        elif tok == "DD":
            parts.append(f"{dt.day:02d}")
        elif tok == "D":
            parts.append(str(dt.day))
        else:
            parts.append(tok)
    return "".join(parts)


def format_iso_to_template(value: Any, template: Optional[str]) -> str:
    """
    Format an ISO date (YYYY-MM-DD) or datetime/date using a token template such as
    'DD/MM/YYYY' or 'D MMMM YYYY'. Returns empty string if input is empty/invalid.
    """
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        if not s:
            return ""
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return s  # leave as-is if not ISO

    if not template:
        return dt.strftime("%Y-%m-%d")
    return _format_with_tokens(dt, template)


def _template_to_strptime(template: Optional[str]) -> Optional[str]:
    """Convert our token template to a strptime-compatible pattern.

    Supports tokens: YYYY, YY, MMMM, MMM, MM, M, DD, D.
    Returns None if template is falsy.
    """
    if not template:
        return None
    # Order matters: replace longer tokens first
    replacements = [
        ("YYYY", "%Y"),
        ("YY", "%y"),
        ("MMMM", "%B"),
        ("MMM", "%b"),
        ("MM", "%m"),
        ("M", "%m"),
        ("DD", "%d"),
        ("D", "%d"),
    ]
    # Tokenize similarly to _format_with_tokens to preserve literals
    pattern = re.compile(r"YYYY|YY|MMMM|MMM|MM|M|DD|D|.", re.DOTALL)
    parts = []
    for tok in pattern.findall(template):
        for k, v in replacements:
            if tok == k:
                parts.append(v)
                break
        else:
            # literal; keep as-is for strptime to match
            parts.append(tok)
    return "".join(parts)


def _candidate_strptime_patterns(template: Optional[str]) -> list[str]:
    """Generate tolerant strptime patterns from a token template.

    Tries variations for month tokens (full/abbr/numeric) and year width.
    """
    base = _template_to_strptime(template)
    if not base:
        return []
    cands = {base}

    # Month variations
    if "%B" in base:
        cands.add(base.replace("%B", "%b"))
        cands.add(base.replace("%B", "%m"))
    if "%b" in base:
        cands.add(base.replace("%b", "%B"))
        cands.add(base.replace("%b", "%m"))
    if "%m" in base:
        cands.add(base.replace("%m", "%B"))
        cands.add(base.replace("%m", "%b"))

    # Year width variations
    more = set()
    for f in list(cands):
        if "%y" in f:
            more.add(f.replace("%y", "%Y"))
        if "%Y" in f:
            more.add(f.replace("%Y", "%y"))
    cands |= more

    return list(cands)


def parse_date_with_template(value: Any, template: Optional[str]) -> Optional[datetime]:
    """Parse a date using the token template, tolerant to MMM vs MMMM.

    Returns a datetime on success, or None if parsing fails.
    If value is already a datetime/date, normalizes to datetime.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    for fmt in _candidate_strptime_patterns(template):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def ensure_abbrev_month(template: Optional[str]) -> Optional[str]:
    """Return the same template but with abbreviated month (MMM) instead of full (MMMM)."""
    if not template:
        return template
    return template.replace("MMMM", "MMM")
