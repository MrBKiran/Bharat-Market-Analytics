"""Excel export (.xlsx): a Summary sheet, one sheet per table, and a Charts sheet with native Excel charts.

Numbers stay numbers (sortable, summable) and only their DISPLAY is formatted: rupees with Indian digit grouping (12,34,567.00), dates as
31-Dec-2026, percentages as 12.50%. Header rows are frozen and filterable. Text that Excel could run as a formula is defused (see
`neutralise`), and every text cell is stored as a string.
"""
import io
from datetime import date, datetime, timezone
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.reports.model import MAX_ROWS_EXCEL, Chart, Column, Report, ReportError, Table, neutralise, sheet_name, tidy

INR_FORMAT = '[>=10000000]"₹ "##\\,##\\,##\\,##0.00;[>=100000]"₹ "##\\,##\\,##0.00;"₹ "#,##0.00'
FORMATS = {"inr": INR_FORMAT, "pct": '+0.00"%";-0.00"%";0.00"%"', "percent": '0.00"%"', "ratio": "0.00", "float": "#,##0.00", "int": "#,##0",
           "date": "DD-MMM-YYYY", "datetime": "DD-MMM-YYYY HH:MM"}
HEADER_FILL = PatternFill("solid", fgColor="0B6B4F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
WARN_FILL = PatternFill("solid", fgColor="FFF4DC")


def excel_value(value: Any, kind: str) -> Any:
    """A value in a form openpyxl accepts: no timezones, no NaN, text defused."""
    if value is None:
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    value = tidy(value, kind)
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        return value.date() if kind == "date" else value
    if isinstance(value, (bool, int, float, date)):
        return value
    return neutralise(str(value))


def _write_table(ws, table: Table) -> None:
    ws.append([neutralise(c.header) for c in table.columns])
    for cell in ws[1]:
        cell.font, cell.fill = HEADER_FONT, HEADER_FILL
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for row in table.rows:
        ws.append([excel_value(v, c.kind) for v, c in zip(row, table.columns)])
    for index, column in enumerate(table.columns, start=1):
        fmt = FORMATS.get(column.kind)
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=index, max_col=index):
                cell.number_format = fmt
        sample = [len(str(r[index - 1])) for r in table.rows[:200] if r[index - 1] is not None]
        wide = column.width or min(max([len(column.header)] + sample, default=10) + 3, 42)
        ws.column_dimensions[get_column_letter(index)].width = max(wide, 12 if column.kind in ("inr", "date", "datetime") else 8)
    ws.freeze_panes = "A2"
    if table.rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(table.columns))}{len(table.rows) + 1}"


def _write_summary(ws, report: Report) -> None:
    ws["A1"] = neutralise(report.title)                       # titles contain instrument names, which anyone can set: defuse them too
    ws["A1"].font = Font(bold=True, size=15)
    ws["A2"] = neutralise(report.subtitle)
    ws["A3"] = f"Generated {report.generated_at.strftime('%d %b %Y %H:%M')} UTC by BharatMarket Analytics"
    row = 5
    for label, text in report.meta:
        ws.cell(row=row, column=1, value=neutralise(label)).font = Font(bold=True)
        ws.cell(row=row, column=2, value=neutralise(text))
        row += 1
    if report.summary:
        row += 1
        ws.cell(row=row, column=1, value="Key figures").font = Font(bold=True, size=12)
        row += 1
        for label, value, kind in report.summary:
            ws.cell(row=row, column=1, value=neutralise(label))
            cell = ws.cell(row=row, column=2, value=excel_value(value, kind))
            if FORMATS.get(kind):
                cell.number_format = FORMATS[kind]
            cell.alignment = Alignment(horizontal="left")
            row += 1
    if report.warnings:
        row += 1
        ws.cell(row=row, column=1, value="Read before using these numbers").font = Font(bold=True, size=12)
        row += 1
        for text in report.warnings:
            cell = ws.cell(row=row, column=1, value=neutralise(text))
            cell.fill, cell.alignment = WARN_FILL, Alignment(wrap_text=True, vertical="top")
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            ws.row_dimensions[row].height = 15 * (1 + len(text) // 95)
            row += 1
    if report.tables:
        row += 1
        ws.cell(row=row, column=1, value="Sheets in this file").font = Font(bold=True, size=12)
        row += 1
        for t in report.tables:
            ws.cell(row=row, column=1, value=neutralise(sheet_name(t.name)))
            ws.cell(row=row, column=2, value=neutralise(f"{t.title}: {len(t.rows):,} rows" + (f". {t.note}" if t.note else "")))
            row += 1
    for text in report.notes:
        row += 1
        cell = ws.cell(row=row, column=1, value=neutralise(text))
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ws.row_dimensions[row].height = 15 * (1 + len(text) // 95)
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 46
    ws.column_dimensions["C"].width = 18
    ws.column_dimensions["D"].width = 18


def _write_charts(ws, charts: list[Chart]) -> None:
    """Chart data goes in columns on the left, the native chart next to it. All series of one chart share the first series' x values."""
    column, anchor_row = 1, 1
    for chart in charts:
        first = chart.series[0].points
        if not first or any(len(s.points) != len(first) for s in chart.series):
            continue
        ws.cell(row=1, column=column, value="Date").font = Font(bold=True)
        for i, (x, _) in enumerate(first, start=2):
            cell = ws.cell(row=i, column=column, value=excel_value(x, "date"))
            cell.number_format = FORMATS["date"]
        for j, s in enumerate(chart.series, start=1):
            ws.cell(row=1, column=column + j, value=neutralise(s.label)).font = Font(bold=True)
            for i, (_, y) in enumerate(s.points, start=2):
                ws.cell(row=i, column=column + j, value=excel_value(y, "float"))
        line = LineChart()
        line.title, line.height, line.width = chart.title, 9, 20
        data = Reference(ws, min_col=column + 1, max_col=column + len(chart.series), min_row=1, max_row=len(first) + 1)
        line.add_data(data, titles_from_data=True)
        line.set_categories(Reference(ws, min_col=column, min_row=2, max_row=len(first) + 1))
        line.x_axis.number_format, line.x_axis.majorTimeUnit = "MMM-YY", "months"
        line.x_axis.delete, line.y_axis.delete = False, False
        for s in line.series:
            s.smooth, s.graphicalProperties.line.width = False, 19000
        ws.column_dimensions[get_column_letter(column)].width = 13
        ws.add_chart(line, f"{get_column_letter(column + len(chart.series) + 2)}{anchor_row}")
        column += len(chart.series) + 12                       # the next chart's data starts to the right of this chart


def render_excel(report: Report) -> tuple[bytes, str, str]:
    for table in report.tables:
        if len(table.rows) > MAX_ROWS_EXCEL:
            raise ReportError(422, "too_many_rows_for_excel", f"{table.title} has {len(table.rows):,} rows, more than the {MAX_ROWS_EXCEL:,} an Excel file can reasonably hold here. "
                              "Choose a shorter date range or a coarser timeframe, or download the CSV (up to 250,000 rows).")
    wb = Workbook()
    _write_summary(wb.active, report)
    wb.active.title = "Summary"
    for table in report.tables:
        _write_table(wb.create_sheet(sheet_name(table.name)), table)
    if report.charts:
        _write_charts(wb.create_sheet("Charts"), report.charts)
    wb.properties.title, wb.properties.creator = report.title[:250], "BharatMarket Analytics"
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
