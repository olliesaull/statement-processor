"""
Unit tests for date and money formatting helpers.
These focus on how contact configuration drives display formatting.
"""

from dataclasses import dataclass

import pytest

from core.models import ContactConfig
from utils.formatting import format_money
from utils.statement_view import _format_statement_value, get_date_format_from_config, get_number_separators_from_config


@dataclass(frozen=True)
class DateTemplateCase:
    """
    Represents a template extraction case for date format configs.

    This test-only model keeps the date template matrix easy to expand.

    Attributes:
        name: Human-friendly case id for pytest output.
        contact_config: Contact config under test.
        expected: Expected date format template.
    """

    name: str
    contact_config: ContactConfig
    expected: str


@dataclass(frozen=True)
class DateFormatCase:
    """
    Represents a date formatting case for statement values.

    This test-only model groups inputs for _format_statement_value calls.

    Attributes:
        name: Human-friendly case id for pytest output.
        raw_value: Input statement value.
        contact_config: Contact config used to derive the date format.
        expected: Expected formatted output.
    """

    name: str
    raw_value: str
    contact_config: ContactConfig | None
    expected: str


@dataclass(frozen=True)
class NumberFormatCase:
    """
    Represents a number formatting case driven by contact config.

    This test-only model keeps separator-driven cases readable.

    Attributes:
        name: Human-friendly case id for pytest output.
        contact_config: Contact config providing separator hints.
        raw_value: Input numeric string.
        expected: Expected formatted output.
    """

    name: str
    contact_config: ContactConfig
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


CONTACT_CONFIG_DEFAULT = ContactConfig(date_format="DD/MM/YYYY", decimal_separator=".", thousands_separator=",")
CONTACT_CONFIG_EU = ContactConfig(date_format="DD/MM/YYYY", decimal_separator=",", thousands_separator=".")
CONTACT_CONFIG_SPACE = ContactConfig(date_format="DD/MM/YYYY", decimal_separator=".", thousands_separator=" ")
CONTACT_CONFIG_INVALID_SEPARATORS = ContactConfig(decimal_separator="|", thousands_separator="?")
CONTACT_CONFIG_ORDINAL = ContactConfig(date_format="Do MMM YYYY", decimal_separator=".", thousands_separator=",")
CONTACT_CONFIG_TEXT_MONTH = ContactConfig(date_format="MMMM D YYYY", decimal_separator=".", thousands_separator=",")
CONTACT_CONFIG_TWO_DIGIT_YEAR = ContactConfig(date_format="DD/MM/YY", decimal_separator=".", thousands_separator=",")


# region Date format (contact config)
_DATE_TEMPLATE_CASES = [
    # The date helper should return the configured template unchanged.
    DateTemplateCase(name="default template", contact_config=CONTACT_CONFIG_DEFAULT, expected="DD/MM/YYYY")
]


_DATE_FORMAT_CASES = [
    # ISO input should still follow the configured template when possible.
    DateFormatCase(name="iso to configured format", raw_value="2024-03-01", contact_config=CONTACT_CONFIG_DEFAULT, expected="01/03/2024"),
    # Unparseable values should be preserved to avoid corrupting data.
    DateFormatCase(name="preserve unparseable", raw_value="not-a-date", contact_config=CONTACT_CONFIG_DEFAULT, expected="not-a-date"),
    # Ordinal tokens should round-trip when the template expects them.
    DateFormatCase(name="ordinal day tokens", raw_value="1st Jan 2024", contact_config=CONTACT_CONFIG_ORDINAL, expected="1st Jan 2024"),
    # Full month names should be supported by the configured template.
    DateFormatCase(name="textual months", raw_value="March 5 2024", contact_config=CONTACT_CONFIG_TEXT_MONTH, expected="March 5 2024"),
    # Two-digit years should stay in their original output form.
    DateFormatCase(name="two digit year", raw_value="05/03/24", contact_config=CONTACT_CONFIG_TWO_DIGIT_YEAR, expected="05/03/24"),
    # When no format is configured, keep ISO output.
    DateFormatCase(name="missing config uses iso", raw_value="2024-03-01", contact_config=None, expected="2024-03-01"),
]


@pytest.mark.parametrize("case", _DATE_TEMPLATE_CASES, ids=[case.name for case in _DATE_TEMPLATE_CASES])
def test_date_format_from_config_returns_template(case: DateTemplateCase) -> None:
    result = get_date_format_from_config(case.contact_config)
    assert result == case.expected


@pytest.mark.parametrize("case", _DATE_FORMAT_CASES, ids=[case.name for case in _DATE_FORMAT_CASES])
def test_date_formatting(case: DateFormatCase) -> None:
    date_fmt = get_date_format_from_config(case.contact_config) if case.contact_config else None
    result = _format_statement_value(case.raw_value, "date", date_fmt, ".", ",")
    assert result == case.expected


# endregion


# region Number formatting (contact config)
_NUMBER_FORMAT_CASES = [
    # EU separators should parse and normalize into the default UI format.
    NumberFormatCase(name="eu separators", contact_config=CONTACT_CONFIG_EU, raw_value="1.234,50", expected="1,234.50"),
    # Space thousands separators should normalize into standard output.
    NumberFormatCase(name="space thousands", contact_config=CONTACT_CONFIG_SPACE, raw_value="1 234.5", expected="1,234.50"),
    # Invalid separator config should fall back to defaults.
    NumberFormatCase(name="invalid config falls back", contact_config=CONTACT_CONFIG_INVALID_SEPARATORS, raw_value="1234.5", expected="1,234.50"),
    # Mismatched separator configs should return the original value.
    NumberFormatCase(name="mismatched separators", contact_config=CONTACT_CONFIG_SPACE, raw_value="1,234.50", expected="1,234.50"),
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
def test_number_formatting_with_config(case: NumberFormatCase) -> None:
    dec_sep, thou_sep = get_number_separators_from_config(case.contact_config)
    result = format_money(case.raw_value, decimal_separator=dec_sep, thousands_separator=thou_sep)
    assert result == case.expected


@pytest.mark.parametrize("case", _FORMAT_MONEY_CASES, ids=[case.name for case in _FORMAT_MONEY_CASES])
def test_format_money_passthrough(case: FormatMoneyCase) -> None:
    assert format_money(case.raw_value) == case.expected


# endregion
