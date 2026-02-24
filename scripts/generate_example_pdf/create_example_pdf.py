from pdf2image import convert_from_path
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle

data = [
    ["Date", "Reference", "Description", "Debit", "Credit", "Balance"],
    ["2025-08-01", "REF-250801", "Opening balance", "", "", "5,000.00"],
    ["2025-08-03", "INV-250803", "Supplier payment", "1,200.00", "", "3,800.00"],
    ["2025-08-05", "CN-250805", "Credit note - maintenance adjustment", "", "150.00", "3,950.00"],
    ["2025-08-10", "Invoice # INV-250810", "Tenant service invoice", "425.00", "", "3,525.00"],
]

pdf_path = "sample_statement.pdf"
c = canvas.Canvas(pdf_path, pagesize=LETTER)
width, height = LETTER

c.setFont("Helvetica-Bold", 18)
c.drawString(72, height - 72, "Acme Property Management")

c.setFont("Helvetica", 11)
c.drawString(72, height - 92, "415 Summit Avenue")
c.drawString(72, height - 108, "Seattle, WA 98104")
c.drawString(72, height - 124, "Phone: (206) 555-0142 | accounts@acmeproperty.com")

c.setFont("Helvetica-Bold", 14)
c.drawString(72, height - 150, "Sample Statement")

c.setFont("Helvetica", 11)
c.drawString(72, height - 170, "Statement Period: August 2025")

table = Table(data, colWidths=[80, 120, 180, 60, 60, 70])
table.setStyle(
    TableStyle(
        [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.black),
            ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ]
    )
)
table_width, table_height = table.wrapOn(c, width, height)
table_x = (width - table_width) / 2
table.drawOn(c, table_x, height - 230 - table_height)

c.showPage()
c.save()

images = convert_from_path(pdf_path, dpi=150)
images[0].save("sample_statement.png", "PNG")
