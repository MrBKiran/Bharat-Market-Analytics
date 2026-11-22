"""CSV, Excel and PDF reports (Phase 13).

    from app.reports import generate
    content, filename, media_type, rows = generate(db, "prices", "xlsx", {"symbol": "TCS"})

`builders` turn database content into a format-independent `Report`; `csv_out`, `excel_out` and `pdf_out` render it. See docs/REPORTS.md.
"""
from typing import Optional

from pymongo.database import Database

from app.reports.builders import BUILDERS, CATALOG, build_report
from app.reports.csv_out import render_csv
from app.reports.excel_out import render_excel
from app.reports.model import Report, ReportError, safe_filename
from app.reports.pdf_out import render_pdf

FORMATS = ("csv", "xlsx", "pdf")


def render(report: Report, fmt: str, table: Optional[str] = None) -> tuple[bytes, str, str]:
    """(bytes, file extension, media type) of `report` in `fmt`."""
    if fmt == "csv":
        return render_csv(report, table)
    if fmt == "xlsx":
        return render_excel(report)
    if fmt == "pdf":
        return render_pdf(report)
    raise ReportError(422, "invalid_format", f"format must be one of {', '.join(FORMATS)}.")


def generate(db: Database, report_id: str, fmt: str, params: dict, table: Optional[str] = None) -> tuple[bytes, str, str, int]:
    """Build and render in one step. Returns (bytes, download file name, media type, number of data rows)."""
    if fmt not in FORMATS:
        raise ReportError(422, "invalid_format", f"format must be one of {', '.join(FORMATS)}.")
    report = build_report(db, report_id, params)
    content, extension, media = render(report, fmt, table)
    return content, safe_filename(report.filename_stem, extension), media, report.row_count


__all__ = ["BUILDERS", "CATALOG", "FORMATS", "Report", "ReportError", "build_report", "generate", "render"]
