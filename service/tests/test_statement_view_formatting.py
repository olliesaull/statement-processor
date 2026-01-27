"""
Unit tests for date and money formatting helpers.
These focus on how contact configuration drives display formatting.
"""

from utils.formatting import format_money
from utils.statement_view import _format_statement_value, get_date_format_from_config, get_number_separators_from_config

CONTACT_CONFIG_DEFAULT = {"date_format": "DD/MM/YYYY", "decimal_separator": ".", "thousands_separator": ","}
CONTACT_CONFIG_EU = {"date_format": "DD/MM/YYYY", "decimal_separator": ",", "thousands_separator": "."}
CONTACT_CONFIG_SPACE = {"date_format": "DD/MM/YYYY", "decimal_separator": ".", "thousands_separator": " "}
CONTACT_CONFIG_INVALID_SEPARATORS = {"decimal_separator": "|", "thousands_separator": "?"}
CONTACT_CONFIG_ORDINAL = {"date_format": "Do MMM YYYY", "decimal_separator": ".", "thousands_separator": ","}
CONTACT_CONFIG_TEXT_MONTH = {"date_format": "MMMM D YYYY", "decimal_separator": ".", "thousands_separator": ","}
CONTACT_CONFIG_TWO_DIGIT_YEAR = {"date_format": "DD/MM/YY", "decimal_separator": ".", "thousands_separator": ","}


# region Date format (contact config)
def test_date_format_from_config_returns_template() -> None:
    """Return the configured date format string from contact config.

    We keep this simple to assert that date formatting tests are driven by the
    same config shape the app uses, rather than hardcoded templates.

    Args:
        None.

    Returns:
        None.
    """
    result = get_date_format_from_config(CONTACT_CONFIG_DEFAULT)
    assert result == "DD/MM/YYYY"


def test_date_format_formats_iso_when_template_mismatch() -> None:
    """Reformat ISO dates even when the configured template does not match input.

    Statement uploads often normalize dates to ISO. The display layer should
    still render according to the contact's configured format when possible.

    Args:
        None.

    Returns:
        None.
    """
    result = _format_statement_value("2024-03-01", "date", get_date_format_from_config(CONTACT_CONFIG_DEFAULT), ".", ",")
    assert result == "01/03/2024"


def test_date_format_preserves_unparseable_input() -> None:
    """Leave values unchanged when they cannot be parsed as dates.

    This guards against corrupting data when an OCR or user-provided value does
    not match either the configured format or ISO fallbacks.

    Args:
        None.

    Returns:
        None.
    """
    raw_value = "not-a-date"
    result = _format_statement_value(raw_value, "date", get_date_format_from_config(CONTACT_CONFIG_DEFAULT), ".", ",")
    assert result == raw_value


def test_date_format_handles_ordinal_days() -> None:
    """Format ordinal day inputs when the template uses Do.

    Some suppliers use ordinal day tokens (e.g., "1st"). We validate that the
    parser accepts them and the formatter preserves the expected output.

    Args:
        None.

    Returns:
        None.
    """
    result = _format_statement_value("1st Jan 2024", "date", get_date_format_from_config(CONTACT_CONFIG_ORDINAL), ".", ",")
    assert result == "1st Jan 2024"


def test_date_format_handles_textual_months() -> None:
    """Format full month names when configured.

    This locks in support for formats like "March 5 2024" so month-name parsing
    continues to work as templates evolve.

    Args:
        None.

    Returns:
        None.
    """
    result = _format_statement_value("March 5 2024", "date", get_date_format_from_config(CONTACT_CONFIG_TEXT_MONTH), ".", ",")
    assert result == "March 5 2024"


def test_date_format_handles_two_digit_years() -> None:
    """Support two-digit year templates without shifting output format.

    We treat "YY" as 2000-based years for parsing, but when formatting with a
    two-digit template we should keep the original "YY" output form.

    Args:
        None.

    Returns:
        None.
    """
    result = _format_statement_value("05/03/24", "date", get_date_format_from_config(CONTACT_CONFIG_TWO_DIGIT_YEAR), ".", ",")
    assert result == "05/03/24"


def test_date_format_defaults_to_iso_when_config_missing() -> None:
    """Default to ISO output when no date format is configured.

    The display layer should fall back to the canonical ISO representation when
    a contact has no configured date format to avoid losing date values.

    Args:
        None.

    Returns:
        None.
    """
    result = _format_statement_value("2024-03-01", "date", None, ".", ",")
    assert result == "2024-03-01"


# endregion


# region Number formatting (contact config)
def test_number_formatting_parses_eu_separators() -> None:
    """Parse EU-style separators using contact config and format consistently.

    The parser should accept values like "1.234,50" when the config declares
    comma decimals and dot thousands. Output stays in the standard UI format.

    Args:
        None.

    Returns:
        None.
    """
    dec_sep, thou_sep = get_number_separators_from_config(CONTACT_CONFIG_EU)
    result = format_money("1.234,50", decimal_separator=dec_sep, thousands_separator=thou_sep)
    assert result == "1,234.50"


def test_number_formatting_parses_space_thousands_separator() -> None:
    """Parse space-separated thousands when configured.

    Many statements use spaces for thousands. We ensure those values are parsed
    and formatted into the canonical output representation.

    Args:
        None.

    Returns:
        None.
    """
    dec_sep, thou_sep = get_number_separators_from_config(CONTACT_CONFIG_SPACE)
    result = format_money("1 234.5", decimal_separator=dec_sep, thousands_separator=thou_sep)
    assert result == "1,234.50"


def test_number_formatting_invalid_config_falls_back_to_defaults() -> None:
    """Fall back to default separators when config values are unsupported.

    The UI should remain stable even if invalid separators are configured. The
    parser falls back to default separators and still formats the number.

    Args:
        None.

    Returns:
        None.
    """
    dec_sep, thou_sep = get_number_separators_from_config(CONTACT_CONFIG_INVALID_SEPARATORS)
    result = format_money("1234.5", decimal_separator=dec_sep, thousands_separator=thou_sep)
    assert result == "1,234.50"


def test_number_formatting_empty_and_none_return_blank() -> None:
    """Return blank strings for empty inputs.

    The display layer treats missing totals as empty values. We ensure formatting
    keeps the output blank instead of a literal "None" or "0.00".

    Args:
        None.

    Returns:
        None.
    """
    assert format_money("") == ""
    assert format_money(None) == ""


def test_number_formatting_non_numeric_value_is_preserved() -> None:
    """Preserve non-numeric values rather than forcing a number format.

    OCR or user inputs can include placeholders like "N/A". The formatter should
    leave those intact so the UI does not hide original statement values.

    Args:
        None.

    Returns:
        None.
    """
    assert format_money("N/A") == "N/A"


# endregion


# region Config mismatch behavior
def test_number_formatting_mismatched_separators_returns_original_value() -> None:
    """Leave values unchanged when config separators do not match the statement.

    If the statement uses commas for thousands but the config expects spaces,
    the parser cannot normalize the value and returns the original string.
    This documents the current behavior so we can revisit it intentionally.

    Args:
        None.

    Returns:
        None.
    """
    dec_sep, thou_sep = get_number_separators_from_config(CONTACT_CONFIG_SPACE)
    raw_value = "1,234.50"
    result = format_money(raw_value, decimal_separator=dec_sep, thousands_separator=thou_sep)
    assert result == raw_value


# endregion
