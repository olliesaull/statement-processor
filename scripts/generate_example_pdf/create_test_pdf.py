"""Utility to generate a two-page PDF for testing.

The generated statement includes exact matches, substring matches, intentional no-match rows,
a payment-keyword row, an invalid date row, and a balance-forward row.
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


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
            ["Date", "Reference", "Description", "Debit", "Credit"],
            ["2025-08-09", "Balance Forward", "Balance Forward", "", ""],
            ["2025-08-01", "INV-1001", "Exact invoice match", "1,200.00", ""],
            ["2025-08-02", "Invoice # INV-1002", "Substring invoice match", "300.00", ""],
            ["2025-08-03", "INV-1003-NOMATCH", "Invoice no match", "150.00", ""],
            ["2025-08-04", "CRN-2001", "Exact credit note match", "", "75.00"],
            ["2025-08-05", "Credit Note CRN-2002", "Substring credit note match", "", "50.00"],
            ["2025-08-06", "CRN-2003-NOMATCH", "Credit note no match", "", "60.00"],
            ["2025-08-07", "Payment for INV-1004", "Payment keyword should skip match", "", "200.00"],
        ],
        colWidths=[0.9 * inch, 1.7 * inch, 2.5 * inch, 0.8 * inch, 0.8 * inch],
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
            ["2025-02-30", "INV-1005", "Invalid date row", "90.00", ""],
            ["2025-08-10", "INV-1006", "Exact invoice match (page 2)", "500.00", ""],
        ],
        colWidths=[0.9 * inch, 1.7 * inch, 2.5 * inch, 0.8 * inch, 0.8 * inch],
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
        Paragraph("Test Statements Ltd", heading_style),
        Paragraph("415 Summit Avenue", body_style),
        Paragraph("Seattle, WA 98104", body_style),
        Paragraph("Phone: (206) 555-0142 | accounts@teststatements.com", body_style),
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
