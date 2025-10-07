"""Utility to generate a two-page PDF for testing."""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def build_test_pdf(destination: Path) -> None:
    """Create a PDF with a headered table on page one and a plain table on page two."""
    doc = SimpleDocTemplate(str(destination), pagesize=LETTER, topMargin=72, bottomMargin=72)

    styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        "StatementHeading",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
    )
    subheading_style = ParagraphStyle(
        "StatementSubheading",
        parent=styles["Heading2"],
        fontName="Helvetica",
        fontSize=12,
        leading=16,
    )
    body_style = styles["Normal"]
    body_style.fontName = "Helvetica"
    body_style.fontSize = 10
    body_style.leading = 14

    statement_summary = Table(
        [
            ["Statement Period", "August 2025", "Account", "Main Operating"],
            ["Prepared For", "Northgate Apartments", "Prepared On", "2025-08-31"],
        ],
        colWidths=[1.6 * inch, 1.6 * inch, 1.3 * inch, 1.5 * inch],
        hAlign="LEFT",
    )
    statement_summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )

    ledger_with_headers = Table(
        [
            ["Date", "Reference", "Description", "Debit", "Credit", "Balance"],
            ["2025-08-01", "REF-250801", "Opening balance", "", "", "5,000.00"],
            ["2025-08-03", "INV-250803", "Supplier payment", "1,200.00", "", "3,800.00"],
            ["2025-08-05", "CN-250805", "Credit note - maintenance adjustment", "", "150.00","3,950.00"],
            ["2025-08-10", "INV-250810", "Tenant service invoice", "425.00", "", "3,525.00"],
            ["2025-08-16", "RCPT-250816", "Rent collection", "", "2,200.00", "5,725.00"],
            ["2025-08-22", "FEE-250822", "Management fee", "350.00", "", "5,375.00"],
        ],
        colWidths=[0.9 * inch, 1.1 * inch, 2.2 * inch, 0.8 * inch, 0.8 * inch, 0.9 * inch],
        hAlign="LEFT",
    )
    ledger_with_headers.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, 0), 11),
                ("FONTSIZE", (0, 1), (-1, -1), 10),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )

    continuation_table = Table(
        [
            ["2025-08-24", "ADJ-250824", "Adjustment posted", "125.00", "", "5,500.00"],
            ["2025-08-25", "RCPT-250825", "Utility reimbursement", "", "300.00", "5,800.00"],
            ["2025-08-26", "REP-250826", "Emergency repair", "850.00", "", "4,950.00"],
            ["2025-08-28", "DEP-250828", "Security deposit release", "1,000.00", "", "3,950.00"],
            ["2025-08-29", "DIST-250829", "Owner distribution", "2,500.00", "", "1,450.00"],
            ["2025-08-30", "INT-250830", "Interest earned", "", "5.75", "1,455.75"],
            ["2025-08-31", "BAL-250831", "Statement closing balance", "", "", "1,455.75"],
        ],
        colWidths=[0.9 * inch, 1.1 * inch, 2.2 * inch, 0.8 * inch, 0.8 * inch, 0.9 * inch],
        hAlign="LEFT",
    )
    continuation_table.setStyle(
        TableStyle(
            [
                ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ]
        )
    )

    story = [
        Paragraph("Acme Property Management", heading_style),
        Paragraph("415 Summit Avenue", body_style),
        Paragraph("Seattle, WA 98104", body_style),
        Paragraph("Phone: (206) 555-0142 | accounts@acmeproperty.com", body_style),
        Spacer(1, 0.25 * inch),
        Paragraph("Monthly Statement", subheading_style),
        Paragraph("Property: Northgate Apartments", body_style),
        Spacer(1, 0.15 * inch),
        statement_summary,
        Spacer(1, 0.3 * inch),
        Paragraph("Account Activity", subheading_style),
        ledger_with_headers,
        PageBreak(),
        Paragraph("Account Activity (continued)", subheading_style),
        Paragraph("Transactions below continue the current period activity without repeated headers.", body_style),
        Spacer(1, 0.15 * inch),
        continuation_table,
    ]

    doc.build(story)


def main() -> None:
    output_path = Path(__file__).resolve().parent / "test_pdf.pdf"
    build_test_pdf(output_path)


if __name__ == "__main__":
    main()
