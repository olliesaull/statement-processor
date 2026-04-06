"""
Unit tests for date and money formatting helpers.

These focus on how statement data (date_format, separators) drives display
formatting after the migration from ContactConfig to self-describing JSON.
"""

from dataclasses import dataclass

import pytest

from utils.formatting import format_money
from utils.statement_view import _format_statement_value


@dataclass(frozen=True)
class DateFormatCase:
    """
    Represents a date formatting case for statement values.

    This test-only model groups inputs for _format_statement_value calls.

    Attributes:
        name: Human-friendly case id for pytest output.
        raw_value: Input statement value.
        date_fmt: Date format template (e.g. "DD/MM/YYYY") or None.
        expected: Expected formatted output.
    """

    name: str
    raw_value: str
    date_fmt: str | None
    expected: str


@dataclass(frozen=True)
class NumberFormatCase:
    """
    Represents a number formatting case for heuristic separator detection.

    This test-only model keeps numeric formatting cases readable.

    Attributes:
        name: Human-friendly case id for pytest output.
        raw_value: Input numeric string.
        expected: Expected formatted output.
    """

    name: str
    raw_value: str
    expected: str


@dataclass(frozen=True)
class FormatMoneyCase:
    """
    Represents a direct format_money passthrough case.

    This test-only model captures the formatter behavior for empty or non-numeric values.

    Attributes:
        name: Human-friendly case id for pytest output.
        raw_value: Input value passed directly to format_money.
        expected: Expected formatted output.
    """

    name: str
    raw_value: str | None
    expected: str


# region Date formatting
_DATE_FORMAT_CASES = [
    # ISO input should follow the configured template when possible.
    DateFormatCase(name="iso to configured format", raw_value="2024-03-01", date_fmt="DD/MM/YYYY", expected="01/03/2024"),
    # Unparseable values should be preserved to avoid corrupting data.
    DateFormatCase(name="preserve unparseable", raw_value="not-a-date", date_fmt="DD/MM/YYYY", expected="not-a-date"),
    # Ordinal tokens should round-trip when the template expects them.
    DateFormatCase(name="ordinal day tokens", raw_value="1st Jan 2024", date_fmt="Do MMM YYYY", expected="1st Jan 2024"),
    # Full month names should be supported by the configured template.
    DateFormatCase(name="textual months", raw_value="March 5 2024", date_fmt="MMMM D YYYY", expected="March 5 2024"),
    # Two-digit years should stay in their original output form.
    DateFormatCase(name="two digit year", raw_value="05/03/24", date_fmt="DD/MM/YY", expected="05/03/24"),
    # When no format is configured, keep ISO output.
    DateFormatCase(name="missing config uses iso", raw_value="2024-03-01", date_fmt=None, expected="2024-03-01"),
]


@pytest.mark.parametrize("case", _DATE_FORMAT_CASES, ids=[case.name for case in _DATE_FORMAT_CASES])
def test_date_formatting(case: DateFormatCase) -> None:
    result = _format_statement_value(case.raw_value, "date", case.date_fmt)
    assert result == case.expected


# endregion


# region Number formatting
_NUMBER_FORMAT_CASES = [
    # EU separators: heuristic detects comma-decimal from 2 digits after last separator.
    NumberFormatCase(name="eu separators", raw_value="1.234,50", expected="1,234.50"),
    # Standard format: comma-thousands, dot-decimal.
    NumberFormatCase(name="standard format", raw_value="1,234.50", expected="1,234.50"),
    # Space thousands with dot-decimal.
    NumberFormatCase(name="space thousands", raw_value="1 234.50", expected="1,234.50"),
    # Trailing minus (common in SA statements).
    NumberFormatCase(name="trailing minus", raw_value="126.50-", expected="-126.50"),
]


_FORMAT_MONEY_CASES = [
    # Empty strings should stay empty so UI blanks remain blank.
    FormatMoneyCase(name="empty string", raw_value="", expected=""),
    # None inputs should also return blank output.
    FormatMoneyCase(name="none value", raw_value=None, expected=""),
    # Non-numeric values should be preserved verbatim.
    FormatMoneyCase(name="non numeric", raw_value="N/A", expected="N/A"),
]


@pytest.mark.parametrize("case", _NUMBER_FORMAT_CASES, ids=[case.name for case in _NUMBER_FORMAT_CASES])
def test_number_formatting_with_separators(case: NumberFormatCase) -> None:
    result = format_money(case.raw_value)
    assert result == case.expected


@pytest.mark.parametrize("case", _FORMAT_MONEY_CASES, ids=[case.name for case in _FORMAT_MONEY_CASES])
def test_format_money_passthrough(case: FormatMoneyCase) -> None:
    assert format_money(case.raw_value) == case.expected


# endregion
