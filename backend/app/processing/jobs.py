"""Job tracking and quality reports for processing steps (job_type = "clean")."""
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from pymongo.database import Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def tracked_job(db: Database, step: str, params: Optional[dict] = None) -> Iterator[dict]:
    """Record a processing step in `processing_jobs`. Put results in ctx["counts"]."""
    now = _now()
    job_id = f"clean_{step}_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    db["processing_jobs"].insert_one({
        "job_id": job_id, "job_type": "clean", "status": "running", "progress": 0.0, "source": "processing",
        "created_at": now, "started_at": now, "updated_at": now, "params": {"step": step, **(params or {})},
    })
    ctx: dict = {"job_id": job_id, "counts": {}}
    try:
        yield ctx
    except Exception as exc:
        db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
            "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500], "updated_at": _now(),
            "counts": ctx["counts"]}})
        raise
    done = _now()
    db["processing_jobs"].update_one({"job_id": job_id}, {"$set": {
        "status": "completed", "progress": 100.0, "finished_at": done, "updated_at": done,
        "counts": ctx["counts"]}})


def write_report(db: Database, dataset: str, job_id: Optional[str], totals: dict, issues: Optional[list] = None) -> None:
    doc = {"dataset": dataset, "totals": totals, "issues": issues or [], "created_at": _now()}
    if job_id:
        doc["job_id"] = job_id
    db["data_quality_reports"].insert_one(doc)
