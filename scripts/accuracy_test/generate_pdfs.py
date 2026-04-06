"""Generate synthetic PDFs with known expected JSON for accuracy testing.

Each scenario creates a PDF with reportlab and returns the expected
extraction result. The PDFs are intentionally simple — the goal is
deterministic content for accuracy validation, not visual fidelity.

Public API:
    generate_all_scenarios() → list[(scenario_name, pdf_bytes, expected_dict)]
"""

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_pdf(
    tables_per_page: list[list[list[str]]],
    title: str = "Statement",
) -> bytes:
    """Build a PDF with one table per page.

    Args:
        tables_per_page: Each entry is a list of rows (including header row)
            to render as a single-page table.
        title: Ignored in content — kept for clarity in caller code.

    Returns:
        Raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    elements: list[Any] = []

    for page_idx, rows in enumerate(tables_per_page):
        if page_idx > 0:
            # Force page break between tables.
            elements.append(Spacer(1, 0))
            elements[-1].keepWithNext = False

        tbl = Table(rows)
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        elements.append(tbl)

        # Page break after each table except the last.
        if page_idx < len(tables_per_page) - 1:
            from reportlab.platypus import PageBreak

            elements.append(PageBreak())

    doc.build(elements)
    return buf.getvalue()


def _item(
    date: str = "",
    number: str = "",
    total: dict[str, Any] | None = None,
    due_date: str = "",
    reference: str = "",
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a statement_item dict for expected output."""
    return {
        "date": date,
        "number": number,
        "total": total or {},
        "due_date": due_date,
        "reference": reference,
        "raw": raw or {},
    }


# ---------------------------------------------------------------------------
# Scenario 1: Simple single-page, clean table
# ---------------------------------------------------------------------------


def _scenario_simple() -> tuple[bytes, dict[str, Any]]:
    """Baseline sanity — standard fields, header detection, header_mapping."""
    header = ["Date", "Invoice No.", "Amount", "Balance"]
    rows = [
        ["15/01/2024", "INV-001", "1,234.56", "1,234.56"],
        ["20/01/2024", "INV-002", "789.00", "2,023.56"],
        ["25/01/2024", "CRN-001", "-150.00", "1,873.56"],
    ]
    pdf_bytes = _build_pdf([  [header] + rows  ], title="Simple Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Amount", "Balance"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Amount": "total",
            "Balance": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": [
            _item(
                date="15/01/2024", number="INV-001",
                total={"Amount": 1234.56, "Balance": 1234.56},
                raw={"date": "15/01/2024", "number": "INV-001", "Amount": "1,234.56", "Balance": "1,234.56"},
            ),
            _item(
                date="20/01/2024", number="INV-002",
                total={"Amount": 789.00, "Balance": 2023.56},
                raw={"date": "20/01/2024", "number": "INV-002", "Amount": "789.00", "Balance": "2,023.56"},
            ),
            _item(
                date="25/01/2024", number="CRN-001",
                total={"Amount": -150.00, "Balance": 1873.56},
                raw={"date": "25/01/2024", "number": "CRN-001", "Amount": "-150.00", "Balance": "1,873.56"},
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 2: Multi-page requiring chunking (~15 pages)
# ---------------------------------------------------------------------------


def _scenario_multipage() -> tuple[bytes, dict[str, Any]]:
    """Multi-page statement that requires chunking. ~15 pages of items."""
    header = ["Date", "Invoice No.", "Debit", "Credit", "Balance"]
    items_per_page = 30
    total_pages = 15

    all_rows: list[dict[str, Any]] = []
    pages: list[list[list[str]]] = []
    running_balance = 0.0
    item_idx = 0

    for page in range(total_pages):
        page_rows: list[list[str]] = [header]
        for _ in range(items_per_page):
            item_idx += 1
            # Alternate months so dates span a range, with days > 12
            # to keep date_confidence high.
            month = ((item_idx - 1) % 6) + 1
            day = 15 + (item_idx % 10)  # Days 15-24 — always > 12
            date_str = f"{day:02d}/{month:02d}/2024"
            inv_num = f"INV-{item_idx:04d}"

            debit = round(100.0 + (item_idx * 1.5), 2)
            running_balance = round(running_balance + debit, 2)

            page_rows.append([
                date_str, inv_num, f"{debit:,.2f}", "", f"{running_balance:,.2f}",
            ])
            all_rows.append(
                _item(
                    date=date_str, number=inv_num,
                    total={"Debit": debit, "Credit": "", "Balance": running_balance},
                    raw={
                        "date": date_str, "number": inv_num,
                        "Debit": f"{debit:,.2f}", "Credit": "",
                        "Balance": f"{running_balance:,.2f}",
                    },
                )
            )
        pages.append(page_rows)

    pdf_bytes = _build_pdf(pages, title="Multi-page Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Debit", "Credit", "Balance"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Debit": "total",
            "Credit": "total",
            "Balance": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": all_rows,
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 3: Chunk-boundary duplication
# ---------------------------------------------------------------------------


def _scenario_chunk_boundary_duplication() -> tuple[bytes, dict[str, Any]]:
    """Identical row at end of page 10 and start of page 11.

    With CHUNK_SIZE=10, the overlap means page 10 appears in both
    chunk 1 (pages 1-10) and chunk 2 (pages 10-11). The dedup logic
    should drop the duplicate while keeping non-duplicates.
    """
    header = ["Date", "Invoice No.", "Amount"]
    items_per_page = 25
    pages: list[list[list[str]]] = []
    all_expected_items: list[dict[str, Any]] = []
    item_idx = 0

    for page in range(11):  # 11 pages — chunk boundary at page 10
        page_rows: list[list[str]] = [header]
        for row_in_page in range(items_per_page):
            item_idx += 1
            day = 13 + (item_idx % 15)  # Days 13-27 — always > 12
            date_str = f"{day:02d}/03/2024"
            inv_num = f"INV-{item_idx:04d}"
            amount = round(50.0 + item_idx * 2.0, 2)

            page_rows.append([date_str, inv_num, f"{amount:.2f}"])
            all_expected_items.append(
                _item(
                    date=date_str, number=inv_num,
                    total={"Amount": amount},
                    raw={"date": date_str, "number": inv_num, "Amount": f"{amount:.2f}"},
                )
            )
        pages.append(page_rows)

    # The last row of page 10 and first row of page 11 will naturally
    # be different items. The LLM + overlap page is what causes the
    # duplication in production — the synthetic PDF just needs enough
    # pages to trigger chunking. The dedup test validates the logic
    # in unit tests (Task 5); here we validate the full pipeline
    # handles multi-chunk PDFs correctly.

    pdf_bytes = _build_pdf(pages, title="Chunk Boundary Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Amount"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Amount": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": "",
        "statement_items": all_expected_items,
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 4: Ambiguous dates (all days ≤ 12)
# ---------------------------------------------------------------------------


def _scenario_ambiguous_dates() -> tuple[bytes, dict[str, Any]]:
    """All dates have day ≤ 12 — format genuinely ambiguous (DD/MM vs MM/DD).

    The LLM should return date_confidence: "low".
    """
    header = ["Date", "Invoice No.", "Amount"]
    # All days 01-12 so DD/MM and MM/DD are both valid.
    rows = [
        ["03/04/2024", "INV-101", "500.00"],
        ["05/06/2024", "INV-102", "750.00"],
        ["01/12/2024", "INV-103", "200.00"],
        ["08/09/2024", "INV-104", "1,100.00"],
        ["11/02/2024", "INV-105", "350.00"],
    ]

    pdf_bytes = _build_pdf([  [header] + rows  ], title="Ambiguous Dates Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Amount"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Amount": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "low",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": [
            _item(
                date="03/04/2024", number="INV-101",
                total={"Amount": 500.00},
                raw={"date": "03/04/2024", "number": "INV-101", "Amount": "500.00"},
            ),
            _item(
                date="05/06/2024", number="INV-102",
                total={"Amount": 750.00},
                raw={"date": "05/06/2024", "number": "INV-102", "Amount": "750.00"},
            ),
            _item(
                date="01/12/2024", number="INV-103",
                total={"Amount": 200.00},
                raw={"date": "01/12/2024", "number": "INV-103", "Amount": "200.00"},
            ),
            _item(
                date="08/09/2024", number="INV-104",
                total={"Amount": 1100.00},
                raw={"date": "08/09/2024", "number": "INV-104", "Amount": "1,100.00"},
            ),
            _item(
                date="11/02/2024", number="INV-105",
                total={"Amount": 350.00},
                raw={"date": "11/02/2024", "number": "INV-105", "Amount": "350.00"},
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 5: "Reference" column containing invoice numbers
# ---------------------------------------------------------------------------


def _scenario_reference_as_invoice() -> tuple[bytes, dict[str, Any]]:
    """PDF header says "Reference" but values are invoice numbers.

    The LLM should map by content (invoice numbers → `number`),
    not by header name.
    """
    header = ["Date", "Reference", "Your Ref", "Debit", "Credit"]
    rows = [
        ["15/01/2024", "INV-2001", "PO-5501", "3,200.00", ""],
        ["18/01/2024", "CRN-0050", "PO-5502", "", "400.00"],
        ["22/01/2024", "INV-2002", "PO-5503", "1,750.00", ""],
    ]

    pdf_bytes = _build_pdf([  [header] + rows  ], title="Reference Column Statement")

    expected = {
        "detected_headers": ["Date", "Reference", "Your Ref", "Debit", "Credit"],
        "header_mapping": {
            "Date": "date",
            "Reference": "number",
            "Your Ref": "reference",
            "Debit": "total",
            "Credit": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": [
            _item(
                date="15/01/2024", number="INV-2001", reference="PO-5501",
                total={"Debit": 3200.00, "Credit": ""},
                raw={
                    "date": "15/01/2024", "number": "INV-2001",
                    "reference": "PO-5501", "Debit": "3,200.00", "Credit": "",
                },
            ),
            _item(
                date="18/01/2024", number="CRN-0050", reference="PO-5502",
                total={"Debit": "", "Credit": 400.00},
                raw={
                    "date": "18/01/2024", "number": "CRN-0050",
                    "reference": "PO-5502", "Debit": "", "Credit": "400.00",
                },
            ),
            _item(
                date="22/01/2024", number="INV-2002", reference="PO-5503",
                total={"Debit": 1750.00, "Credit": ""},
                raw={
                    "date": "22/01/2024", "number": "INV-2002",
                    "reference": "PO-5503", "Debit": "1,750.00", "Credit": "",
                },
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 6: Comma decimal, space thousands (European format)
# ---------------------------------------------------------------------------


def _scenario_comma_decimal() -> tuple[bytes, dict[str, Any]]:
    """European number format: comma decimal, space thousands (e.g. 1 234,56).

    Tests separator detection and convert_amount() correctness.
    """
    header = ["Date", "Invoice No.", "Amount", "Balance"]
    rows = [
        ["15.03.2024", "FKT-001", "1 234,56", "1 234,56"],
        ["18.03.2024", "FKT-002", "567,89", "1 802,45"],
        ["22.03.2024", "FKT-003", "12 450,00", "14 252,45"],
    ]

    pdf_bytes = _build_pdf([  [header] + rows  ], title="European Format Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Amount", "Balance"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Amount": "total",
            "Balance": "total",
        },
        "date_format": "DD.MM.YYYY",
        "date_confidence": "high",
        "decimal_separator": ",",
        "thousands_separator": " ",
        "statement_items": [
            _item(
                date="15.03.2024", number="FKT-001",
                total={"Amount": 1234.56, "Balance": 1234.56},
                raw={"date": "15.03.2024", "number": "FKT-001", "Amount": "1 234,56", "Balance": "1 234,56"},
            ),
            _item(
                date="18.03.2024", number="FKT-002",
                total={"Amount": 567.89, "Balance": 1802.45},
                raw={"date": "18.03.2024", "number": "FKT-002", "Amount": "567,89", "Balance": "1 802,45"},
            ),
            _item(
                date="22.03.2024", number="FKT-003",
                total={"Amount": 12450.00, "Balance": 14252.45},
                raw={"date": "22.03.2024", "number": "FKT-003", "Amount": "12 450,00", "Balance": "14 252,45"},
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 7: Currency symbols (R, ZAR, $ prefixes)
# ---------------------------------------------------------------------------


def _scenario_currency_symbols() -> tuple[bytes, dict[str, Any]]:
    """Monetary values prefixed with currency symbols (R, ZAR, $).

    Tests currency stripping before numeric conversion.
    """
    header = ["Date", "Invoice No.", "Amount"]
    rows = [
        ["15/01/2024", "INV-301", "R1,234.56"],
        ["18/01/2024", "INV-302", "ZAR 5,678.90"],
        ["22/01/2024", "INV-303", "$2,345.67"],
        ["25/01/2024", "CRN-050", "R450.00-"],
    ]

    pdf_bytes = _build_pdf([  [header] + rows  ], title="Currency Symbols Statement")

    expected = {
        "detected_headers": ["Date", "Invoice No.", "Amount"],
        "header_mapping": {
            "Date": "date",
            "Invoice No.": "number",
            "Amount": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": [
            _item(
                date="15/01/2024", number="INV-301",
                total={"Amount": 1234.56},
                raw={"date": "15/01/2024", "number": "INV-301", "Amount": "R1,234.56"},
            ),
            _item(
                date="18/01/2024", number="INV-302",
                total={"Amount": 5678.90},
                raw={"date": "18/01/2024", "number": "INV-302", "Amount": "ZAR 5,678.90"},
            ),
            _item(
                date="22/01/2024", number="INV-303",
                total={"Amount": 2345.67},
                raw={"date": "22/01/2024", "number": "INV-303", "Amount": "$2,345.67"},
            ),
            _item(
                date="25/01/2024", number="CRN-050",
                total={"Amount": -450.00},
                raw={"date": "25/01/2024", "number": "CRN-050", "Amount": "R450.00-"},
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Scenario 8: BBF/EFT/Payment rows mixed with invoices
# ---------------------------------------------------------------------------


def _scenario_mixed_payments() -> tuple[bytes, dict[str, Any]]:
    """Statement with Balance Brought Forward, EFT payments, and invoices.

    Tests that dedup does NOT false-positive on adjacent payments
    with same amount but different dates. Also validates BBF extraction.
    """
    header = ["Date", "Transaction No.", "Description", "Debit", "Credit", "Balance"]
    rows = [
        ["01/02/2024", "", "Balance Brought Forward", "", "", "5,000.00"],
        ["05/02/2024", "INV-4001", "Sales - Widget Pack A", "1,200.00", "", "6,200.00"],
        ["08/02/2024", "INV-4002", "Sales - Widget Pack B", "800.00", "", "7,000.00"],
        ["10/02/2024", "EFT-001", "Payment received - thank you", "", "2,500.00", "4,500.00"],
        ["15/02/2024", "INV-4003", "Sales - Widget Pack C", "1,500.00", "", "6,000.00"],
        ["18/02/2024", "EFT-002", "Payment received - thank you", "", "2,500.00", "3,500.00"],
        ["22/02/2024", "CRN-010", "Credit note - returned goods", "", "300.00", "3,200.00"],
        ["28/02/2024", "INV-4004", "Sales - Widget Pack D", "950.00", "", "4,150.00"],
    ]

    pdf_bytes = _build_pdf([  [header] + rows  ], title="Mixed Payments Statement")

    expected = {
        "detected_headers": [
            "Date", "Transaction No.", "Description", "Debit", "Credit", "Balance",
        ],
        "header_mapping": {
            "Date": "date",
            "Transaction No.": "number",
            "Description": "total",
            "Debit": "total",
            "Credit": "total",
            "Balance": "total",
        },
        "date_format": "DD/MM/YYYY",
        "date_confidence": "high",
        "decimal_separator": ".",
        "thousands_separator": ",",
        "statement_items": [
            _item(
                date="01/02/2024", number="",
                total={
                    "Description": "Balance Brought Forward",
                    "Debit": "", "Credit": "", "Balance": 5000.00,
                },
                raw={
                    "date": "01/02/2024", "number": "",
                    "Description": "Balance Brought Forward",
                    "Debit": "", "Credit": "", "Balance": "5,000.00",
                },
            ),
            _item(
                date="05/02/2024", number="INV-4001",
                total={
                    "Description": "Sales - Widget Pack A",
                    "Debit": 1200.00, "Credit": "", "Balance": 6200.00,
                },
                raw={
                    "date": "05/02/2024", "number": "INV-4001",
                    "Description": "Sales - Widget Pack A",
                    "Debit": "1,200.00", "Credit": "", "Balance": "6,200.00",
                },
            ),
            _item(
                date="08/02/2024", number="INV-4002",
                total={
                    "Description": "Sales - Widget Pack B",
                    "Debit": 800.00, "Credit": "", "Balance": 7000.00,
                },
                raw={
                    "date": "08/02/2024", "number": "INV-4002",
                    "Description": "Sales - Widget Pack B",
                    "Debit": "800.00", "Credit": "", "Balance": "7,000.00",
                },
            ),
            _item(
                date="10/02/2024", number="EFT-001",
                total={
                    "Description": "Payment received - thank you",
                    "Debit": "", "Credit": 2500.00, "Balance": 4500.00,
                },
                raw={
                    "date": "10/02/2024", "number": "EFT-001",
                    "Description": "Payment received - thank you",
                    "Debit": "", "Credit": "2,500.00", "Balance": "4,500.00",
                },
            ),
            _item(
                date="15/02/2024", number="INV-4003",
                total={
                    "Description": "Sales - Widget Pack C",
                    "Debit": 1500.00, "Credit": "", "Balance": 6000.00,
                },
                raw={
                    "date": "15/02/2024", "number": "INV-4003",
                    "Description": "Sales - Widget Pack C",
                    "Debit": "1,500.00", "Credit": "", "Balance": "6,000.00",
                },
            ),
            _item(
                date="18/02/2024", number="EFT-002",
                total={
                    "Description": "Payment received - thank you",
                    "Debit": "", "Credit": 2500.00, "Balance": 3500.00,
                },
                raw={
                    "date": "18/02/2024", "number": "EFT-002",
                    "Description": "Payment received - thank you",
                    "Debit": "", "Credit": "2,500.00", "Balance": "3,500.00",
                },
            ),
            _item(
                date="22/02/2024", number="CRN-010",
                total={
                    "Description": "Credit note - returned goods",
                    "Debit": "", "Credit": 300.00, "Balance": 3200.00,
                },
                raw={
                    "date": "22/02/2024", "number": "CRN-010",
                    "Description": "Credit note - returned goods",
                    "Debit": "", "Credit": "300.00", "Balance": "3,200.00",
                },
            ),
            _item(
                date="28/02/2024", number="INV-4004",
                total={
                    "Description": "Sales - Widget Pack D",
                    "Debit": 950.00, "Credit": "", "Balance": 4150.00,
                },
                raw={
                    "date": "28/02/2024", "number": "INV-4004",
                    "Description": "Sales - Widget Pack D",
                    "Debit": "950.00", "Credit": "", "Balance": "4,150.00",
                },
            ),
        ],
    }
    return pdf_bytes, expected


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SCENARIOS: list[tuple[str, Any]] = [
    ("01_simple", _scenario_simple),
    ("02_multipage", _scenario_multipage),
    ("03_chunk_boundary", _scenario_chunk_boundary_duplication),
    ("04_ambiguous_dates", _scenario_ambiguous_dates),
    ("05_reference_as_invoice", _scenario_reference_as_invoice),
    ("06_comma_decimal", _scenario_comma_decimal),
    ("07_currency_symbols", _scenario_currency_symbols),
    ("08_mixed_payments", _scenario_mixed_payments),
]


def generate_all_scenarios() -> list[tuple[str, bytes, dict[str, Any]]]:
    """Generate all test scenarios.

    Returns:
        List of (scenario_name, pdf_bytes, expected_dict) tuples.
    """
    results: list[tuple[str, bytes, dict[str, Any]]] = []
    for name, fn in _SCENARIOS:
        pdf_bytes, expected = fn()
        results.append((name, pdf_bytes, expected))
    return results


if __name__ == "__main__":
    import json
    from pathlib import Path

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    scenarios = generate_all_scenarios()
    for name, pdf_bytes, expected in scenarios:
        pdf_path = output_dir / f"{name}.pdf"
        pdf_path.write_bytes(pdf_bytes)

        expected_path = output_dir / f"{name}_expected.json"
        expected_path.write_text(json.dumps(expected, indent=2, ensure_ascii=False))

        items = expected.get("statement_items", [])
        print(f"  {name}: {len(pdf_bytes)} bytes, {len(items)} items → {pdf_path.name}, {expected_path.name}")

    print(f"\n{len(scenarios)} scenarios written to {output_dir}")
