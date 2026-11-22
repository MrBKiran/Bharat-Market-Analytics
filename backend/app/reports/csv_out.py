"""CSV export: plain data, one table per file (or every table in a ZIP).

Rules: UTF-8 with a byte-order mark (so Excel opens it correctly), CRLF line ends, ISO dates, numbers without symbols or digit grouping (a
spreadsheet or a script can use them as they are), empty cells for missing values, and text that could be executed as a spreadsheet formula
is defused (see `neutralise`). Units are in the column headers, e.g. "Close (Rs)" or "Return (%)".
"""
import csv
import io
import zipfile
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.reports.model import Report, Table, ReportError, neutralise, safe_filename, tidy


def csv_cell(value: Any, kind: str = "text") -> Any:
    value = tidy(value, kind)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "" if value != value or value in (float("inf"), float("-inf")) else repr(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return neutralise(str(value) if not isinstance(value, (int, str)) else value)


def table_to_csv(table: Table) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([neutralise(c.header) for c in table.columns])
    for row in table.rows:
        writer.writerow([csv_cell(v, c.kind) for v, c in zip(row, table.columns)])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def render_csv(report: Report, table: Optional[str] = None) -> tuple[bytes, str, str]:
    """(file bytes, file extension, media type). `table` picks one table; "all" returns a ZIP of every table."""
    if table == "all":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for t in report.tables:
                archive.writestr(safe_filename(f"{report.filename_stem}_{t.name}", "csv"), table_to_csv(t))
        return buffer.getvalue(), "zip", "application/zip"
    try:
        chosen = report.table(table)
    except KeyError:
        names = ", ".join(t.name for t in report.tables)
        raise ReportError(422, "unknown_table", f"This report has no table {table!r}. Choose one of: {names}, or all.")
    return table_to_csv(chosen), "csv", "text/csv; charset=utf-8"
