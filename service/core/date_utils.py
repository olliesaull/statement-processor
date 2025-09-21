from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Iterable, List, Optional, Sequence, Tuple


TOKEN_ORDER: Sequence[str] = (
    "YYYY",
    "MMMM",
    "MMM",
    "MM",
    "DD",
    "YY",
    "Do",
    "dddd",
    "M",
    "D",
)

TOKEN_REGEX = {
    "YYYY": r"(?P<{name}>\d{{4}})",
    "YY": r"(?P<{name}>\d{{2}})",
    "MMMM": r"(?P<{name}>[A-Za-z]+)",
    "MMM": r"(?P<{name}>[A-Za-z]{{3,}})",
    "MM": r"(?P<{name}>\d{{2}})",
    "M": r"(?P<{name}>\d{{1,2}})",
    "DD": r"(?P<{name}>\d{{2}})",
    "D": r"(?P<{name}>\d{{1,2}})",
    "Do": r"(?P<{name}>\d{{1,2}}(?:st|nd|rd|th))",
    "dddd": r"(?P<{name}>[A-Za-z]+)",
}

MONTH_NAME_TO_NUM = {
    name.lower(): idx
    for idx, name in enumerate(calendar.month_name)
    if name
}
MONTH_ABBR_TO_NUM = {
    abbr.lower(): idx
    for idx, abbr in enumerate(calendar.month_abbr)
    if abbr
}
MONTH_NAME_TO_NUM["sept"] = 9  # common alternative abbreviation


def ensure_abbrev_month(fmt: Optional[str]) -> Optional[str]:
    """Prefer abbreviated month token (MMMM -> MMM) when displaying."""
    if not fmt:
        return fmt
    normalized = _normalize_template(fmt)
    return normalized.replace("MMMM", "MMM")


def parse_with_format(value: Any, template: Optional[str]) -> Optional[datetime]:
    """Parse ``value`` using the custom Supplier Date Format tokens."""
    if value is None:
        return None
    if not template:
        return None
    s = str(value).strip()
    if not s:
        return None

    compiled = _prepare_template(template)
    regex, group_order, tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal = compiled
    match = regex.match(s)
    if not match:
        return None

    components: dict[str, int] = {}
    for group_name, token in group_order:
        raw = match.group(group_name)
        if not raw:
            continue
        if token == "YYYY":
            _set_component(components, "year", int(raw))
        elif token == "YY":
            _set_component(components, "year", 2000 + int(raw))
        elif token in {"MMMM", "MMM"}:
            month = _month_from_name(raw)
            if month is None:
                raise ValueError(f"Unknown month name '{raw}' for format '{template}'")
            _set_component(components, "month", month)
        elif token in {"MM", "M"}:
            _set_component(components, "month", int(raw))
        elif token in {"DD", "D"}:
            _set_component(components, "day", int(raw))
        elif token == "Do":
            _set_component(components, "day", _parse_ordinal(raw))
        elif token == "dddd":
            continue

    if {"year", "month", "day"} - components.keys():
        return None

    year = components["year"]
    month = components["month"]
    day = components["day"]

    if not has_textual_month and numeric_month and numeric_day and not uses_ordinal:
        if 1 <= day <= 12 and 1 <= month <= 12 and day != month:
            raise ValueError(
                f"Ambiguous date '{s}' for format '{template}'. "
                "Please choose an unambiguous format such as DD/MM/YYYY or MM/DD/YYYY."
            )

    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def format_iso_with(value: Any, template: Optional[str]) -> str:
    """Format a stored ISO date using the Supplier Date Format tokens."""
    if value is None:
        return ""
    if not template:
        return _coerce_to_iso_string(value) or ""

    dt = _coerce_to_datetime(value)
    if dt is None:
        return str(value)

    compiled = _prepare_template(template)
    _, _, tokens, *_ = compiled
    return _format_tokens(tokens, dt)


def coerce_datetime_with_template(value: Any, template: Optional[str]) -> Optional[datetime]:
    """Return a ``datetime`` parsed from ``value`` using ``template`` or fallback heuristics.

    The statement upload may already have normalized dates to ISO. In that case the
    configured format will not match, so we fall back to the generic coercion used by
    ``format_iso_with`` to ensure we can still reformat values for display.
    """

    parsed: Optional[datetime] = None
    if template:
        try:
            parsed = parse_with_format(value, template)
        except ValueError:
            parsed = None

    if parsed is not None:
        return parsed

    return _coerce_to_datetime(value)


def _set_component(components: dict[str, int], key: str, value: int) -> None:
    if key in components and components[key] != value:
        raise ValueError(f"Conflicting values for {key}: {components[key]} vs {value}")
    components[key] = value


def _parse_ordinal(value: str) -> int:
    match = re.match(r"(\d{1,2})(st|nd|rd|th)$", value, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid ordinal day '{value}'")
    return int(match.group(1))


def _month_from_name(value: str) -> Optional[int]:
    txt = value.strip().lower()
    if txt in MONTH_NAME_TO_NUM:
        return MONTH_NAME_TO_NUM[txt]
    if txt in MONTH_ABBR_TO_NUM:
        return MONTH_ABBR_TO_NUM[txt]
    prefix = txt[:3]
    if prefix in MONTH_ABBR_TO_NUM:
        return MONTH_ABBR_TO_NUM[prefix]
    return None


def _coerce_to_iso_string(value: Any) -> Optional[str]:
    dt = _coerce_to_datetime(value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d")


def _coerce_to_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    s = str(value).strip()
    if not s:
        return None
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return datetime.strptime(s[:10], "%Y-%m-%d")
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        try:
            return datetime.fromisoformat(s)
        except Exception:
            return None


def _format_tokens(tokens: Sequence, dt: datetime) -> str:
    parts: List[str] = []
    for kind, value in tokens:
        if kind == "literal":
            parts.append(value)
        elif kind == "token":
            parts.append(_format_token(value, dt))
        elif kind == "optional":
            rendered = _format_tokens(value, dt)
            if rendered:
                parts.append(rendered)
    return "".join(parts)


def _format_token(token: str, dt: datetime) -> str:
    if token == "YYYY":
        return f"{dt.year:04d}"
    if token == "YY":
        return f"{dt.year % 100:02d}"
    if token == "MMMM":
        return calendar.month_name[dt.month]
    if token == "MMM":
        return calendar.month_abbr[dt.month]
    if token == "MM":
        return f"{dt.month:02d}"
    if token == "M":
        return str(dt.month)
    if token == "DD":
        return f"{dt.day:02d}"
    if token == "D":
        return str(dt.day)
    if token == "Do":
        return _ordinal(dt.day)
    if token == "dddd":
        return dt.strftime("%A")
    return token


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


@lru_cache(maxsize=128)
def _prepare_template(template: str):
    normalized = _normalize_template(template)
    tokens = _tokenize(normalized)
    group_order: List[Tuple[str, str]] = []
    pattern = _tokens_to_regex(tokens, normalized, group_order, Counter())
    regex = re.compile(f"^{pattern}$", re.IGNORECASE)
    flat_tokens = list(_iter_tokens(tokens))
    has_textual_month = any(t in {"MMM", "MMMM"} for t in flat_tokens)
    numeric_month = any(t in {"M", "MM"} for t in flat_tokens)
    numeric_day = any(t in {"D", "DD"} for t in flat_tokens)
    uses_ordinal = any(t == "Do" for t in flat_tokens)
    return regex, tuple(group_order), tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal


def _tokens_to_regex(
    tokens: Sequence,
    template: str,
    group_order: List[Tuple[str, str]],
    counts: Counter,
) -> str:
    parts: List[str] = []
    for kind, value in tokens:
        if kind == "literal":
            if value == " ":
                parts.append(r"\s+")
            else:
                parts.append(re.escape(value))
        elif kind == "token":
            if value not in TOKEN_REGEX:
                parts.append(re.escape(value))
            else:
                counts[value] += 1
                group_name = f"{value}_{counts[value]}"
                parts.append(TOKEN_REGEX[value].format(name=group_name))
                group_order.append((group_name, value))
        elif kind == "optional":
            inner = _tokens_to_regex(value, template, group_order, counts)
            parts.append(f"(?:{inner})?")
    return "".join(parts)


def _iter_tokens(tokens: Sequence) -> Iterable[str]:
    for kind, value in tokens:
        if kind == "token" and value in TOKEN_REGEX:
            yield value
        elif kind == "optional":
            yield from _iter_tokens(value)


def _tokenize(template: str) -> Tuple:
    tokens: List[Tuple[str, Any]] = []
    i = 0
    length = len(template)
    while i < length:
        if template[i] == "[":
            end = template.find("]", i)
            if end == -1:
                raise ValueError(f"Unbalanced brackets in format '{template}'")
            inner = template[i + 1 : end]
            tokens.append(("optional", _tokenize(inner)))
            i = end + 1
            continue
        matched = False
        for token in TOKEN_ORDER:
            if template.startswith(token, i):
                tokens.append(("token", token))
                i += len(token)
                matched = True
                break
        if matched:
            continue
        tokens.append(("literal", template[i]))
        i += 1
    return tuple(tokens)


def _normalize_template(template: str) -> str:
    replacements = [
        ("%-d", "D"),
        ("%#d", "D"),
        ("%_d", "D"),
        ("%d", "DD"),
        ("%-e", "D"),
        ("%#e", "D"),
        ("%e", "D"),
        ("%-m", "M"),
        ("%#m", "M"),
        ("%_m", "M"),
        ("%m", "MM"),
        ("%B", "MMMM"),
        ("%b", "MMM"),
        ("%A", "dddd"),
        ("%a", "dddd"),
        ("%Y", "YYYY"),
        ("%y", "YY"),
    ]
    result = template
    for old, new in replacements:
        result = result.replace(old, new)
    result = result.replace("%%", "%")
    return result
