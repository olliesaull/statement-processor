"""Tests for number separator disambiguation logic."""

from core.number_disambiguation import disambiguate_number_separators, extract_monetary_values


# ── disambiguate_number_separators ──


def test_european_style_dot_thousands_comma_decimal() -> None:
    """Values like '1.234,56' → decimal=',', thousands='.'."""
    dec, thou = disambiguate_number_separators(["1.234,56"], ".", ",")
    assert dec == ","
    assert thou == "."


def test_us_style_comma_thousands_dot_decimal() -> None:
    """Values like '1,234.56' → decimal='.', thousands=','."""
    dec, thou = disambiguate_number_separators(["1,234.56"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_multiple_thousands_separators() -> None:
    """Values like '1.234.567,89' → dot appears twice so it's thousands."""
    dec, thou = disambiguate_number_separators(["1.234.567,89"], ".", ",")
    assert dec == ","
    assert thou == "."


def test_no_thousands_dot_decimal() -> None:
    """Values like '1234.56' → decimal='.', no thousands detected."""
    dec, thou = disambiguate_number_separators(["1234.56"], ".", ",")
    assert dec == "."
    # Thousands stays as LLM suggested since no evidence to contradict.
    assert thou == ","


def test_no_thousands_comma_decimal() -> None:
    """Values like '1234,56' → decimal=','."""
    dec, thou = disambiguate_number_separators(["1234,56"], ".", ",")
    assert dec == ","
    # LLM said decimal='.', which conflicts with our finding, so thousands
    # gets the LLM's decimal value.
    assert thou == "."


def test_three_digits_after_separator_is_thousands() -> None:
    """Values like '1,234' with 3 digits after → thousands=','."""
    dec, thou = disambiguate_number_separators(["1,234"], ".", ",")
    assert thou == ","
    # Decimal stays as LLM suggested.
    assert dec == "."


def test_no_separators_keeps_llm_values() -> None:
    """Plain integers like '1234' → keep LLM suggestion."""
    dec, thou = disambiguate_number_separators(["1234", "5678"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_empty_values_keeps_llm() -> None:
    """Empty value list → keep LLM suggestion."""
    dec, thou = disambiguate_number_separators([], ".", ",")
    assert dec == "."
    assert thou == ","


def test_negative_value_handled() -> None:
    """Negative values like '-1.234,56' are handled correctly."""
    dec, thou = disambiguate_number_separators(["-1.234,56"], ".", ",")
    assert dec == ","
    assert thou == "."


def test_parenthetical_negative() -> None:
    """Parenthetical negatives like '(1,234.56)' are handled correctly."""
    dec, thou = disambiguate_number_separators(["(1,234.56)"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_corrects_swapped_llm_suggestion() -> None:
    """LLM says decimal='.', thousands=',' but data shows European style."""
    dec, thou = disambiguate_number_separators(["1.234,56", "2.345,67"], ".", ",")
    assert dec == ","
    assert thou == "."


def test_confirms_correct_llm_suggestion() -> None:
    """LLM is correct — no change needed."""
    dec, thou = disambiguate_number_separators(["1,234.56"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_mixed_values_converge() -> None:
    """Multiple values with consistent separators converge."""
    values = ["1.234,56", "567,89", "12.345,00"]
    dec, thou = disambiguate_number_separators(values, ".", ",")
    assert dec == ","
    assert thou == "."


def test_currency_symbols_stripped() -> None:
    """Currency symbols like '$' or '£' are stripped before analysis."""
    dec, thou = disambiguate_number_separators(["$1,234.56", "£2,345.67"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_single_digit_decimal() -> None:
    """Values like '1,234.5' with 1 digit after → decimal='.'."""
    dec, thou = disambiguate_number_separators(["1,234.5"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_whole_number_with_thousands() -> None:
    """Values like '1,234,567' with repeated comma → thousands=','."""
    dec, thou = disambiguate_number_separators(["1,234,567"], ".", ",")
    assert thou == ","
    # No decimal evidence, keep LLM.
    assert dec == "."


def test_space_as_thousands_separator() -> None:
    """Values like '1 234,56' → thousands=' ', decimal=','."""
    dec, thou = disambiguate_number_separators(["1 234,56"], ".", " ")
    assert dec == ","
    assert thou == " "


# ── extract_monetary_values ──


def test_extract_monetary_values_single_column() -> None:
    """Extracts values from a single total column."""
    headers = ["Invoice", "Date", "Amount"]
    rows = [["INV-001", "01/01/2025", "1,234.56"], ["INV-002", "02/01/2025", "789.00"]]
    values = extract_monetary_values(headers, rows, ["Amount"])
    assert values == ["1,234.56", "789.00"]


def test_extract_monetary_values_multiple_columns() -> None:
    """Extracts values from multiple total columns (e.g. Debit/Credit)."""
    headers = ["Invoice", "Debit", "Credit"]
    rows = [["INV-001", "1,234.56", ""], ["INV-002", "", "789.00"]]
    values = extract_monetary_values(headers, rows, ["Debit", "Credit"])
    assert values == ["1,234.56", "789.00"]


def test_extract_monetary_values_case_insensitive() -> None:
    """Header matching is case-insensitive."""
    headers = ["Invoice", "AMOUNT"]
    rows = [["INV-001", "1,234.56"]]
    values = extract_monetary_values(headers, rows, ["amount"])
    assert values == ["1,234.56"]


def test_extract_monetary_values_missing_column() -> None:
    """Missing column name returns no values for that column."""
    headers = ["Invoice", "Amount"]
    rows = [["INV-001", "1,234.56"]]
    values = extract_monetary_values(headers, rows, ["Total"])
    assert values == []
