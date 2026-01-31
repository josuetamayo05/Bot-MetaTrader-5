from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.graphics.shapes import Drawing, String, Rect
from reportlab.graphics.charts.barcharts import VerticalBarChart

PIP_SIZE = 0.0001

# Paleta (oscura + cian)
BG = colors.HexColor("#0B0F14")
CARD = colors.HexColor("#111827")
TEXT = colors.HexColor("#E5E7EB")
MUTED = colors.HexColor("#9CA3AF")
ACCENT = colors.HexColor("#00E5FF")
BORDER = colors.HexColor("#1F2937")

def _on_page(canvas, doc, brand="JA CubanCode"):
    """Dibuja fondo, header y menú lateral (en cada página)."""
    w, h = A4

    # Fondo
    canvas.saveState()
    canvas.setFillColor(BG)
    canvas.rect(0, 0, w, h, fill=1, stroke=0)

    # Sidebar (menú)
    sidebar_w = 4.3 * cm
    canvas.setFillColor(colors.HexColor("#0F172A"))
    canvas.rect(0, 0, sidebar_w, h, fill=1, stroke=0)

    # Header (barra superior)
    header_h = 1.5 * cm
    canvas.setFillColor(colors.HexColor("#0B1220"))
    canvas.rect(sidebar_w, h - header_h, w - sidebar_w, header_h, fill=1, stroke=0)

    # Marca
    canvas.setFillColor(ACCENT)
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(0.7*cm, h - 1.2*cm, brand)

    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawString(0.7*cm, h - 1.65*cm, "FX Analytics • Sesiones • Alertas")

    # Menú
    canvas.setFillColor(TEXT)
    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawString(0.7*cm, h - 3.0*cm, "MENÚ")

    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 9)
    y = h - 3.6*cm
    for item in ["Resumen", "Sesiones", "Alertas", "Notas"]:
        canvas.drawString(0.9*cm, y, f"• {item}")
        y -= 0.55*cm

    # Footer
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(w - 0.7*cm, 0.6*cm, f"Página {doc.page}")
    canvas.restoreState()

def _bar_chart_session_ranges(ranges_dict):
    """
    ranges_dict: {"ASIA": 29.5, "LONDON": 39.6, "NY": 104.9}
    """
    labels = ["ASIA", "LONDON", "NY"]
    values = [float(ranges_dict.get(k, 0.0) or 0.0) for k in labels]

    d = Drawing(420, 170)
    # “Card” de fondo del gráfico
    d.add(Rect(0, 0, 420, 170, fillColor=CARD, strokeColor=BORDER, strokeWidth=1))

    d.add(String(16, 145, "Rangos por sesión (pips)", fillColor=TEXT, fontName="Helvetica-Bold", fontSize=10))
    d.add(String(16, 130, "Comparación rápida del día", fillColor=MUTED, fontName="Helvetica", fontSize=8.5))

    bc = VerticalBarChart()
    bc.x = 40
    bc.y = 25
    bc.height = 95
    bc.width = 350
    bc.data = [values]
    bc.strokeColor = colors.transparent
    bc.valueAxis.valueMin = 0
    bc.valueAxis.valueMax = max(10, int(max(values) * 1.25))
    bc.valueAxis.valueStep = max(5, int(bc.valueAxis.valueMax / 5))

    bc.categoryAxis.categoryNames = labels
    bc.categoryAxis.labels.boxAnchor = "n"
    bc.categoryAxis.labels.dx = 0
    bc.categoryAxis.labels.dy = -6
    bc.categoryAxis.labels.fontName = "Helvetica"
    bc.categoryAxis.labels.fontSize = 9
    bc.categoryAxis.labels.fillColor = MUTED

    bc.valueAxis.labels.fontName = "Helvetica"
    bc.valueAxis.labels.fontSize = 8
    bc.valueAxis.labels.fillColor = MUTED
    bc.valueAxis.strokeColor = BORDER

    bc.bars[0].fillColor = ACCENT
    bc.bars[0].strokeColor = colors.transparent

    d.add(bc)
    return d

def build_pro_pdf(date_utc, session_rows, alerts_rows, out_path, ny_p90_val=None, brand="JA CubanCode"):
    """
    session_rows: lista de tu session_summary:
      (session, open, close, high, low, range_pips, bars)
    alerts_rows: lista de alerts_once filtrada por el día:
      (first_ts_utc, alert_key, message)
    """
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=16, textColor=TEXT, spaceAfter=10)
    h2 = ParagraphStyle("h2", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=12, textColor=TEXT, spaceBefore=10, spaceAfter=6)
    p = ParagraphStyle("p", parent=styles["Normal"], fontName="Helvetica", fontSize=10, textColor=MUTED, leading=14)

    # Layout: dejamos espacio para sidebar y header
    sidebar_w = 4.3 * cm
    header_h = 1.5 * cm

    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=sidebar_w + 1.0*cm,
        rightMargin=1.0*cm,
        topMargin=header_h + 1.0*cm,
        bottomMargin=1.2*cm,
        title=f"EURUSD Reporte {date_utc}",
        author=brand
    )

    story = []
    story.append(Paragraph(f"EURUSD — Reporte Diario (UTC) — {date_utc}", title))
    story.append(Paragraph("Resumen visual del comportamiento por sesiones y alertas registradas.", p))
    story.append(Spacer(1, 10))

    # Construir diccionario de rangos para gráfico
    ranges = {}
    for (sess, o, c, hi, lo, rp, bars) in session_rows:
        if rp is not None:
            ranges[sess] = float(rp)

    story.append(_bar_chart_session_ranges(ranges))
    story.append(Spacer(1, 14))

    # Card resumen (tabla compacta)
    story.append(Paragraph("Sesiones", h2))

    table_data = [["Sesión", "Rango (pips)", "Open", "Close", "High", "Low", "Velas"]]
    for (sess, o, cl, hi, lo, rp, bars) in session_rows:
        if rp is None:
            table_data.append([sess, "—", "—", "—", "—", "—", str(bars or 0)])
        else:
            table_data.append([
                sess,
                f"{float(rp):.1f}",
                f"{float(o):.5f}",
                f"{float(cl):.5f}",
                f"{float(hi):.5f}",
                f"{float(lo):.5f}",
                str(int(bars))
            ])

    t = Table(table_data, colWidths=[52, 70, 60, 60, 60, 60, 45])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#0B1220")),
        ("TEXTCOLOR", (0,0), (-1,0), TEXT),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,0), 9),
        ("ALIGN", (1,1), (-1,-1), "CENTER"),
        ("TEXTCOLOR", (0,1), (-1,-1), TEXT),
        ("FONTNAME", (0,1), (-1,-1), "Helvetica"),
        ("FONTSIZE", (0,1), (-1,-1), 8.8),
        ("BACKGROUND", (0,1), (-1,-1), CARD),
        ("GRID", (0,0), (-1,-1), 0.5, BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(t)

    if ny_p90_val is not None:
        story.append(Spacer(1, 10))
        story.append(Paragraph(f"Referencia: NY p90 histórico ≈ {ny_p90_val:.1f} pips (últimos días).", p))

    story.append(Spacer(1, 18))
    story.append(Paragraph("Alertas registradas", h2))

    if not alerts_rows:
        story.append(Paragraph("No hubo alertas registradas para este día.", p))
    else:
        # Lista limpia (compacta)
        for ts, key, _msg in alerts_rows[:25]:
            story.append(Paragraph(f"• {ts} — {key}", p))

        if len(alerts_rows) > 25:
            story.append(Paragraph(f"(Se muestran 25 de {len(alerts_rows)} alertas.)", p))

    doc.build(story, onFirstPage=lambda c, d: _on_page(c, d, brand=brand),
                    onLaterPages=lambda c, d: _on_page(c, d, brand=brand))