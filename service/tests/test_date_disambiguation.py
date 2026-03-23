"""Tests for date format disambiguation logic."""

from core.date_disambiguation import disambiguate_date_format


def test_unambiguous_dd_mm_when_day_exceeds_12() -> None:
    """A value like '15/03/2025' proves DD/MM ordering."""
    result = disambiguate_date_format(["03/05/2025", "15/03/2025", "07/08/2025"], "DD/MM/YYYY")
    assert result == "DD/MM/YYYY"


def test_unambiguous_mm_dd_when_day_exceeds_12_in_second_position() -> None:
    """A value like '03/15/2025' proves MM/DD ordering."""
    result = disambiguate_date_format(["05/03/2025", "03/15/2025"], "MM/DD/YYYY")
    assert result == "MM/DD/YYYY"


def test_fully_ambiguous_returns_empty() -> None:
    """When all dates have day and month <= 12, result is empty."""
    result = disambiguate_date_format(["03/05/2025", "07/08/2025", "01/12/2025"], "DD/MM/YYYY")
    assert result == ""


def test_empty_date_list_returns_empty() -> None:
    """No dates to analyze means ambiguous."""
    result = disambiguate_date_format([], "DD/MM/YYYY")
    assert result == ""


def test_non_numeric_dates_passthrough() -> None:
    """Dates with month names (e.g. 'D MMMM YYYY') are never ambiguous."""
    result = disambiguate_date_format(["5 January 2025", "3 March 2025"], "D MMMM YYYY")
    assert result == "D MMMM YYYY"


def test_corrects_llm_format_when_data_contradicts() -> None:
    """If LLM says MM/DD but data proves DD/MM, the format should be corrected."""
    result = disambiguate_date_format(
        ["15/03/2025", "20/06/2025"],
        "MM/DD/YYYY",  # LLM got it wrong
    )
    assert result == "DD/MM/YYYY"


def test_preserves_llm_format_when_unambiguous() -> None:
    """The LLM-suggested format string is returned as-is when confirmed."""
    result = disambiguate_date_format(["25/03/2025"], "DD/MM/YYYY")
    assert result == "DD/MM/YYYY"
