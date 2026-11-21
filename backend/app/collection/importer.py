"""Bulk importer: files -> validated documents -> MongoDB, with progress, resume and de-duplication.

How one file is imported (`import_file`):
  1. Hash the file (SHA-256). If it was already imported completely -> skip it.
  2. Work out its format (adapters.py) and look up the instrument in `instruments`.
  3. Create a job in `processing_jobs` and a record in `import_files`.
  4. Read the file in chunks. For every chunk: validate rows, drop duplicates, insert, then save
     progress and a checkpoint. A crash leaves the job `failed`; running the same command again
     resumes from the checkpoint and never inserts a bar twice.
  5. Write a data quality report and (for intraday data) update `intraday_coverage`.

De-duplication:
  * stock_prices / market_indices: the unique index rejects repeats (counted as duplicates).
  * intraday_prices (time-series, no unique index possible): before inserting a chunk we look up
    which timestamps already exist for that exact series and drop them.
"""
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from pymongo.database import Database
from pymongo.errors import BulkWriteError

from app.collection.adapters import ALLOWED_SUFFIXES, ImportPlan, detect_plan, expand_paths, read_chunks
from app.collection.common import sha256_file
from app.collection.validation import validate_bars
from app.core.config import get_settings

logger = logging.getLogger(__name__)

MAX_REJECT_ROWS_SAVED = 100_000
IMPORTABLE_TYPES = ("equity", "etf", "index")
EXTRA_INT_FIELDS = {"trades"}          # these must be stored as integers, not floats
# Optional fields the database requires to be > 0 / >= 0. A bad value drops only that FIELD
# (e.g. Prev Close = 0 on a stock's listing day), never the whole price bar.
POSITIVE_EXTRAS = {"prev_close", "adj_close"}
NON_NEGATIVE_EXTRAS = {"turnover", "trades", "deliverable_volume", "pct_deliverable", "vwap"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(doc: dict) -> dict:
    """Drop None values (MongoDB validators reject null for typed fields)."""
    return {k: v for k, v in doc.items() if v is not None}


def _count_data_rows(path: Path) -> Optional[int]:
    if path.suffix.lower() != ".csv":
        return None
    with open(path, "rb") as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def _target_collection(plan: ImportPlan) -> str:
    if plan.intraday:
        return "intraday_prices"
    return "market_indices" if plan.instrument_type == "index" else "stock_prices"


# ----------------------------------------------------------------------------- instruments
def resolve_instrument(db: Database, plan: ImportPlan, auto_register: bool, *, dry_run: bool = False) -> Optional[dict]:
    """Find the instrument for this file, optionally registering it. None = unknown instrument."""
    query = {"symbol": plan.symbol, "active": True}
    if plan.exchange:
        query["exchange"] = plan.exchange
    found = db["instruments"].find_one(query, {"_id": 0})
    if found:
        return found
    if not auto_register or plan.instrument_type not in IMPORTABLE_TYPES:
        return None

    key = {"exchange": plan.exchange or "NSE", "symbol": plan.symbol, "instrument_type": plan.instrument_type}
    info = plan.instrument_info
    doc = _clean({"name": info.get("name") or plan.symbol, "isin": info.get("isin"),
                  "industry_legacy": info.get("industry_legacy"), "currency": "INR",
                  "auto_registered": True, "active": True, "created_at": _now()})
    if dry_run:
        return {**key, **doc}
    db["instruments"].update_one(key, {"$setOnInsert": doc}, upsert=True)
    return db["instruments"].find_one(key, {"_id": 0})


# ----------------------------------------------------------------------------- documents
def _num(v):
    """Python int when the number is whole, else float (NumPy types are not valid BSON)."""
    f = float(v)
    return int(f) if f.is_integer() else f


def _build_documents(good: pd.DataFrame, plan: ImportPlan, job_id: str, collection: str) -> tuple[list[dict], int]:
    """Returns (documents, number of optional field values dropped because they were invalid)."""
    stamps = [t.to_pydatetime() for t in good["ts"]]
    opens, highs, lows, closes = (good[c].tolist() for c in ("open", "high", "low", "close"))
    volumes = good["volume"].tolist()
    extras = {name: good[name].tolist() for name in plan.extras if name in good.columns}

    if collection == "intraday_prices":
        meta = {"symbol": plan.symbol, "exchange": plan.exchange, "interval": plan.interval,
                "instrument_type": plan.instrument_type, "source": plan.source,
                "is_synthetic": plan.is_synthetic, "price_basis": plan.price_basis}
        docs = []
        for i, t in enumerate(stamps):
            doc = {"ts": t, "meta": meta, "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i]}
            if volumes[i] == volumes[i]:                       # not NaN
                doc["volume"] = _num(volumes[i])
            docs.append(doc)
        return docs, 0

    base = {"symbol": plan.symbol, "exchange": plan.exchange, "interval": plan.interval,
            "source": plan.source, "retrieved_at": plan.retrieved_at, "is_synthetic": plan.is_synthetic,
            "price_basis": plan.price_basis, "import_job_id": job_id}
    if collection == "stock_prices":
        base["instrument_type"] = plan.instrument_type
    docs = []
    dropped = 0
    for i, t in enumerate(stamps):
        doc = {**base, "date": t, "open": opens[i], "high": highs[i], "low": lows[i], "close": closes[i]}
        if volumes[i] == volumes[i]:
            doc["volume"] = _num(volumes[i])
        for name, values in extras.items():
            v = values[i]
            if v is None or v != v or v is pd.NA:               # missing
                continue
            if isinstance(v, (int, float)) and ((name in POSITIVE_EXTRAS and v <= 0)
                                                or (name in NON_NEGATIVE_EXTRAS and v < 0)):
                dropped += 1                                    # keep the bar, skip only this field
                continue
            doc[name] = (int(v) if name in EXTRA_INT_FIELDS else v)
        docs.append(doc)
    return docs, dropped


# ----------------------------------------------------------------------------- inserting
def _insert_regular(coll, docs: list[dict]) -> tuple[int, int, int, list[str]]:
    """Insert, tolerating failures. Returns (inserted, duplicates, db_rejected, error_samples)."""
    try:
        res = coll.insert_many(docs, ordered=False)
        return len(res.inserted_ids), 0, 0, []
    except BulkWriteError as exc:
        details = exc.details
        errors = details.get("writeErrors", [])
        dups = sum(1 for e in errors if e.get("code") == 11000)
        others = [e for e in errors if e.get("code") != 11000]
        samples = [str(e.get("errmsg", ""))[:120] for e in others[:3]]
        return details.get("nInserted", 0), dups, len(others), samples


def _insert_timeseries(coll, docs: list[dict]) -> tuple[int, int, int, list[str]]:
    return _insert_regular(coll, docs)      # same behaviour; separate name so tests can intercept either


def _drop_existing_timestamps(db: Database, plan: ImportPlan, good: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Exact de-duplication for the time-series collection: remove bars already stored."""
    if good.empty:
        return good, 0
    cursor = db["intraday_prices"].find(
        {"meta.symbol": plan.symbol, "meta.exchange": plan.exchange, "meta.interval": plan.interval,
         "meta.source": plan.source, "ts": {"$gte": good["ts"].min().to_pydatetime(),
                                            "$lte": good["ts"].max().to_pydatetime()}},
        {"ts": 1, "_id": 0})
    existing = [d["ts"] for d in cursor]
    if not existing:
        return good, 0
    seen = pd.to_datetime(existing, utc=True)
    keep = ~good["ts"].isin(seen)
    return good[keep], int((~keep).sum())


def _update_coverage(db: Database, plan: ImportPlan, good: pd.DataFrame, inserted: int) -> None:
    if inserted <= 0 or good.empty:
        return
    db["intraday_coverage"].update_one(
        {"exchange": plan.exchange, "symbol": plan.symbol, "interval": plan.interval, "source": plan.source},
        {"$min": {"first_ts": good["ts"].min().to_pydatetime()},
         "$max": {"last_ts": good["ts"].max().to_pydatetime()},
         "$inc": {"row_count": int(inserted)}, "$set": {"updated_at": _now()}},
        upsert=True)


# ----------------------------------------------------------------------------- rejected rows
def _save_rejects(bad: pd.DataFrame, job_id: str, file_name: str, already_saved: int) -> None:
    room = MAX_REJECT_ROWS_SAVED - already_saved
    if bad.empty or room <= 0:
        return
    folder = get_settings().data_dir / "rejected"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{job_id}__{file_name}.rejected.csv"
    out = bad.head(room).copy()
    out["ts"] = out["ts"].astype(str)
    out.to_csv(target, mode="a", header=not target.exists(), index=False)


# ----------------------------------------------------------------------------- main entry point
def import_file(db: Database, path, *, overrides: Optional[dict] = None, auto_register: bool = False,
                chunk_size: int = 100_000, dry_run: bool = False, force: bool = False,
                progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Import one file. Returns a result dict with a `status`:
    completed | failed | already_imported | skipped | dry_run | error.

    `force=True` re-processes a file that was already imported completely (for example after a fix to
    the importer). It is safe: bars that are already stored are counted as duplicates, not stored twice."""
    started = time.perf_counter()
    path = Path(path)
    result: dict = {"file": path.name, "path": str(path)}

    if not path.is_file():
        return {**result, "status": "error", "reason": "file not found"}
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return {**result, "status": "error", "reason": f"unsupported file type {path.suffix!r}"}
    # (The upload size limit, MAX_UPLOAD_MB, applies to files uploaded through the API in Phase 10;
    #  local bulk imports of large files are allowed.)
    try:
        plan = detect_plan(path, overrides)
    except Exception as exc:  # noqa: BLE001
        return {**result, "status": "error", "reason": f"cannot read file: {exc}"}

    sha = sha256_file(path)
    existing = None if dry_run else db["import_files"].find_one({"sha256": sha})
    if existing and existing.get("status") == "completed" and not force:
        return {**result, "status": "already_imported", "job_id": existing.get("job_id")}
    resuming = bool(existing) and existing.get("status") != "completed"

    inst = resolve_instrument(db, plan, auto_register, dry_run=dry_run)
    if inst is None:
        return {**result, "status": "skipped",
                "reason": f"{plan.symbol} is not in the instruments collection (use --auto-register)"}
    if inst["instrument_type"] not in IMPORTABLE_TYPES:
        return {**result, "status": "skipped", "reason": f"unsupported instrument type {inst['instrument_type']}"}
    plan.exchange, plan.instrument_type = inst["exchange"], inst["instrument_type"]
    collection = _target_collection(plan)
    total_rows = _count_data_rows(path)
    result.update(symbol=plan.symbol, interval=plan.interval, collection=collection, adapter=plan.adapter)

    counts = {"read": 0, "inserted": 0, "duplicates": 0, "rejected": 0, "optional_fields_dropped": 0}
    reasons: dict[str, int] = {}
    examples: dict[str, list] = {}
    start_chunk = 0
    job_id = f"imp_{_now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"

    if not dry_run:
        if resuming:   # an earlier attempt failed or was interrupted: resume it
            job_id = existing.get("job_id", job_id)
            old = db["processing_jobs"].find_one({"job_id": job_id}) or {}
            start_chunk = int(old.get("checkpoint", {}).get("chunks_done", 0))
            saved = old.get("counts", {})
            counts.update({k: int(saved.get(k, 0)) for k in counts})
            reasons = dict(saved.get("reasons", {}))
            db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {"status": "running", "updated_at": _now()},
                                                                  "$unset": {"error": ""}})
            db["import_files"].update_one({"sha256": sha}, {"$set": {"status": "processing"}, "$unset": {"error": ""}})
        else:
            now = _now()
            db["processing_jobs"].insert_one(_clean({
                "job_id": job_id, "job_type": "import", "status": "running", "progress": 0.0,
                "source": plan.source, "created_at": now, "started_at": now, "updated_at": now,
                "params": {"path": str(path), "format": plan.adapter, "collection": collection,
                           "symbol": plan.symbol, "interval": plan.interval, "chunk_size": chunk_size,
                           "auto_register": auto_register},
                "checkpoint": {"chunks_done": 0}, "counts": {**counts, "reasons": {}}}))
            record = _clean({
                "sha256": sha, "filename": path.name, "size_bytes": path.stat().st_size, "source": plan.source,
                "dataset": plan.dataset, "status": "processing", "retrieved_at": plan.retrieved_at,
                "job_id": job_id, "is_synthetic": plan.is_synthetic, "created_at": now})
            if existing:   # --force on a finished file: start a fresh run on the same record
                db["import_files"].update_one({"sha256": sha}, {"$set": record})
            else:
                db["import_files"].insert_one(record)
    result["job_id"] = job_id

    try:
        for i, chunk in enumerate(read_chunks(plan, chunk_size)):
            if i < start_chunk:
                continue
            counts["read"] += len(chunk)
            good, bad = validate_bars(chunk, volume_required=(collection == "stock_prices"))

            if len(bad):
                for reason, group in bad.groupby("reject_reason"):
                    reasons[reason] = reasons.get(reason, 0) + len(group)
                    if len(examples.setdefault(reason, [])) < 3:
                        row = group.iloc[0]
                        # float(): NumPy numbers cannot be stored in MongoDB
                        examples[reason].append({"ts": str(row["ts"]), "open": float(row["open"]),
                                                 "high": float(row["high"]), "low": float(row["low"]),
                                                 "close": float(row["close"])})
                if not dry_run:
                    _save_rejects(bad, job_id, path.name, counts["rejected"])
                counts["rejected"] += len(bad)

            if dry_run:
                counts["inserted"] += len(good)
            elif len(good):
                if collection == "intraday_prices":
                    good, already = _drop_existing_timestamps(db, plan, good)
                    counts["duplicates"] += already
                if len(good):
                    docs, dropped = _build_documents(good, plan, job_id, collection)
                    counts["optional_fields_dropped"] += dropped
                    insert = _insert_timeseries if collection == "intraday_prices" else _insert_regular
                    ins, dup, other, samples = insert(db[collection], docs)
                    counts["inserted"] += ins
                    counts["duplicates"] += dup
                    counts["rejected"] += other
                    if other:
                        reasons["rejected_by_database"] = reasons.get("rejected_by_database", 0) + other
                        examples.setdefault("rejected_by_database", []).extend(samples)
                    if collection == "intraday_prices":
                        _update_coverage(db, plan, good, ins)

            pct = min(99.0, round(100.0 * counts["read"] / total_rows, 1)) if total_rows else 0.0
            if not dry_run:
                db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
                    "progress": pct, "updated_at": _now(), "checkpoint": {"chunks_done": i + 1},
                    "counts": {**counts, "reasons": reasons}}})
            if progress:
                elapsed = time.perf_counter() - started
                progress({"file": path.name, "read": counts["read"], "total": total_rows, "percent": pct,
                          "rows_per_second": int(counts["read"] / elapsed) if elapsed else 0})
    except Exception as exc:  # noqa: BLE001 - keep the job resumable, report, carry on with other files
        logger.exception("Import of %s failed", path.name)
        message = f"{type(exc).__name__}: {exc}"[:500]
        if not dry_run:
            db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
                "status": "failed", "error": message, "updated_at": _now(), "counts": {**counts, "reasons": reasons}}})
            db["import_files"].update_one({"sha256": sha}, {"$set": {"status": "failed", "error": message}})
        return {**result, "status": "failed", "error": message, "counts": counts}

    duration = time.perf_counter() - started
    rate = int(counts["read"] / duration) if duration > 0 else 0
    result.update(counts=counts, reasons=reasons, duration_seconds=round(duration, 2), rows_per_second=rate)
    if dry_run:
        return {**result, "status": "dry_run"}

    finished = _now()
    db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
        "status": "completed", "progress": 100.0, "finished_at": finished, "updated_at": finished,
        "counts": {**counts, "reasons": reasons}}})
    db["import_files"].update_one({"sha256": sha}, {"$set": {
        "status": "completed", "rows_read": counts["read"], "rows_inserted": counts["inserted"],
        "rows_duplicate": counts["duplicates"], "rows_rejected": counts["rejected"], "completed_at": finished,
        "collection": collection, "duration_seconds": round(duration, 2), "rows_per_second": rate}})
    db["data_quality_reports"].insert_one(_clean({
        "dataset": plan.dataset or path.name, "job_id": job_id, "created_at": finished,
        "totals": {**counts, "file": path.name, "symbol": plan.symbol, "interval": plan.interval,
                   "collection": collection},
        "issues": [{"reason": r, "count": n, "examples": examples.get(r, [])} for r, n in sorted(reasons.items())]}))
    return {**result, "status": "completed"}


def import_paths(db: Database, paths: list, **kwargs) -> list[dict]:
    """Import many files/folders one after another. One bad file never stops the others."""
    return [import_file(db, f, **kwargs) for f in expand_paths([Path(p) for p in paths])]


def recent_jobs(db: Database, limit: int = 20) -> list[dict]:
    return list(db["processing_jobs"].find({"job_type": "import"}, {"_id": 0}).sort("created_at", -1).limit(limit))
