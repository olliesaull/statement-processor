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
