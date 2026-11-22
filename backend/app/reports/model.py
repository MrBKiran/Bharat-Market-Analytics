"""The shape of a report, independent of its file format.

A builder (builders.py) turns database content into a `Report`; a renderer (csv_out / excel_out / pdf_out) turns a `Report` into a
file. Keeping the two apart means every report is available in every format without writing it three times, and a number is formatted by
exactly one rule per format.

Column kinds (what the value MEANS, so each format can show it properly):
    text, date, datetime, int, float, inr (rupees), pct (percent, 12.5 means 12.5%), ratio, bool
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional

KINDS = ("text", "date", "datetime", "int", "float", "inr", "pct", "percent", "ratio", "bool")   # pct is signed (+12.50%), percent is plain (24.05%)
MAX_ROWS = 250_000                 # per table, any format: keeps one request's memory bounded
MAX_ROWS_EXCEL = 100_000           # per table, Excel only: building a workbook costs about 3 KB of memory per row (measured: 209,888 rows = 615 MB, 21 s)
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Column:
    key: str
    header: str
    kind: str = "text"
    width: Optional[int] = None    # Excel column width in characters; automatic when None


@dataclass
class Table:
    name: str                      # CSV table name and Excel sheet name (letters, digits, _ only; at most 31 characters)
    title: str
    columns: list[Column]
    rows: list[list[Any]]
    note: Optional[str] = None
    pdf_columns: Optional[list[str]] = None   # keys of the columns shown in the PDF (the PDF page is narrower than a sheet)
    pdf_rows: int = 40                        # how many rows the PDF shows; CSV and Excel always carry every row
    pdf_tail: bool = False                    # show the LAST rows (newest bars) instead of the first


@dataclass
class Series:
    label: str
    points: list[tuple[Any, float]]           # (date or datetime, value)


@dataclass
class Chart:
    title: str
    series: list[Series]
    y_kind: str = "float"


@dataclass
class Report:
    id: str
    title: str
    subtitle: str
    generated_at: datetime
    filename_stem: str
    meta: list[tuple[str, str]] = field(default_factory=list)                 # label, text: what was asked for
    summary: list[tuple[str, Any, str]] = field(default_factory=list)         # label, value, kind
    tables: list[Table] = field(default_factory=list)
    charts: list[Chart] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    landscape: bool = False

    def table(self, name: Optional[str]) -> Table:
        if name is None:
            return self.tables[0]
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(name)

    @property
    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables)


class ReportError(ValueError):
    """A report that cannot be built, with an HTTP-style status and a short code the API passes on."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def tidy(value: Any, kind: str) -> Any:
    """Remove binary floating-point noise from numbers before they are written to a file. The database keeps prices as 32-bit floats
    (3138.1 is stored as 3138.10009765625); an export should show 3138.1. Rupee amounts above 1,000 keep paise, everything else keeps
    7-8 significant digits, so small adjusted prices lose nothing that matters."""
    if isinstance(value, float) and not isinstance(value, bool) and value == value and value not in (float("inf"), float("-inf")):
        if kind == "inr":
            return round(value, 2) if abs(value) >= 1000 else float(f"{value:.7g}")
        if kind in ("pct", "percent", "ratio", "float"):
            return float(f"{value:.8g}")
    return value


# --------------------------------------------------------------------------------------------- text safety
def neutralise(value: Any) -> Any:
    """Defuse spreadsheet formulas. A text cell that begins with = + - @ is executed by Excel when the file is opened
    (CSV injection), so such text gets a leading apostrophe. Numbers, dates and everything else pass through unchanged."""
    if isinstance(value, str) and value.startswith(FORMULA_STARTS):
        return "'" + value
    return value


def safe_filename(stem: str, extension: str) -> str:
    """A download name made only of letters, digits and . _ - (nothing a browser or shell could misread)."""
    cleaned = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", stem)).strip("._-") or "report"    # no ".." sequences either
    return f"{cleaned[:120]}.{extension}"


def sheet_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_ ]", "_", name)[:31] or "Sheet"


# ------------------------------------------------------------------------------------------- Indian numbers
def group_indian(integer_digits: str) -> str:
    """'1234567' -> '12,34,567' (the last three digits, then pairs)."""
    if len(integer_digits) <= 3:
        return integer_digits
    head, tail = integer_digits[:-3], integer_digits[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    if head:
        pairs.insert(0, head)
    return ",".join(pairs + [tail])


def format_indian(value: float, digits: int = 2) -> str:
    sign = "-" if value < 0 else ""
    text = f"{abs(value):.{digits}f}"
    integer, _, decimals = text.partition(".")
    return sign + group_indian(integer) + (f".{decimals}" if decimals else "")


def format_value(value: Any, kind: str) -> str:
    """The text of a value for a PDF page. (The PDF font has no rupee sign, so rupees are written 'Rs'.)"""
    if value is None or (isinstance(value, float) and value != value):
        return "-"
    if kind == "inr":
        return "Rs " + format_indian(float(value), 2)
    if kind == "pct":
        return f"{float(value):+.2f}%" if abs(float(value)) < 1e12 else "-"
    if kind == "percent":
        return f"{float(value):.2f}%"
    if kind == "ratio":
        return f"{float(value):.2f}"
    if kind == "float":
        return format_indian(float(value), 2)
    if kind == "int":
        return format_indian(float(value), 0)
    if kind == "bool":
        return "Yes" if value else "No"
    if kind == "date":
        return value.strftime("%d %b %Y") if isinstance(value, (date, datetime)) else str(value)
    if kind == "datetime":
        return value.strftime("%d %b %Y %H:%M") if isinstance(value, datetime) else str(value)
    return str(value)
