"""PDF export: a readable report with the key figures, warnings, charts and tables, on A4.

Uses reportlab's built-in Helvetica, which has no rupee sign, so rupees are written "Rs 12,34,567.00" (Indian digit grouping). Every text is
escaped before it reaches the layout engine, so a company name such as "A <b>&</b> B" cannot break or restyle the page. A PDF shows the
newest 40 rows of a long table (and says so); the CSV and Excel exports carry every row.
"""
import io
from datetime import date, datetime
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table as PdfTable, TableStyle

from app.reports.model import Chart, Report, Table, format_indian, format_value

GREEN = colors.HexColor("#0b6b4f")
INK = colors.HexColor("#13202c")
MUTED = colors.HexColor("#566676")
RULE = colors.HexColor("#d9e0e7")
BAND = colors.HexColor("#f4f6f8")
WARN_BG = colors.HexColor("#fff4dc")
PALETTE = [colors.HexColor(c) for c in ("#0b6b4f", "#1a56a8", "#b45309", "#7c3aed", "#be185d", "#0e7490")]
RIGHT_KINDS = {"int", "float", "inr", "pct", "percent", "ratio"}
DISCLAIMER = ("For information only: not investment advice. Prices are split-adjusted, not dividend-adjusted. Past performance does not "
              "predict future results. Computed from the data held in this project's database; check against official sources before relying on it.")


def pdf_text(value: Any) -> str:
    """Escape for the layout engine and drop what Helvetica cannot draw (the rupee sign becomes 'Rs')."""
    text = str(value).replace("₹", "Rs ").replace("→", "->").replace("≥", ">=").replace("≤", "<=").replace("−", "-")
    return escape(text.encode("cp1252", "replace").decode("cp1252"))


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("t", parent=base["Title"], fontName="Helvetica-Bold", fontSize=19, leading=23, alignment=0, textColor=INK, spaceAfter=2),
        "subtitle": ParagraphStyle("s", parent=base["Normal"], fontSize=10, textColor=MUTED, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12, textColor=GREEN, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("b", parent=base["Normal"], fontSize=8.5, leading=11, textColor=INK),
        "small": ParagraphStyle("sm", parent=base["Normal"], fontSize=7.5, leading=9.5, textColor=MUTED),
        "cell": ParagraphStyle("c", parent=base["Normal"], fontSize=7.5, leading=9, textColor=INK),
        "cell_r": ParagraphStyle("cr", parent=base["Normal"], fontSize=7.5, leading=9, textColor=INK, alignment=2),
        "head": ParagraphStyle("hd", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.white),
        "head_r": ParagraphStyle("hdr", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=colors.white, alignment=2),
        "warn": ParagraphStyle("w", parent=base["Normal"], fontSize=8.5, leading=11, textColor=colors.HexColor("#6b4200")),
    }


class _NumberedCanvas(pdf_canvas.Canvas):
    """Adds "Page 2 of 5" and the report name to every page (the total is only known at the end, hence two passes)."""

    footer_text = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            width, _ = self._pagesize
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.drawString(15 * mm, 9 * mm, pdf_text(self.footer_text)[:120].replace("&amp;", "&"))
            self.drawRightString(width - 15 * mm, 9 * mm, f"Page {self._pageNumber} of {total}")
            self.setStrokeColor(RULE)
            self.line(15 * mm, 12.5 * mm, width - 15 * mm, 12.5 * mm)
            super().showPage()
        super().save()


# ---------------------------------------------------------------------------------------------------- charts
def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    if high == low:
        return [low]
    return [low + (high - low) * i / (count - 1) for i in range(count)]


def _tick_label(value: float, kind: str) -> str:
    magnitude = abs(value)
    if kind == "inr":
        if magnitude >= 1e7:
            return f"Rs {value / 1e7:.1f} Cr"
        if magnitude >= 1e5:
            return f"Rs {value / 1e5:.1f} L"
        return "Rs " + format_indian(value, 0)
    return format_indian(value, 0 if magnitude >= 100 else 2)


def line_chart(chart: Chart, width: float, height: float) -> Drawing:
    """A simple date-axis line chart drawn with plain shapes (reportlab's own line plot has no usable date axis)."""
    d = Drawing(width, height)
    series = [s for s in chart.series if s.points]
    if not series:
        d.add(String(width / 2, height / 2, "No data", textAnchor="middle", fontName="Helvetica", fontSize=9, fillColor=MUTED))
        return d
    left, right, top, bottom = 58, 10, 26, 22
    plot_w, plot_h = width - left - right, height - top - bottom

    def to_ordinal(x) -> int:
        return (x.date() if isinstance(x, datetime) else x).toordinal() if isinstance(x, (date, datetime)) else int(x)

    xs = [to_ordinal(p[0]) for s in series for p in s.points]
    ys = [p[1] for s in series for p in s.points if p[1] == p[1]]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad = (y1 - y0) * 0.06 or abs(y1) * 0.05 or 1
    y0, y1 = y0 - pad, y1 + pad

    def px(x):
        return left + (to_ordinal(x) - x0) / ((x1 - x0) or 1) * plot_w

    def py(y):
        return bottom + (y - y0) / ((y1 - y0) or 1) * plot_h

    d.add(Rect(left, bottom, plot_w, plot_h, strokeColor=RULE, fillColor=None, strokeWidth=0.6))
    for tick in _nice_ticks(y0 + pad, y1 - pad, 5):
        d.add(Line(left, py(tick), left + plot_w, py(tick), strokeColor=RULE, strokeWidth=0.3))
        d.add(String(left - 4, py(tick) - 2.5, _tick_label(tick, chart.y_kind), textAnchor="end", fontName="Helvetica", fontSize=6.5, fillColor=MUTED))
    for i in range(5):
        ordinal = x0 + (x1 - x0) * i / 4
        when = date.fromordinal(int(ordinal))
        label = when.strftime("%b %Y") if (x1 - x0) > 150 else when.strftime("%d %b")
        d.add(String(left + plot_w * i / 4, bottom - 11, label, textAnchor="middle", fontName="Helvetica", fontSize=6.5, fillColor=MUTED))
    for index, s in enumerate(series):
        step = max(1, len(s.points) // 500)
        points = [c for p in s.points[::step] if p[1] == p[1] for c in (px(p[0]), py(p[1]))]
        if len(points) >= 4:
            d.add(PolyLine(points, strokeColor=PALETTE[index % len(PALETTE)], strokeWidth=1.1))
    legend_x = left
    for index, s in enumerate(series):
        d.add(Rect(legend_x, height - 14, 7, 7, fillColor=PALETTE[index % len(PALETTE)], strokeColor=None))
        d.add(String(legend_x + 10, height - 13, pdf_text(s.label).replace("&amp;", "&")[:28], fontName="Helvetica", fontSize=7.5, fillColor=INK))
        legend_x += 14 + 4.4 * min(len(s.label), 28)
    return d


# ---------------------------------------------------------------------------------------------------- tables
CHAR = 4.6          # points per character at the table font size: Helvetica 7.5 pt, allowing for wide capitals


def _column_widths(columns, rows, total: float) -> list[float]:
    """Each column gets at least what its widest cell and its longest header WORD need (so "RELIANCE" never splits), then the spare
    width is shared out in proportion; if the minimums do not fit, everything is scaled down evenly."""
    minimum, wanted = [], []
    for index, column in enumerate(columns):
        cells = [len(format_value(r[index], column.kind)) for r in rows[:60]]
        word = max((len(w) for w in column.header.split()), default=4)
        need = max(max(cells, default=4), word)
        minimum.append(min(need, 34) * CHAR + 9)
        wanted.append(min(max(max(cells, default=4), len(column.header) * 0.8), 34) * CHAR + 9)
    if sum(minimum) >= total:
        return [m * total / sum(minimum) for m in minimum]
    spare = total - sum(minimum)
    extra = [max(w - m, 0.5) for w, m in zip(wanted, minimum)]
    return [m + spare * e / sum(extra) for m, e in zip(minimum, extra)]


def _pdf_table(table: Table, width: float, styles) -> list:
    keys = table.pdf_columns or [c.key for c in table.columns]
    picked = [(i, c) for i, c in enumerate(table.columns) if c.key in keys]
    rows = table.rows
    shown = rows[-table.pdf_rows:] if table.pdf_tail else rows[:table.pdf_rows]
    story: list = [Paragraph(pdf_text(table.title), styles["h2"])]
    if not rows:
        story.append(Paragraph("No rows.", styles["small"]))
        return story
    head = [Paragraph(pdf_text(c.header), styles["head_r" if c.kind in RIGHT_KINDS else "head"]) for _, c in picked]
    body = [[Paragraph(pdf_text(format_value(r[i], c.kind)), styles["cell_r" if c.kind in RIGHT_KINDS else "cell"]) for i, c in picked] for r in shown]
    sub_rows = [[r[i] for i, _ in picked] for r in shown]
    grid = PdfTable([head] + body, colWidths=_column_widths([c for _, c in picked], sub_rows, width), repeatRows=1)
    grid.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREEN), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]), ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(grid)
    notes = []
    if len(rows) > len(shown):
        notes.append(f"Showing the {'newest' if table.pdf_tail else 'first'} {len(shown):,} of {len(rows):,} rows. The CSV and Excel exports contain every row.")
    if len(picked) < len(table.columns):
        notes.append(f"Showing {len(picked)} of {len(table.columns)} columns; the CSV and Excel exports contain all of them.")
    if table.note:
        notes.append(table.note)
    for text in notes:
        story.append(Paragraph(pdf_text(text), styles["small"]))
    return story


# ---------------------------------------------------------------------------------------------------- report
def render_pdf(report: Report) -> tuple[bytes, str, str]:
    page = landscape(A4) if report.landscape else A4
    width = page[0] - 30 * mm
    styles = _styles()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=page, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=17 * mm,
                            title=report.title[:200], author="BharatMarket Analytics", subject=report.subtitle[:200])
    story: list = [Paragraph(pdf_text(report.title), styles["title"]), Paragraph(pdf_text(report.subtitle), styles["subtitle"])]

    if report.meta:
        rows = [[Paragraph(f"<b>{pdf_text(k)}</b>", styles["body"]), Paragraph(pdf_text(v), styles["body"])] for k, v in report.meta]
        rows.append([Paragraph("<b>Generated</b>", styles["body"]), Paragraph(pdf_text(report.generated_at.strftime("%d %b %Y %H:%M") + " UTC"), styles["body"])])
        meta = PdfTable(rows, colWidths=[34 * mm, width - 34 * mm])
        meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                                  ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE)]))
        story += [meta, Spacer(1, 6)]

    if report.warnings:
        box = PdfTable([[Paragraph("<b>Read before using these numbers</b>", styles["warn"])]] + [[Paragraph(pdf_text("- " + w), styles["warn"])] for w in report.warnings], colWidths=[width])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e6c46a")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
        story += [box, Spacer(1, 6)]

    if report.summary:
        story.append(Paragraph("Key figures", styles["h2"]))
        pairs = [(Paragraph(pdf_text(label), styles["small"]), Paragraph(f"<b>{pdf_text(format_value(value, kind))}</b>", styles["body"])) for label, value, kind in report.summary]
        per_row = 3 if not report.landscape else 4
        grid_rows = []
        for i in range(0, len(pairs), per_row):
            chunk = pairs[i:i + per_row]
            cells = [[label, value] for label, value in chunk]
            grid_rows.append([c for pair in cells for c in (pair[0], pair[1])] + [""] * (2 * per_row - 2 * len(chunk)))
        unit = width / per_row
        figures = PdfTable(grid_rows, colWidths=[unit * 0.44, unit * 0.56] * per_row)
        figures.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, RULE),
                                     ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("BACKGROUND", (0, 0), (-1, -1), BAND)]))
        story.append(figures)

    for chart in report.charts:
        story += [Paragraph(pdf_text(chart.title), styles["h2"]), line_chart(chart, width, 62 * mm)]

    for table in report.tables:
        if table.pdf_rows > 0:                          # pdf_rows=0 marks data that belongs in the spreadsheet only (a full series)
            story += _pdf_table(table, width, styles)

    for text in report.notes:
        story += [Spacer(1, 4), Paragraph(pdf_text(text), styles["small"])]
    story += [Spacer(1, 8), Paragraph(pdf_text(DISCLAIMER), styles["small"])]

    class Canvas(_NumberedCanvas):
        footer_text = f"BharatMarket Analytics - {report.title}"

    doc.build(story, canvasmaker=Canvas)
    return buffer.getvalue(), "pdf", "application/pdf"
