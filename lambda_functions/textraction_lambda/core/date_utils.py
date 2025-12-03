from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date, datetime
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

MONTH_NAME_TO_NUM = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
MONTH_ABBR_TO_NUM = {abbr.lower(): idx for idx, abbr in enumerate(calendar.month_abbr) if abbr}
MONTH_NAME_TO_NUM["sept"] = 9


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
        pass

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
        if kind == "YYYY":
            parts.append(f"{dt.year:04d}")
        elif kind == "YY":
            parts.append(f"{dt.year % 100:02d}")
        elif kind == "MMMM":
            parts.append(dt.strftime("%B"))
        elif kind == "MMM":
            parts.append(dt.strftime("%b"))
        elif kind == "MM":
            parts.append(f"{dt.month:02d}")
        elif kind == "M":
            parts.append(f"{dt.month}")
        elif kind == "DD":
            parts.append(f"{dt.day:02d}")
        elif kind == "D":
            parts.append(f"{dt.day}")
        elif kind == "Do":
            parts.append(_format_ordinal(dt.day))
        elif kind == "dddd":
            parts.append(dt.strftime("%A"))
        elif kind == "SEP":
            parts.append(str(value))
        else:
            parts.append(str(value))
    return "".join(parts)


def _format_ordinal(day: int) -> str:
    suffix = "th"
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _prepare_template(template: str):
    tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal = _tokenize_format(template)
    return _compile(tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal)


def _tokenize_format(template: str):
    tokens: List[Tuple[str, str]] = []
    cursor = 0
    remaining = template
    has_textual_month = False
    numeric_month = False
    numeric_day = False
    uses_ordinal = False

    while remaining:
        matched = False
        for token in TOKEN_ORDER:
            if remaining.startswith(token):
                tokens.append((token, token))
                cursor += len(token)
                remaining = template[cursor:]
                matched = True
                if token in {"MMMM", "MMM"}:
                    has_textual_month = True
                if token in {"MM", "M"}:
                    numeric_month = True
                if token in {"DD", "D"}:
                    numeric_day = True
                if token == "Do":
                    uses_ordinal = True
                break

        if matched:
            continue

        tokens.append(("SEP", remaining[0]))
        cursor += 1
        remaining = template[cursor:]

    return tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal


def _compile(tokens: Sequence, has_textual_month: bool, numeric_month: bool, numeric_day: bool, uses_ordinal: bool):
    def name_gen():
        idx = 0
        while True:
            yield f"t{idx}"
            idx += 1

    names = name_gen()

    regex_parts: List[str] = []
    group_order: List[Tuple[str, str]] = []
    for kind, value in tokens:
        if kind in TOKEN_REGEX:
            name = next(names)
            regex_parts.append(TOKEN_REGEX[kind].format(name=name))
            group_order.append((name, kind))
        else:
            regex_parts.append(re.escape(str(value)))

    regex = re.compile("".join(regex_parts) + r"$")
    return regex, group_order, tokens, has_textual_month, numeric_month, numeric_day, uses_ordinal


def common_formats(samples: Iterable[str], top_k: int = 5) -> List[str]:
    def normalize_template(template: str) -> str:
        cleaned = []
        for c in template:
            if c.isalpha():
                cleaned.append("A")
            elif c.isdigit():
                cleaned.append("9")
            else:
                cleaned.append(c)
        return "".join(cleaned)

    normalized = Counter()
    for s in samples:
        template = "".join("Y" if c.isdigit() else "M" if c.isalpha() else c for c in s)
        normalized[normalize_template(template)] += 1

    return [tpl for tpl, _ in normalized.most_common(top_k)]
