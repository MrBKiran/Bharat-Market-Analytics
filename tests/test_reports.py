"""Phase 13 tests: CSV, Excel and PDF reports.

Formatting and safety are tested on small hand-made reports; every report type is then built from a seeded throwaway database (real analytics, a
real stored backtest) and downloaded through the real HTTP endpoint in every format.
"""
import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest
from openpyxl import load_workbook

from app.reports import FORMATS, ReportError, build_report, generate, render
from app.reports import builders
from app.reports.csv_out import csv_cell, render_csv
from app.reports.excel_out import render_excel
from app.reports.model import Chart, Column, Report, Series, Table, format_indian, format_value, group_indian, neutralise, safe_filename, tidy
from app.reports.pdf_out import pdf_text, render_pdf

from test_api import YF_SIX_GOOD, db, make_client, put_daily, wavy_daily  # noqa: F401  (db and make_client are pytest fixtures)
from test_contract import world  # noqa: F401  (a database filled by the real code paths, plus a client)

UTC = timezone.utc
NOW = datetime(2026, 11, 22, 9, 0, tzinfo=UTC)


def tiny_report(rows=None, name="Tata <b>Consultancy</b> & Sons") -> Report:
    rows = rows if rows is not None else [[date(2026, 1, 5), 1234567.891, 12.3456, True, name], [date(2026, 1, 6), None, float("nan"), False, "=HYPERLINK(\"http://evil\")"]]
    cols = [Column("d", "Date", "date"), Column("amt", "Amount (Rs)", "inr"), Column("pct", "Change (%)", "pct"), Column("flag", "Flag", "bool"), Column("name", "Name", "text")]
    return Report(id="t", title="Test report", subtitle="A <b>subtitle</b>", generated_at=NOW, filename_stem="Test report/../x",
                  meta=[("Symbol", name)], summary=[("Total", 12345678.9, "inr"), ("Share", 24.05, "percent")], warnings=["Be careful & read this"],
                  tables=[Table("main", "Main table", cols, rows), Table("second", "Second", [Column("a", "A", "int")], [[1], [2]])],
                  charts=[Chart("Close", [Series("Close", [(date(2026, 1, 5) + timedelta(days=i), 100 + i) for i in range(30)])], "inr")])


# ------------------------------------------------------------------------------------------------ numbers
@pytest.mark.parametrize("digits,expected", [("1", "1"), ("999", "999"), ("1000", "1,000"), ("12345", "12,345"), ("123456", "1,23,456"), ("1234567", "12,34,567"), ("123456789", "12,34,56,789")])
def test_indian_digit_grouping(digits, expected):
    assert group_indian(digits) == expected


def test_value_formatting_for_the_pdf():
    assert format_indian(1234567.891) == "12,34,567.89" and format_indian(-1234.5, 0) in ("-1,234", "-1,235")
    assert format_value(1234567.8, "inr") == "Rs 12,34,567.80"                     # the PDF font has no rupee sign
    assert format_value(-3.14159, "pct") == "-3.14%" and format_value(3.14159, "pct") == "+3.14%" and format_value(24.05, "percent") == "24.05%"
    assert format_value(None, "inr") == "-" and format_value(float("nan"), "float") == "-"
    assert format_value(True, "bool") == "Yes" and format_value(date(2026, 1, 5), "date") == "05 Jan 2026"


def test_floating_point_noise_is_removed_before_writing_files():
    assert tidy(3138.10009765625, "inr") == 3138.1                                 # a 32-bit float from the database
    assert tidy(25800.599609375, "inr") == 25800.6
    assert tidy(7.31912345678, "inr") == 7.319123                                  # small prices keep significant digits
    assert tidy(12.3456789123, "pct") == 12.345679
    assert tidy(3064001.9, "inr") == 3064001.9                                     # rupee amounts keep paise
    nan = tidy(float("nan"), "float")
    assert nan != nan and tidy(True, "float") is True and tidy("x", "inr") == "x" and tidy(5, "int") == 5


# ---------------------------------------------------------------------------------------------- text safety
@pytest.mark.parametrize("text", ["=1+1", "+SUM(A1)", "-2+3", "@cmd", "\tx", "\rx", '=HYPERLINK("http://evil")'])
def test_spreadsheet_formulas_are_defused(text):
    assert neutralise(text) == "'" + text


def test_ordinary_text_and_numbers_pass_through_unchanged():
    for value in ("TCS", "Tata Consultancy", "1+1 is two", "", 5, -5, 3.5, None, True, date(2026, 1, 5)):
        assert neutralise(value) == value


def test_download_names_cannot_contain_path_or_header_characters():
    name = safe_filename("../../Evil name/\r\nX-Header: y?.exe", "csv")
    assert "/" not in name and "\\" not in name and "\r" not in name and "\n" not in name and name.endswith(".csv") and " " not in name and ":" not in name
    assert safe_filename("", "pdf") == "report.pdf" and safe_filename("é€", "xlsx") == "report.xlsx"
    assert len(safe_filename("x" * 500, "csv")) <= 125


# ------------------------------------------------------------------------------------------------------ CSV
def test_csv_has_a_bom_crlf_iso_dates_and_defused_text():
    raw, ext, media = render_csv(tiny_report())
    assert raw.startswith(b"\xef\xbb\xbf") and ext == "csv" and media.startswith("text/csv")
    lines = raw.decode("utf-8-sig").split("\r\n")
    assert lines[0] == "Date,Amount (Rs),Change (%),Flag,Name"
    assert lines[1].startswith("2026-01-05,1234567.89,12.3456,true,") and "Tata <b>Consultancy</b> & Sons" in lines[1]
    assert lines[2] == "2026-01-06,,,false,\"'=HYPERLINK(\"\"http://evil\"\")\""        # blanks for missing values and NaN, formula defused


def test_csv_cells_by_type():
    assert csv_cell(None) == "" and csv_cell(float("inf")) == "" and csv_cell(True) == "true" and csv_cell(5) == 5
    assert csv_cell(datetime(2026, 1, 5, 3, 45, tzinfo=UTC)) == "2026-01-05T03:45:00Z" and csv_cell(date(2026, 1, 5)) == "2026-01-05"


def test_csv_table_choice_and_zip():
    report = tiny_report()
    assert render_csv(report, "second")[0].decode("utf-8-sig") == "A\r\n1\r\n2\r\n"
    with pytest.raises(ReportError) as err:
        render_csv(report, "nope")
    assert err.value.status == 422 and err.value.code == "unknown_table" and "main" in err.value.message
    data, ext, media = render_csv(report, "all")
    assert ext == "zip" and media == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["Test_report_._x_main.csv", "Test_report_._x_second.csv"]
        assert not any(".." in n or "/" in n for n in z.namelist())


# ---------------------------------------------------------------------------------------------------- Excel
def test_excel_workbook_structure_formats_and_safety():
    data, ext, media = render_excel(tiny_report())
    assert ext == "xlsx" and "spreadsheetml" in media
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Summary", "main", "second", "Charts"]
    ws = wb["main"]
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:E3" and ws["A1"].font.bold
    assert [c.value for c in ws[1]] == ["Date", "Amount (Rs)", "Change (%)", "Flag", "Name"]
    assert isinstance(ws["A2"].value, datetime) and ws["A2"].number_format == "DD-MMM-YYYY"
    assert isinstance(ws["B2"].value, float) and "₹" in ws["B2"].number_format and "##\\,##\\,##" in ws["B2"].number_format      # Indian grouping format
    assert ws["B2"].value == 1234567.89 and ws["C2"].number_format.startswith("+0.00")
    assert ws["B3"].value is None and ws["C3"].value is None                       # missing and NaN become empty cells
    cell = ws["E3"]
    assert cell.data_type == "s" and cell.value == "'=HYPERLINK(\"http://evil\")"       # text, never a formula
    assert ws["E2"].value == "Tata <b>Consultancy</b> & Sons"
    summary = wb["Summary"]
    assert summary["A1"].value == "Test report" and len(wb["Charts"]._charts) == 1
    texts = [str(c.value) for row in summary.iter_rows() for c in row if c.value is not None]
    assert any("Be careful" in t for t in texts) and any("main" == t for t in texts)


def test_excel_never_turns_text_into_formulas_anywhere():
    report = tiny_report(name="=cmd|' /C calc'!A0")
    report.title, report.subtitle = "=TITLE()", "@sub"
    report.meta, report.warnings, report.notes = [("=label", "+value")], ["=warn()"], ["-note"]
    wb = load_workbook(io.BytesIO(render_excel(report)[0]))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", (ws.title, c.coordinate, c.value)


def test_excel_has_a_lower_row_limit_than_csv_and_explains_it(monkeypatch):
    from app.reports import excel_out
    monkeypatch.setattr(excel_out, "MAX_ROWS_EXCEL", 1)
    with pytest.raises(ReportError) as err:
        render_excel(tiny_report())                                                  # the main table has 2 rows
    assert err.value.status == 422 and err.value.code == "too_many_rows_for_excel"
    assert "CSV" in err.value.message and "shorter date range" in err.value.message
    assert render_csv(tiny_report())[0] and render_pdf(tiny_report())[0]             # the other formats are unaffected


def test_excel_sheet_names_are_valid():
    report = tiny_report()
    report.tables[0].name = "A very long sheet name: with [bad] / chars *?"
    wb = load_workbook(io.BytesIO(render_excel(report)[0]))
    name = wb.sheetnames[1]
    assert len(name) <= 31 and not set("[]:*?/\\") & set(name)


# ----------------------------------------------------------------------------------------------------- PDF
def pdf_text_of(data: bytes) -> list[str]:
    pdfium = pytest.importorskip("pypdfium2", reason="pypdfium2 reads the PDF text back; install it to run this check")
    doc = pdfium.PdfDocument(data)
    return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]


def test_pdf_is_a_real_document_with_indian_rupee_text_and_literal_markup():
    data, ext, media = render_pdf(tiny_report())
    assert data.startswith(b"%PDF-") and ext == "pdf" and media == "application/pdf"
    pages = pdf_text_of(data)
    text = "\n".join(pages)
    assert "Test report" in text and "Key figures" in text and "Read before using these numbers" in text
    assert "Rs 12,34,567.89" in text and "Rs 1,23,45,678.90" in text                  # table cell and key figure, Indian grouping
    assert "24.05%" in text and "+12.35%" in text
    assert "Tata <b>Consultancy</b> & Sons" in text                                  # markup in a name is shown, never interpreted
    assert "₹" not in text and "Page 1 of 1" in text


def test_pdf_shows_the_newest_rows_of_a_long_table_and_says_so():
    rows = [[date(2026, 1, 1) + timedelta(days=i), 1000.0 + i, 1.0, True, f"row {i}"] for i in range(120)]
    report = tiny_report(rows)
    report.tables[0].pdf_rows, report.tables[0].pdf_tail = 40, True
    text = "\n".join(pdf_text_of(render_pdf(report)[0]))
    assert "Showing the newest 40 of 120 rows. The CSV and Excel exports contain every row." in text
    assert "row 119" in text and "row 80" in text and "row 79" not in text


def test_pdf_tables_marked_spreadsheet_only_are_left_out_and_columns_can_be_subset():
    report = tiny_report()
    report.tables[1].pdf_rows = 0
    report.tables[0].pdf_columns = ["d", "name"]
    text = "\n".join(pdf_text_of(render_pdf(report)[0]))
    assert "Second" not in text and "Showing 2 of 5 columns" in text and "Amount (Rs)" not in text


def test_pdf_survives_awkward_text():
    for nasty in ["₹ → ≥ ≤ − €", "A & B < C > D", "<script>alert(1)</script>", "x" * 400, "日本語", ""]:
        assert render_pdf(tiny_report(name=nasty))[0].startswith(b"%PDF-")
    assert pdf_text("₹5 → 6") == "Rs 5 -&gt; 6" and "&lt;b&gt;" in pdf_text("<b>")


# ----------------------------------------------------------------------------- every report, from a database
@pytest.fixture()
def seeded(world):
    db, client = world
    run = db["backtest_results"].find_one({"batch": "compare", "symbol": "RELIANCE", "strategy": "ema_crossover"}, {"run_id": 1})["run_id"]
    return db, client, run


def params_for(run_id):
    return {"prices": {"symbol": "RELIANCE"}, "indicators": {"symbol": "RELIANCE", "names": "rsi_14,ema_20"}, "performance": {}, "comparison": {"symbols": "RELIANCE,TCS,NIFTY_50"},
            "sectors": {}, "backtest": {"run_id": run_id}, "strategies": {}, "benchmarks": {}}


@pytest.mark.parametrize("report_id", sorted(builders.BUILDERS))
def test_every_report_builds_and_renders_in_every_format(seeded, report_id):
    db, _, run = seeded
    params = params_for(run)[report_id]
    for fmt in FORMATS:
        content, filename, media, rows = generate(db, report_id, fmt, params)
        assert content and rows > 0 and filename.endswith((".csv", ".xlsx", ".pdf")) and filename.startswith("BharatMarket_")
        assert all(ch.isalnum() or ch in "._-" for ch in filename)
    report = build_report(db, report_id, params)
    assert report.title and report.subtitle and report.tables and all(len(r) == len(t.columns) for t in report.tables for r in t.rows)


def test_prices_report_content(seeded):
    db, _, _ = seeded
    report = build_report(db, "prices", {"symbol": "tcs", "start": datetime(2016, 1, 1, tzinfo=UTC), "end": datetime(2016, 12, 31, tzinfo=UTC)})
    table = report.tables[0]
    docs = list(db["adjusted_prices"].find({"symbol": "TCS", "date": {"$gte": datetime(2016, 1, 1, tzinfo=UTC), "$lte": datetime(2016, 12, 31, tzinfo=UTC)}}).sort("date", 1))
    assert len(table.rows) == len(docs) > 200 and table.rows[0][0] == docs[0]["date"].date() and table.rows[0][4] == docs[0]["close"]
    summary = {k: v for k, v, _ in report.summary}
    assert summary["Bars"] == len(docs) and summary["Highest high"] == max(d["high"] for d in docs)
    assert summary["Return over the period"] == pytest.approx((docs[-1]["close"] / docs[0]["open"] - 1) * 100)
    assert "split-adjusted" in dict(report.meta)["Price basis"] and any("dividends" in w for w in report.warnings)
    assert report.filename_stem.startswith("BharatMarket_prices_TCS_1d_2016-01-")


def test_prices_report_intraday_has_both_time_columns(seeded):
    db, _, _ = seeded
    base = datetime(2026, 1, 5, 3, 45, tzinfo=UTC)
    db["intraday_prices"].insert_many([{"ts": base + timedelta(minutes=5 * i), "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.5 + i, "volume": 0,
                                        "meta": {"symbol": "NIFTY_50", "exchange": "NSE", "interval": "5m", "source": "kaggle"}} for i in range(4)])
    table = build_report(db, "prices", {"symbol": "NIFTY_50", "interval": "5m"}).tables[0]
    assert [c.key for c in table.columns][:2] == ["time_utc", "time_ist"]
    assert table.rows[0][0] == "2026-01-05T03:45:00Z" and table.rows[0][1] == datetime(2026, 1, 5, 9, 15)       # 09:15 IST is 03:45 UTC


def test_prices_report_rejects_bad_requests(seeded, monkeypatch):
    db, _, _ = seeded
    cases = [({}, 422, "missing_parameter"), ({"symbol": "bad symbol"}, 422, "invalid_symbol"), ({"symbol": "NOSUCH"}, 404, "not_found"),
             ({"symbol": "TCS", "interval": "7d"}, 422, "invalid_interval"), ({"symbol": "TCS", "basis": "raw"}, 422, "source_required"),
             ({"symbol": "TCS", "interval": "1m"}, 404, "no_data"), ({"symbol": "TCS", "start": datetime(2020, 1, 1, tzinfo=UTC), "end": datetime(2019, 1, 1, tzinfo=UTC)}, 422, "invalid_range"),
             ({"symbol": "TCS", "start": datetime(2090, 1, 1, tzinfo=UTC)}, 404, "no_data")]
    for params, status, code in cases:
        with pytest.raises(ReportError) as err:
            build_report(db, "prices", params)
        assert (err.value.status, err.value.code) == (status, code), params
    monkeypatch.setattr(builders, "MAX_ROWS", 50)
    with pytest.raises(ReportError) as big:
        build_report(db, "prices", {"symbol": "TCS"})
    assert big.value.code == "too_many_rows" and "shorter date range" in big.value.message


def test_indicators_report_columns_blanks_and_validation(seeded):
    db, _, _ = seeded
    report = build_report(db, "indicators", {"symbol": "RELIANCE", "names": "rsi_14, ema_20"})
    assert [c.key for c in report.tables[0].columns] == ["date", "rsi_14", "ema_20"]
    first = report.tables[0].rows[0]
    assert first[1] is None or 0 <= first[1] <= 100
    assert any("blank on purpose" in w for w in report.warnings)
    with pytest.raises(ReportError) as err:
        build_report(db, "indicators", {"symbol": "RELIANCE", "names": "rsi_14,bogus"})
    assert err.value.code == "unknown_indicator"
    with pytest.raises(ReportError) as none:
        build_report(db, "indicators", {"symbol": "INFY"})
    assert none.value.status == 404 and none.value.code == "no_indicators" and "compute" in none.value.message.lower()
    assert len(build_report(db, "indicators", {"symbol": "RELIANCE"}).tables[0].columns) == 1 + 25                # every stored indicator


def test_performance_sectors_comparison_strategies_benchmarks(seeded):
    db, _, _ = seeded
    perf = build_report(db, "performance", {})
    assert len(perf.tables[0].rows) == 16 and perf.tables[0].rows[0][7] >= perf.tables[0].rows[-1][7]                # best 1M first
    only = build_report(db, "performance", {"symbols": "tcs,infy"})
    assert sorted(r[0] for r in only.tables[0].rows) == ["INFY", "TCS"]
    with pytest.raises(ReportError) as nothing:
        build_report(db, "performance", {"symbols": "ZZZZ"})
    assert nothing.value.status == 404
    sectors = build_report(db, "sectors", {})
    assert [t.name for t in sectors.tables] == ["sectors", "members"] and any("assigned by this project" in w for w in sectors.warnings)
    comparison = build_report(db, "comparison", {"symbols": "reliance,tcs"})
    assert [t.name for t in comparison.tables] == ["statistics", "correlation", "rebased_series"] and comparison.charts[0].series[0].points
    assert comparison.tables[1].rows[0][1] == 1.0
    for bad in ({}, {"symbols": "TCS"}, {"symbols": ",".join(f"S{i}" for i in range(11))}):
        with pytest.raises(ReportError):
            build_report(db, "comparison", bad)
    board = build_report(db, "strategies", {})
    assert {r[0] for r in board.tables[0].rows} == {"ema_crossover", "rsi_reversal"} and any("survivorship" in w for w in board.warnings)
    with pytest.raises(ReportError) as empty:
        build_report(db, "strategies", {"batch": "nothing"})
    assert empty.value.code == "no_runs"
    bench = build_report(db, "benchmarks", {})
    assert [t.name for t in bench.tables][0] == "benchmarks" and bench.tables[-1].name == "collections"
    only_pandas = build_report(db, "benchmarks", {"technology": "pandas"})
    assert all(r[1] == "pandas" for r in only_pandas.tables[0].rows)


def test_benchmarks_report_names_the_fastest_technology(seeded):
    db, _, _ = seeded
    now = datetime.now(UTC)
    for tech, secs in (("pandas", 1.0), ("pyspark", 4.0), ("mongodb_aggregation", 9.0)):
        db["benchmark_results"].insert_one({"run_id": "r2", "technology": tech, "operation": "job_x", "record_count": 5000, "duration_seconds": secs, "is_synthetic": True, "created_at": now})
    report = build_report(db, "benchmarks", {"operation": "job_x"})
    fastest = report.tables[1]
    assert fastest.name == "fastest" and fastest.rows[0][2] == "pandas" and fastest.rows[0][3] == 1.0
    assert any("synthetic" in w for w in report.warnings)


def test_backtest_report_content_and_errors(seeded):
    db, _, run = seeded
    doc = db["backtest_results"].find_one({"run_id": run})
    report = build_report(db, "backtest", {"run_id": run})
    assert [t.name for t in report.tables] == ["trades", "equity_curve", "periods"] and len(report.tables[0].rows) == len(doc["trades"])
    summary = {k: v for k, v, _ in report.summary}
    assert summary["Net profit after costs"] == doc["metrics"]["net_pnl"] and summary["Total trades"] == doc["metrics"]["total_trades"]
    assert report.charts[0].series[0].label == "Strategy" and doc["warnings"][0] in report.warnings
    assert "None" not in dict(report.meta)["Parameters"]
    for bad, status, code in (({}, 422, "missing_parameter"), ({"run_id": "../etc"}, 422, "missing_parameter"), ({"run_id": "nope"}, 404, "not_found")):
        with pytest.raises(ReportError) as err:
            build_report(db, "backtest", bad)
        assert (err.value.status, err.value.code) == (status, code)


def test_unknown_report_and_format():
    with pytest.raises(ReportError) as err:
        build_report(None, "nonsense", {})
    assert err.value.status == 404 and err.value.code == "unknown_report"
    with pytest.raises(ReportError) as fmt:
        generate(None, "prices", "docx", {})
    assert fmt.value.code == "invalid_format"
    with pytest.raises(ReportError):
        render(tiny_report(), "docx")


# --------------------------------------------------------------------------------------------- the HTTP endpoint
def test_catalog_lists_every_report_with_its_parameters(seeded):
    _, client, _ = seeded
    r = client.get("/api/reports").json()
    assert [x["id"] for x in r["reports"]] == list(builders.BUILDERS) and r["formats"] == ["csv", "xlsx", "pdf"]
    prices = next(x for x in r["reports"] if x["id"] == "prices")
    assert {"name": "symbol", "required": True, "description": "e.g. RELIANCE"} in prices["params"] and prices["tables"] == ["prices"]
    assert all(x["formats"] == ["csv", "xlsx", "pdf"] and x["title"] and x["description"] for x in r["reports"])


@pytest.mark.parametrize("fmt,media,magic", [("csv", "text/csv", b"\xef\xbb\xbf"), ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04"), ("pdf", "application/pdf", b"%PDF-")])
def test_downloading_a_report(seeded, fmt, media, magic):
    _, client, _ = seeded
    r = client.get("/api/reports/prices", params={"symbol": "TCS", "start": "2017-01-01", "format": fmt})
    assert r.status_code == 200 and r.headers["content-type"].startswith(media) and r.content.startswith(magic)
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="BharatMarket_prices_TCS_1d_2017-01-') and disposition.endswith(f'.{fmt}"')
    assert int(r.headers["x-report-rows"]) > 100 and r.headers["x-content-type-options"] == "nosniff" and r.headers["x-request-id"]


def test_csv_tables_and_zip_over_http(seeded):
    _, client, _ = seeded
    stats = client.get("/api/reports/comparison", params={"symbols": "RELIANCE,TCS", "table": "statistics"})
    assert stats.content.decode("utf-8-sig").startswith("Symbol,First close (Rs),Last close (Rs),Total return (%)")
    archive = client.get("/api/reports/comparison", params={"symbols": "RELIANCE,TCS", "table": "all"})
    assert archive.headers["content-type"] == "application/zip" and archive.headers["content-disposition"].endswith('.zip"')
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert len(z.namelist()) == 3 and all(n.endswith(".csv") for n in z.namelist())
    assert client.get("/api/reports/comparison", params={"symbols": "RELIANCE,TCS", "table": "nope"}).json()["error"]["code"] == "unknown_table"
    assert client.get("/api/reports/prices", params={"symbol": "TCS", "table": "Bad Table!"}).status_code == 422


def test_report_errors_use_the_standard_envelope(seeded):
    _, client, _ = seeded
    cases = [("/api/reports/nope", 404, "unknown_report"), ("/api/reports/prices", 422, "missing_parameter"), ("/api/reports/prices?symbol=NOPE", 404, "not_found"),
             ("/api/reports/prices?symbol=TCS&format=docx", 422, "validation_error"), ("/api/reports/prices?symbol=TCS&interval=2d", 422, "validation_error"),
             ("/api/reports/comparison?symbols=TCS", 422, "invalid_symbols"), ("/api/reports/backtest?run_id=nope", 404, "not_found"), ("/api/reports/strategies?batch=none", 404, "no_runs"),
             ("/api/reports/prices?symbol=TCS%0D%0AX-Evil:%201", 422, "invalid_symbol"), ("/api/reports/prices?symbol=TCS&source=Robert%27);%20DROP", 422, "validation_error")]
    for url, status, code in cases:
        r = client.get(url)
        assert r.status_code == status and r.json()["error"]["code"] == code and r.json()["error"]["request_id"], url


def test_text_from_the_database_cannot_run_as_a_formula_in_an_export(seeded):
    db, client, _ = seeded
    assert client.post("/api/instruments", json={"symbol": "EVIL", "name": "=HYPERLINK(\"http://evil\",\"click\")", "instrument_type": "equity", "sector": "@SUM(1+1)"}).status_code == 201
    csv_text = client.get("/api/reports/sectors", params={"table": "members"}).content.decode("utf-8-sig")
    assert "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text and "'@SUM" in csv_text
    wb = load_workbook(io.BytesIO(client.get("/api/reports/sectors", params={"format": "xlsx"}).content))
    assert all(c.data_type != "f" for ws in wb.worksheets for row in ws.iter_rows() for c in row)


def test_report_downloads_share_the_heavy_rate_limit(make_client):
    c = make_client(rate_limit_per_minute=1000, heavy_rate_limit_per_minute=2)
    codes = [c.get("/api/reports/prices?symbol=TCS").status_code for _ in range(3)]
    assert codes == [404, 404, 429]                                                           # counted even though the symbol has no data
    assert c.get("/api/reports").status_code == 200                                           # the catalog is not heavy
