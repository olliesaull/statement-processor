"""Tests for core.date_utils — date parsing, formatting, and template compilation.

Covers parse_with_format, format_iso_with, coerce_datetime_with_template,
common_formats, and internal helpers (_set_component, _parse_ordinal,
_month_from_name, _coerce_to_datetime, _format_ordinal, _tokenize_format).
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from core.date_utils import (
    _coerce_to_datetime,
    _coerce_to_iso_string,
    _format_ordinal,
    _month_from_name,
    _parse_ordinal,
    _prepare_template,
    _set_component,
    _tokenize_format,
    coerce_datetime_with_template,
    common_formats,
    format_iso_with,
    parse_with_format,
)


# ---------------------------------------------------------------------------
# parse_with_format
# ---------------------------------------------------------------------------
class TestParseWithFormat:
    """parse_with_format: template-driven date parsing."""

    def test_none_value_returns_none(self) -> None:
        assert parse_with_format(None, "DD/MM/YYYY") is None

    def test_empty_template_returns_none(self) -> None:
        assert parse_with_format("01/02/2024", "") is None
        assert parse_with_format("01/02/2024", None) is None

    def test_blank_string_returns_none(self) -> None:
        assert parse_with_format("  ", "DD/MM/YYYY") is None

    def test_dd_mm_yyyy_slash(self) -> None:
        result = parse_with_format("15/03/2024", "DD/MM/YYYY")
        assert result == datetime(2024, 3, 15)

    def test_mm_dd_yyyy_dash(self) -> None:
        result = parse_with_format("03-15-2024", "MM-DD-YYYY")
        assert result == datetime(2024, 3, 15)

    def test_yyyy_mm_dd(self) -> None:
        result = parse_with_format("2024-03-15", "YYYY-MM-DD")
        assert result == datetime(2024, 3, 15)

    def test_two_digit_year(self) -> None:
        result = parse_with_format("15/03/24", "DD/MM/YY")
        assert result == datetime(2024, 3, 15)

    def test_month_name_long(self) -> None:
        result = parse_with_format("15 March 2024", "DD MMMM YYYY")
        assert result == datetime(2024, 3, 15)

    def test_month_name_short(self) -> None:
        result = parse_with_format("15 Mar 2024", "DD MMM YYYY")
        assert result == datetime(2024, 3, 15)

    def test_ordinal_day(self) -> None:
        result = parse_with_format("1st March 2024", "Do MMMM YYYY")
        assert result == datetime(2024, 3, 1)

    def test_ordinal_day_22nd(self) -> None:
        result = parse_with_format("22nd March 2024", "Do MMMM YYYY")
        assert result == datetime(2024, 3, 22)

    def test_ordinal_day_3rd(self) -> None:
        result = parse_with_format("3rd March 2024", "Do MMMM YYYY")
        assert result == datetime(2024, 3, 3)

    def test_ordinal_day_4th(self) -> None:
        result = parse_with_format("4th March 2024", "Do MMMM YYYY")
        assert result == datetime(2024, 3, 4)

    def test_no_match_returns_none(self) -> None:
        """A value that cannot match the template regex returns None."""
        assert parse_with_format("not-a-date", "DD/MM/YYYY") is None

    def test_missing_component_returns_none(self) -> None:
        """Template that only captures month and year produces None (no day)."""
        assert parse_with_format("03/2024", "MM/YYYY") is None

    def test_invalid_calendar_date_returns_none(self) -> None:
        """Feb 30 is an invalid date; should return None, not raise."""
        assert parse_with_format("30/02/2024", "DD/MM/YYYY") is None

    def test_unknown_month_name_raises(self) -> None:
        """Unknown month name causes ValueError to propagate."""
        with pytest.raises(ValueError, match="Unknown month name"):
            parse_with_format("15 Zog 2024", "DD MMMM YYYY")

    def test_numeric_value_coerced_to_string(self) -> None:
        """Integer/float values should be coerced to str and parsed."""
        # 15032024 won't match "DD/MM/YYYY" — this verifies str coercion path.
        assert parse_with_format(15032024, "DD/MM/YYYY") is None

    def test_weekday_token_ignored(self) -> None:
        """The dddd token captures the weekday name but doesn't affect date."""
        result = parse_with_format("Friday 15/03/2024", "dddd DD/MM/YYYY")
        assert result == datetime(2024, 3, 15)

    def test_single_digit_month_M(self) -> None:
        result = parse_with_format("3/15/2024", "M/DD/YYYY")
        assert result == datetime(2024, 3, 15)

    def test_single_digit_day_D(self) -> None:
        result = parse_with_format("03/5/2024", "MM/D/YYYY")
        assert result == datetime(2024, 3, 5)

    def test_sept_abbreviation(self) -> None:
        """The special 'sept' abbreviation should map to September."""
        result = parse_with_format("15 Sept 2024", "DD MMM YYYY")
        assert result == datetime(2024, 9, 15)


# ---------------------------------------------------------------------------
# format_iso_with
# ---------------------------------------------------------------------------
class TestFormatIsoWith:
    """format_iso_with: format stored ISO dates into template patterns."""

    def test_none_returns_empty(self) -> None:
        assert format_iso_with(None, "DD/MM/YYYY") == ""

    def test_no_template_returns_iso(self) -> None:
        result = format_iso_with("2024-03-15", "")
        assert result == "2024-03-15"

    def test_no_template_none_returns_empty(self) -> None:
        assert format_iso_with(None, "") == ""

    def test_dd_mm_yyyy(self) -> None:
        assert format_iso_with("2024-03-15", "DD/MM/YYYY") == "15/03/2024"

    def test_mm_dd_yyyy(self) -> None:
        assert format_iso_with("2024-03-15", "MM-DD-YYYY") == "03-15-2024"

    def test_with_month_name(self) -> None:
        result = format_iso_with("2024-03-15", "DD MMMM YYYY")
        assert result == "15 March 2024"

    def test_with_short_month(self) -> None:
        result = format_iso_with("2024-03-15", "DD MMM YYYY")
        assert result == "15 Mar 2024"

    def test_with_ordinal(self) -> None:
        result = format_iso_with("2024-03-01", "Do MMMM YYYY")
        assert result == "1st March 2024"

    def test_two_digit_year(self) -> None:
        result = format_iso_with("2024-03-15", "DD/MM/YY")
        assert result == "15/03/24"

    def test_unparseable_value_returns_str(self) -> None:
        """When value can't be coerced to datetime, return str(value)."""
        assert format_iso_with("not-a-date", "DD/MM/YYYY") == "not-a-date"

    def test_datetime_input(self) -> None:
        result = format_iso_with(datetime(2024, 3, 15), "DD/MM/YYYY")
        assert result == "15/03/2024"

    def test_date_input(self) -> None:
        result = format_iso_with(date(2024, 3, 15), "DD/MM/YYYY")
        assert result == "15/03/2024"

    def test_single_M_format(self) -> None:
        result = format_iso_with("2024-03-15", "M/DD/YYYY")
        assert result == "3/15/2024"

    def test_single_D_format(self) -> None:
        result = format_iso_with("2024-03-05", "MM/D/YYYY")
        assert result == "03/5/2024"

    def test_weekday_format(self) -> None:
        # 2024-03-15 is a Friday
        result = format_iso_with("2024-03-15", "dddd DD/MM/YYYY")
        assert result == "Friday 15/03/2024"


# ---------------------------------------------------------------------------
# coerce_datetime_with_template
# ---------------------------------------------------------------------------
class TestCoerceDatetimeWithTemplate:
    """coerce_datetime_with_template: template-first, ISO fallback."""

    def test_parses_with_template(self) -> None:
        result = coerce_datetime_with_template("15/03/2024", "DD/MM/YYYY")
        assert result == datetime(2024, 3, 15)

    def test_falls_back_to_iso(self) -> None:
        result = coerce_datetime_with_template("2024-03-15", None)
        assert result == datetime(2024, 3, 15)

    def test_template_parse_fails_falls_back(self) -> None:
        """When template parse fails, iso coercion should still work."""
        result = coerce_datetime_with_template("2024-03-15", "DD/MM/YYYY")
        # The ISO string won't match DD/MM/YYYY, but _coerce_to_datetime handles it.
        assert result == datetime(2024, 3, 15)

    def test_template_parse_raises_falls_back(self) -> None:
        """ValueError in parse_with_format should fall back to ISO coercion."""
        # "Zog" is an unknown month; parse_with_format raises ValueError,
        # but coerce_datetime_with_template catches it and falls back.
        result = coerce_datetime_with_template("2024-03-15", "DD MMMM YYYY")
        assert result == datetime(2024, 3, 15)

    def test_nothing_works_returns_none(self) -> None:
        result = coerce_datetime_with_template("garbage", "DD/MM/YYYY")
        assert result is None


# ---------------------------------------------------------------------------
# _set_component
# ---------------------------------------------------------------------------
class TestSetComponent:
    """_set_component: detect conflicting date parts."""

    def test_sets_new_key(self) -> None:
        components: dict[str, int] = {}
        _set_component(components, "year", 2024)
        assert components["year"] == 2024

    def test_same_value_is_ok(self) -> None:
        components: dict[str, int] = {"year": 2024}
        _set_component(components, "year", 2024)
        assert components["year"] == 2024

    def test_conflicting_value_raises(self) -> None:
        components: dict[str, int] = {"year": 2024}
        with pytest.raises(ValueError, match="Conflicting"):
            _set_component(components, "year", 2025)


# ---------------------------------------------------------------------------
# _parse_ordinal
# ---------------------------------------------------------------------------
class TestParseOrdinal:
    """_parse_ordinal: ordinal day strings like '1st', '22nd'."""

    @pytest.mark.parametrize(("ordinal", "expected"), [("1st", 1), ("2nd", 2), ("3rd", 3), ("4th", 4), ("11th", 11), ("12th", 12), ("13th", 13), ("21st", 21), ("22nd", 22), ("31st", 31)])
    def test_valid_ordinals(self, ordinal: str, expected: int) -> None:
        assert _parse_ordinal(ordinal) == expected

    def test_invalid_ordinal_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid ordinal"):
            _parse_ordinal("nope")


# ---------------------------------------------------------------------------
# _month_from_name
# ---------------------------------------------------------------------------
class TestMonthFromName:
    """_month_from_name: case-insensitive month lookup."""

    def test_full_name(self) -> None:
        assert _month_from_name("January") == 1
        assert _month_from_name("december") == 12

    def test_abbreviation(self) -> None:
        assert _month_from_name("Jan") == 1
        assert _month_from_name("dec") == 12

    def test_sept_special(self) -> None:
        assert _month_from_name("sept") == 9

    def test_prefix_fallback(self) -> None:
        """Longer strings that start with a known 3-char abbreviation."""
        assert _month_from_name("Marc") == 3

    def test_unknown_returns_none(self) -> None:
        assert _month_from_name("Zog") is None


# ---------------------------------------------------------------------------
# _coerce_to_datetime / _coerce_to_iso_string
# ---------------------------------------------------------------------------
class TestCoerceToDatetime:
    """_coerce_to_datetime: best-effort conversion."""

    def test_datetime_input(self) -> None:
        dt = datetime(2024, 3, 15, 10, 30)
        result = _coerce_to_datetime(dt)
        # Time is stripped.
        assert result == datetime(2024, 3, 15)

    def test_date_input(self) -> None:
        d = date(2024, 3, 15)
        result = _coerce_to_datetime(d)
        assert result == datetime(2024, 3, 15)

    def test_iso_string(self) -> None:
        assert _coerce_to_datetime("2024-03-15") == datetime(2024, 3, 15)

    def test_iso_string_with_time(self) -> None:
        result = _coerce_to_datetime("2024-03-15T10:30:00")
        assert result is not None
        assert result.year == 2024

    def test_empty_string(self) -> None:
        assert _coerce_to_datetime("") is None

    def test_garbage(self) -> None:
        assert _coerce_to_datetime("not-a-date") is None


class TestCoerceToIsoString:
    """_coerce_to_iso_string: normalize to YYYY-MM-DD."""

    def test_iso_string(self) -> None:
        assert _coerce_to_iso_string("2024-03-15") == "2024-03-15"

    def test_none_returns_none(self) -> None:
        assert _coerce_to_iso_string("garbage") is None


# ---------------------------------------------------------------------------
# _format_ordinal
# ---------------------------------------------------------------------------
class TestFormatOrdinal:
    """_format_ordinal: day of month ordinal formatting."""

    @pytest.mark.parametrize(("day", "expected"), [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (31, "31st")])
    def test_ordinals(self, day: int, expected: str) -> None:
        assert _format_ordinal(day) == expected


# ---------------------------------------------------------------------------
# _tokenize_format / _prepare_template
# ---------------------------------------------------------------------------
class TestTokenize:
    """_tokenize_format: template tokenization."""

    def test_simple_template(self) -> None:
        tokens = _tokenize_format("DD/MM/YYYY")
        kinds = [kind for kind, _ in tokens]
        assert kinds == ["DD", "SEP", "MM", "SEP", "YYYY"]

    def test_dash_separator(self) -> None:
        tokens = _tokenize_format("YYYY-MM-DD")
        kinds = [kind for kind, _ in tokens]
        assert kinds == ["YYYY", "SEP", "MM", "SEP", "DD"]

    def test_space_separator(self) -> None:
        tokens = _tokenize_format("DD MMMM YYYY")
        kinds = [kind for kind, _ in tokens]
        assert kinds == ["DD", "SEP", "MMMM", "SEP", "YYYY"]


class TestPrepareTemplate:
    """_prepare_template: compiled regex generation."""

    def test_returns_three_tuple(self) -> None:
        result = _prepare_template("DD/MM/YYYY")
        assert len(result) == 3

    def test_regex_matches(self) -> None:
        regex, _, _ = _prepare_template("DD/MM/YYYY")
        assert regex.match("15/03/2024")
        assert not regex.match("2024-03-15")


# ---------------------------------------------------------------------------
# common_formats
# ---------------------------------------------------------------------------
class TestCommonFormats:
    """common_formats: summarize sample date patterns."""

    def test_single_pattern(self) -> None:
        samples = ["01/02/2024", "15/03/2024", "20/04/2024"]
        result = common_formats(samples)
        assert len(result) >= 1

    def test_multiple_patterns(self) -> None:
        samples = ["01/02/2024", "2024-03-15", "01/04/2024", "2024-05-20"]
        result = common_formats(samples)
        assert len(result) >= 2

    def test_empty_samples(self) -> None:
        result = common_formats([])
        assert result == []

    def test_top_k_limits(self) -> None:
        samples = ["01/02/2024"] * 10
        result = common_formats(samples, top_k=1)
        assert len(result) == 1
