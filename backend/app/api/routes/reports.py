"""Reports and exports: CSV, Excel and PDF files built from the database.

  GET /reports                 the eight reports, their parameters and the formats (for building a menu)
  GET /reports/{id}?format=    download one report as csv, xlsx or pdf

    /api/reports/prices?symbol=TCS&start=2025-01-01&format=xlsx
    /api/reports/indicators?symbol=RELIANCE&names=rsi_14,ema_20&format=csv
    /api/reports/backtest?run_id=bt_...&format=pdf
    /api/reports/comparison?symbols=RELIANCE,TCS,NIFTY_50&format=pdf

Reading is open like every other read endpoint; building a file costs CPU, so downloads share the heavy-endpoint rate limit. A CSV carries one table
(`table=` chooses which; `table=all` returns every table in a ZIP). Excel and PDF carry the whole report. Details: docs/REPORTS.md.
"""
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, Query, Response
from pymongo.database import Database

from app.api.deps import get_database, heavy_rate_limit
from app.api.errors import ApiError
from app.api.serialize import SafeAPIRouter
from app.reports import BUILDERS, CATALOG, FORMATS, ReportError, generate
from app.reports.model import MAX_ROWS, MAX_ROWS_EXCEL

router = SafeAPIRouter(tags=["Reports"])


@router.get("/reports", summary="The available reports")
def list_reports():
    return {"reports": CATALOG, "formats": list(FORMATS), "limits": {"max_rows_per_table": MAX_ROWS, "max_rows_excel": MAX_ROWS_EXCEL},
            "notes": ["csv carries one table (choose with `table`, or table=all for a ZIP of every table)",
                      "xlsx and pdf carry the whole report; a pdf shows the newest 40 rows of a long table and says so"]}


@router.get("/reports/{report_id}", dependencies=[Depends(heavy_rate_limit)], summary="Download a report",
            responses={200: {"content": {"text/csv": {}, "application/pdf": {}, "application/zip": {},
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {}}}})
def download_report(
    report_id: str,
    fmt: Literal["csv", "xlsx", "pdf"] = Query("csv", alias="format", description="csv, xlsx (Excel) or pdf"),
    table: Optional[str] = Query(None, pattern=r"^[a-z_]{1,30}$", description="csv only: which table (default: the first); all = a ZIP of every table"),
    symbol: Optional[str] = Query(None, max_length=30),
    symbols: Optional[str] = Query(None, max_length=400, description="comma-separated"),
    exchange: Literal["NSE", "BSE"] = "NSE",
    interval: Optional[Literal["1d", "1m", "5m", "15m", "30m", "1h"]] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    basis: Optional[Literal["adjusted", "raw"]] = None,
    source: Optional[str] = Query(None, pattern=r"^[a-z0-9_]{1,40}$"),
    names: Optional[str] = Query(None, max_length=600, description="indicators report: comma-separated indicator names"),
    run_id: Optional[str] = Query(None, max_length=80),
    technology: Optional[Literal["pandas", "mongodb_aggregation", "pyspark", "ingestion"]] = None,
    operation: Optional[str] = Query(None, max_length=80),
    batch: Optional[str] = Query(None, max_length=40),
    db: Database = Depends(get_database),
):
    if report_id not in BUILDERS:
        raise ApiError(404, "unknown_report", f"Unknown report {report_id!r}.", {"available": list(BUILDERS)})
    params = {"symbol": symbol, "symbols": symbols, "exchange": exchange, "interval": interval, "start": start, "end": end, "basis": basis,
              "source": source, "names": names, "run_id": run_id, "technology": technology, "operation": operation, "batch": batch}
    for key in ("start", "end"):                                   # naive datetimes are UTC, as everywhere in the API
        if params[key] is not None and params[key].tzinfo is None:
            params[key] = params[key].replace(tzinfo=timezone.utc)
    try:
        content, filename, media, rows = generate(db, report_id, fmt, params, table)
    except ReportError as exc:
        raise ApiError(exc.status, exc.code, exc.message)
    return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                                        "X-Report-Rows": str(rows), "X-Report-Filename": filename})
