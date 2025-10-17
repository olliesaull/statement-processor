from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional


def fmt_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if value is None:
        return None
    candidate = getattr(value, "isoformat", None)
    if callable(candidate):
        try:
            result = candidate()
            return result if isinstance(result, str) else None
        except Exception:
            return None
    candidate = getattr(value, "strftime", None)
    if callable(candidate):
        try:
            return candidate("%Y-%m-%d")
        except Exception:
            return None
    return None


def parse_updated_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

