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


def test_trailing_minus_accounting_style() -> None:
    """Accounting-style negatives like '126.50-' with trailing minus."""
    dec, thou = disambiguate_number_separators(["126.50-", "3,848.97"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_trailing_minus_only_values() -> None:
    """All values have trailing minus — should still detect decimal correctly."""
    dec, thou = disambiguate_number_separators(["126.50-", "57.50-", "166.75-", "320.10-"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_trailing_minus_with_thousands() -> None:
    """Trailing minus on value with thousands separator: '38,201.21-'."""
    dec, thou = disambiguate_number_separators(["38,201.21-"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_peninsula_beverages_total_column() -> None:
    """Real data from Peninsula Beverages statement Total column.

    Mix of values with/without thousands separators and trailing minus signs.
    All use US/UK convention: comma=thousands, dot=decimal.
    """
    values = ["3,848.97", "126.50-", "260.29", "57.50-", "2,583.95", "166.75-", "2,202.26", "320.10-", "88.53", "4,783.47", "80.50-", "2,544.29", "126.50-", "1,640.46"]
    dec, thou = disambiguate_number_separators(values, ".", ",")
    assert dec == "."
    assert thou == ","


def test_peninsula_beverages_llm_swapped() -> None:
    """Same real data but LLM returns swapped separators — should correct."""
    values = ["3,848.97", "126.50-", "260.29", "57.50-", "2,583.95", "166.75-", "2,202.26", "320.10-", "88.53", "4,783.47", "80.50-", "2,544.29", "126.50-", "1,640.46"]
    dec, thou = disambiguate_number_separators(values, ",", ".")
    assert dec == "."
    assert thou == ","


def test_all_zero_values() -> None:
    """All '0.00' values — should still detect decimal='.'."""
    dec, thou = disambiguate_number_separators(["0.00", "0.00", "0.00"], ".", ",")
    assert dec == "."
    assert thou == ","


def test_mix_of_zero_and_nonzero() -> None:
    """Mix of '0.00' and real values."""
    values = ["0.00", "3,848.97", "0.00", "126.50-"]
    dec, thou = disambiguate_number_separators(values, ".", ",")
    assert dec == "."
    assert thou == ","


def test_small_values_no_thousands() -> None:
    """All values under 1000 — no thousands separator present."""
    values = ["260.29", "57.50", "88.53", "80.50"]
    dec, thou = disambiguate_number_separators(values, ".", ",")
    assert dec == "."
    # No thousands evidence, keep LLM default.
    assert thou == ","


def test_trailing_plus_sign() -> None:
    """Trailing plus sign like '126.50+'."""
    dec, thou = disambiguate_number_separators(["126.50+", "3,848.97"], ".", ",")
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


def test_extract_monetary_values_missing_column_no_fallback_data() -> None:
    """Missing column with no monetary-looking values returns empty."""
    headers = ["Invoice", "Status"]
    rows = [["INV-001", "Active"]]
    values = extract_monetary_values(headers, rows, ["Total"])
    assert values == []


def test_extract_fallback_when_headers_dont_match() -> None:
    """When total column names don't match headers, scan all cells."""
    # Textract picked up title row instead of real column headers.
    headers = ["ITEMS NOT", "YET PAID", "AS AT DATE", "THIS STATEMENT", "", "", ""]
    rows = [["03.07.2023", "76977177", "SKC CLM ORDER", "208078519", "", "3,848.97", "0.00"], ["03.07.2023", "76982233", "0076977177", "208078520", "", "0.00", "126.50-"]]
    values = extract_monetary_values(headers, rows, ["Invoices", "Credit Notes", "Total"])
    # Should find monetary values via fallback scan.
    assert "3,848.97" in values
    assert "126.50-" in values


def test_extract_fallback_excludes_date_columns() -> None:
    """Fallback scan skips columns matching exclude_columns."""
    headers = ["TITLE ROW", "", "", ""]
    rows = [["03.07.2023", "76977177", "3,848.97", "0.00"]]
    # "Doc date" doesn't match any header, so exclude_columns won't filter
    # by index — but date values are filtered by _looks_monetary rejecting
    # date-like patterns.
    values = extract_monetary_values(headers, rows, ["Total"], exclude_columns=["Doc date"])
    assert "3,848.97" in values
    assert "03.07.2023" not in values


def test_extract_fallback_excludes_dates_by_pattern() -> None:
    """Date-like values (DD/MM/YYYY) are excluded from fallback."""
    headers = ["A", "B", "C"]
    rows = [["03/07/2023", "3,848.97", "text"], ["2023-07-03", "126.50-", "more text"]]
    values = extract_monetary_values(headers, rows, ["Total"])
    assert "3,848.97" in values
    assert "126.50-" in values
    assert "03/07/2023" not in values
    assert "2023-07-03" not in values


def test_extract_fallback_skips_plain_integers_and_text() -> None:
    """Plain integers and text without separators are excluded."""
    headers = ["A", "B", "C", "D"]
    rows = [["76977177", "SKC ORDER", "3,848.97", "1234"]]
    values = extract_monetary_values(headers, rows, ["Total"])
    assert values == ["3,848.97"]


def test_extract_fallback_peninsula_beverages_real_data() -> None:
    """Full Peninsula Beverages scenario: title row headers, real data."""
    headers = ["ITEMS NOT", "YET PAID/CLEARED", "AS AT DATE OF", "THIS STATEMENT", "", "", "", "", "", "", "", ""]
    rows = [
        ["Doc date", "Invoice No.", "Cross Ref", "Doc Ref", "Branch", "Invoices", "Credit Notes", "Clearing", "Payment Not", "Total", "Remittance Advice"],
        ["03.07.2023", "76977177", "SKC CLM ORDER", "208078519", "", "3,848.97", "0.00", "0.00", "0.00", "3,848.97", ""],
        ["03.07.2023", "76982233", "0076977177", "208078520", "", "0.00", "126.50-", "0.00", "0.00", "126.50-", ""],
        ["04.07.2023", "76982497", "SKC ORDER - TABL", "208083269", "", "260.29", "0.00", "0.00", "0.00", "260.29", ""],
    ]
    values = extract_monetary_values(headers, rows, ["Invoices", "Credit Notes", "Total"], exclude_columns=["Doc date"])
    # Should find monetary values via fallback.
    assert len(values) > 0
    assert "3,848.97" in values
    assert "126.50-" in values
    assert "260.29" in values
    # Dates should be excluded.
    assert "03.07.2023" not in values
    assert "04.07.2023" not in values
