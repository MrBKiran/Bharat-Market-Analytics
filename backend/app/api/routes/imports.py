"""Data importing, background processing and job tracking.

  POST /imports/upload       upload one CSV/XLSX price file and import it (validated, de-duplicated, resumable)
  GET  /imports              recent import jobs
  POST /processing/{step}    start a long step in the background: adjust | audit | indicators | analytics  (returns 202)
  GET  /jobs, /jobs/{id}     status of any job (queued, running, completed, failed) with its counts or error

Everything here that changes data needs the API key (see docs/API.md) and is rate limited as "heavy".
Uploads are checked before anything is read: extension (.csv/.xlsx only), size (MAX_UPLOAD_MB), a content sniff (an .xlsx must
be a zip, a .csv must be text), a random server-side file name (the client's name is never used as a path), and then the same
row-by-row validation as the command-line importer. Rejected rows are saved and counted, never silently dropped.
"""
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from pymongo.database import Database
from pymongo.errors import WriteError

from app.analytics import market
from app.analytics.indicator_pipeline import run_indicators
from app.api.serialize import SafeAPIRouter
from app.api.deps import (EXCHANGES, Page, check_write_access, get_database, heavy_rate_limit, norm_symbol, page_params, paginate)
from app.api.errors import ApiError
from app.collection.adapters import ALLOWED_SUFFIXES, detect_plan
from app.collection.importer import import_file
from app.core.config import get_settings
from app.processing import adjustment, quality
from app.processing.jobs import write_report

logger = logging.getLogger(__name__)
router = SafeAPIRouter(tags=["Import and processing"])

JOB_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"
CHUNK = 1024 * 1024
STALE_AFTER = timedelta(hours=2)          # a "running" job untouched for this long is assumed dead (server restarted)


# ---------------------------------------------------------------------------------------------------- upload
def _save_upload(upload: UploadFile) -> Path:
    """Copy the upload to the uploads folder under a random name, enforcing type, size and content checks."""
    settings = get_settings()
    original = Path(upload.filename or "").name
    suffix = Path(original).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ApiError(422, "unsupported_file_type", f"Only {', '.join(sorted(ALLOWED_SUFFIXES))} files can be imported, got {suffix or 'no extension'!r}.")
    limit = settings.max_upload_mb * 1024 * 1024
    folder = settings.data_dir / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    # Each upload gets its own random folder and keeps its (sanitised) name: the importer reads hints such as the exchange
    # from the file name, and the client's name is never used as a path, so "../" tricks cannot escape the uploads folder.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", original).lstrip(".")[:80] or f"upload{suffix}"
    dest = folder / uuid.uuid4().hex[:12] / safe
    dest.parent.mkdir(parents=True)
    size, head = 0, b""
    try:
        with open(dest, "wb") as out:
            while chunk := upload.file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise ApiError(413, "file_too_large", f"The file is larger than the {settings.max_upload_mb} MB upload limit.")
                if not head:
                    head = chunk[:4096]
                out.write(chunk)
        if size == 0:
            raise ApiError(422, "empty_file", "The uploaded file is empty.")
        if suffix == ".xlsx" and not head.startswith(b"PK\x03\x04"):
            raise ApiError(422, "not_an_xlsx", "The file is named .xlsx but is not an Excel workbook.")
        if suffix == ".csv" and b"\x00" in head:
            raise ApiError(422, "not_a_csv", "The file is named .csv but contains binary data.")
    except BaseException:
        _discard(dest)
        raise
    return dest


def _discard(path: Path) -> None:
    """Delete an upload and its private folder."""
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


@router.post("/imports/upload", dependencies=[Depends(check_write_access), Depends(heavy_rate_limit)],
             summary="Upload and import a price file")
def upload_and_import(
    file: UploadFile = File(..., description="a .csv or .xlsx file of price bars"),
    symbol: Optional[str] = Form(None, description="override the symbol (otherwise read from the file or its name)"),
    exchange: Optional[Literal["NSE", "BSE"]] = Form(None),
    interval: Optional[Literal["1d", "1m", "5m", "15m", "30m", "1h"]] = Form(None),
    instrument_type: Optional[Literal["equity", "index", "etf"]] = Form(None),
    source: Optional[str] = Form(None, pattern=r"^[a-z0-9_]{1,40}$"),
    price_basis: Optional[Literal["raw", "split_adjusted", "index_level"]] = Form(None),
    auto_register: bool = Form(False, description="create the instrument if it is not in the master list"),
    dry_run: bool = Form(False, description="check the file and report what would happen, store nothing"),
    db: Database = Depends(get_database),
):
    overrides = {"symbol": norm_symbol(symbol) if symbol else None, "exchange": exchange, "interval": interval,
                 "instrument_type": instrument_type, "source": source, "price_basis": price_basis}
    path = _save_upload(file)
    try:                                       # settle the instrument's exchange and interval first: a clear error beats a database one
        plan = detect_plan(path, {k: v for k, v in overrides.items() if v})
    except Exception as exc:  # noqa: BLE001 - any unreadable or unrecognised file is the client's problem, reported plainly
        _discard(path)
        raise ApiError(422, "import_failed", f"The file could not be read: {exc}".replace(str(path.parent), "upload"))
    if plan.exchange is not None and plan.exchange not in EXCHANGES:
        _discard(path)
        raise ApiError(422, "exchange_unknown", f"The exchange could not be determined (got {plan.exchange!r}). Send exchange=NSE or BSE as a "
                       "form field, or name the file like TCS_NSE.csv.")
    try:
        result = import_file(db, path, overrides={k: v for k, v in overrides.items() if v}, auto_register=auto_register, dry_run=dry_run)
    except WriteError:
        _discard(path)
        raise ApiError(422, "rejected_by_database", "The database rejected the data. Check the symbol, exchange and interval: "
                       "name the file like TCS_NSE.csv or send them as form fields.")
    except BaseException:
        _discard(path)
        raise
    result.pop("path", None)                                      # never reveal server paths
    result["file"] = Path(file.filename or "upload").name
    status = result.get("status")
    if status in ("error", "failed"):
        _discard(path)
        raise ApiError(422, "import_failed", result.get("reason") or "The file could not be imported.", result)
    if status == "skipped":
        _discard(path)
        raise ApiError(409, "unknown_instrument", result.get("reason", "Instrument not registered."), result)
    if dry_run or status == "already_imported":
        _discard(path)
    return result


@router.get("/imports", summary="Recent import jobs")
def list_imports(page: Page = Depends(page_params), db: Database = Depends(get_database)):
    return paginate(db["processing_jobs"], {"job_type": "import"}, page, sort=[("created_at", -1)])


# ----------------------------------------------------------------------------------------------------- jobs
@router.get("/jobs", summary="Processing jobs")
def list_jobs(job_type: Optional[Literal["import", "clean", "indicators", "analytics", "backtest", "spark", "benchmark", "report"]] = None,
              status: Optional[Literal["queued", "running", "paused", "completed", "failed", "cancelled"]] = None,
              page: Page = Depends(page_params), db: Database = Depends(get_database)):
    query = {k: v for k, v in (("job_type", job_type), ("status", status)) if v}
    return paginate(db["processing_jobs"], query, page, sort=[("created_at", -1)])


@router.get("/jobs/{job_id}", summary="One job")
def get_job(job_id: str, db: Database = Depends(get_database)):
    if not re.match(JOB_ID_PATTERN, job_id):
        raise ApiError(422, "invalid_job_id", "job_id may contain letters, digits, _ . and - only.")
    job = db["processing_jobs"].find_one({"job_id": job_id}, {"_id": 0})
    if not job:
        raise ApiError(404, "not_found", f"No job {job_id}.")
    return job


# ------------------------------------------------------------------------------------ background processing
class ProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: Optional[list[str]] = Field(None, max_length=50, description="indicators step only: limit to these symbols")


def _counts(d: dict) -> dict:
    return {k: v for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _step_adjust(db, job_id, params):
    return _counts(adjustment.build_adjusted_prices(db, job_id=job_id))


def _step_audit(db, job_id, params):
    audit = quality.run_audit(db)
    write_report(db, "processed:audit", job_id, audit)
    return {"duplicate_groups": sum(v for v in audit["duplicates"].values() if isinstance(v, int))}


def _step_indicators(db, job_id, params):
    results = run_indicators(db, ["1d"], symbols=params.get("symbols"))
    return {"series": len(results), "documents": sum(r.get("documents", 0) for r in results), "bars": sum(r.get("bars", 0) for r in results)}


def _step_analytics(db, job_id, params):
    return {"analyses_stored": market.run_analytics(db)["stored"]}


STEPS = {"adjust": (_step_adjust, "clean"), "audit": (_step_audit, "clean"),
         "indicators": (_step_indicators, "indicators"), "analytics": (_step_analytics, "analytics")}


def _run_step(db: Database, job_id: str, step: str, params: dict) -> None:
    """Runs after the response has been sent. Records success or failure on the job, never raises."""
    now = datetime.now(timezone.utc)
    db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {"status": "running", "started_at": now, "updated_at": now, "progress": 5.0}})
    try:
        counts = STEPS[step][0](db, job_id, params)
        done = datetime.now(timezone.utc)
        db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {"status": "completed", "progress": 100.0, "counts": counts,
                                                                       "finished_at": done, "updated_at": done}})
    except Exception as exc:  # noqa: BLE001 - a failed job must be recorded, not crash the worker thread
        logger.error("processing step %s failed (job %s): %s", step, job_id, type(exc).__name__, exc_info=True)
        db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
            "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500], "updated_at": datetime.now(timezone.utc)}})


@router.post("/processing/{step}", status_code=202, dependencies=[Depends(check_write_access), Depends(heavy_rate_limit)],
             summary="Start a processing step in the background")
def start_processing(step: Literal["adjust", "audit", "indicators", "analytics"], background: BackgroundTasks,
                     body: Optional[ProcessingRequest] = None, db: Database = Depends(get_database)):
    """Returns at once with a `job_id`; poll `GET /api/jobs/{job_id}`. One run of each step at a time."""
    params: dict = {}
    if body and body.symbols:
        if step != "indicators":
            raise ApiError(422, "symbols_not_supported", "Only the indicators step accepts a symbols list.")
        params["symbols"] = [norm_symbol(s) for s in body.symbols]
    fresh = datetime.now(timezone.utc) - STALE_AFTER
    active = db["processing_jobs"].find_one({"params.step": step, "status": {"$in": ["queued", "running"]}, "updated_at": {"$gte": fresh}},
                                            {"job_id": 1})
    if active:
        raise ApiError(409, "already_running", f"The {step} step is already running.", {"job_id": active["job_id"]})
    now = datetime.now(timezone.utc)
    job_id = f"{STEPS[step][1]}_{step}_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    db["processing_jobs"].insert_one({"job_id": job_id, "job_type": STEPS[step][1], "status": "queued", "progress": 0.0, "source": "api",
                                      "created_at": now, "updated_at": now, "params": {"step": step, **params}})
    background.add_task(_run_step, db, job_id, step, params)
    return {"job_id": job_id, "status": "queued", "step": step, "status_url": f"/api/jobs/{job_id}"}
