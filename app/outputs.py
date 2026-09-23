import csv
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.models import ShopifyOrder


CSV_COLUMNS = (
    "order_id",
    "customer_name",
    "email",
    "sku",
    "product_title",
    "quantity",
    "unit_price",
    "currency",
)


class OutputGenerationError(Exception):
    """Raised when automation outputs cannot be generated."""


def generate_order_outputs(order: ShopifyOrder, output_dir: str | Path) -> None:
    try:
        generate_fulfillment_csv(order, output_dir)
        generate_packing_slip_pdf(order, output_dir)
    except Exception as error:
        raise OutputGenerationError from error


def generate_fulfillment_csv(
    order: ShopifyOrder,
    output_dir: str | Path,
) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / f"order_{order.id}_fulfillment.csv"
    customer_name = f"{order.customer.first_name} {order.customer.last_name}"

    with output_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for item in order.line_items:
            writer.writerow(
                {
                    "order_id": order.id,
                    "customer_name": customer_name,
                    "email": str(order.email),
                    "sku": item.sku,
                    "product_title": item.title,
                    "quantity": item.quantity,
                    "unit_price": str(item.price),
                    "currency": order.currency,
                }
            )

    return output_path


def generate_packing_slip_pdf(
    order: ShopifyOrder,
    output_dir: str | Path,
) -> Path:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    output_path = destination / f"order_{order.id}_packing_slip.pdf"
    styles = getSampleStyleSheet()
    customer_name = f"{order.customer.first_name} {order.customer.last_name}"

    document = SimpleDocTemplate(
        str(output_path),
        pagesize=LETTER,
        rightMargin=0.65 * inch,
        leftMargin=0.65 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
        title=f"Packing Slip - Order {order.id}",
        author="Shopify Order Automation",
    )

    story = [
        Paragraph("PACKING SLIP", styles["Title"]),
        Spacer(1, 0.18 * inch),
        Paragraph(f"<b>Order ID:</b> {order.id}", styles["Normal"]),
        Paragraph(
            f"<b>Order date:</b> {escape(order.created_at.isoformat())}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Customer:</b> {escape(customer_name)}",
            styles["Normal"],
        ),
        Paragraph(
            f"<b>Email:</b> {escape(str(order.email))}",
            styles["Normal"],
        ),
        Spacer(1, 0.25 * inch),
    ]

    table_data = [["SKU", "Product", "Quantity", "Unit price"]]
    table_data.extend(
        [
            Paragraph(escape(item.sku), styles["BodyText"]),
            Paragraph(escape(item.title), styles["BodyText"]),
            str(item.quantity),
            f"{item.price} {order.currency}",
        ]
        for item in order.line_items
    )

    table = Table(
        table_data,
        colWidths=[1.35 * inch, 3.35 * inch, 0.75 * inch, 1.2 * inch],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF3")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#1F2937")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend(
        [
            table,
            Spacer(1, 0.25 * inch),
            Paragraph(
                f"<b>Order total:</b> {order.total_price} {order.currency}",
                styles["Heading3"],
            ),
        ]
    )

    document.build(story, onFirstPage=_add_page_number, onLaterPages=_add_page_number)
    return output_path


def _add_page_number(canvas, document) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(colors.HexColor("#64748B"))
    canvas.drawRightString(
        LETTER[0] - document.rightMargin,
        0.35 * inch,
        f"Page {document.page}",
    )
    canvas.restoreState()
