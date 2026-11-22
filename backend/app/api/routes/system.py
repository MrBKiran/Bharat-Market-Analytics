"""System information: database statistics, performance benchmarks and data-quality reports (read-only)."""
import re
from typing import Literal, Optional

from fastapi import Depends, Query
from pymongo.database import Database

from app.api.serialize import SafeAPIRouter
from app.api.deps import Page, get_database, page_params, paginate

router = SafeAPIRouter(tags=["System"])


@router.get("/stats", summary="What is in the database")
def stats(db: Database = Depends(get_database)):
    collections = {name: db[name].estimated_document_count()
                   for name in sorted(db.list_collection_names()) if not name.startswith("system.")}
    db_stats = db.command("dbstats", scale=1024 * 1024)
    daily = list(db["adjusted_prices"].aggregate([{"$match": {"interval": "1d"}}, {"$group": {"_id": None, "first": {"$min": "$date"},
                                                                                                "last": {"$max": "$date"}, "symbols": {"$addToSet": "$symbol"}}}]))
    return {"database": db.name, "collections": collections,
            "size_mb": {"data": round(db_stats.get("dataSize", 0), 1), "storage": round(db_stats.get("storageSize", 0), 1)},
            "daily_prices": ({"first": daily[0]["first"], "last": daily[0]["last"], "symbols": len(daily[0]["symbols"])} if daily else None),
            "note": "counts are estimates from collection metadata (exact for this purpose, instant on millions of rows)"}


@router.get("/benchmarks", summary="Performance benchmark results")
def benchmarks(technology: Optional[Literal["pandas", "mongodb_aggregation", "pyspark", "ingestion"]] = None,
               operation: Optional[str] = Query(None, max_length=80), is_synthetic: Optional[bool] = None,
               page: Page = Depends(page_params), db: Database = Depends(get_database)):
    query = {k: v for k, v in (("technology", technology), ("operation", operation), ("is_synthetic", is_synthetic)) if v is not None}
    return paginate(db["benchmark_results"], query, page, sort=[("created_at", -1), ("operation", 1), ("technology", 1)])


_SIZE_SUFFIX = re.compile(r"\[[^\]]*\]$")            # Phase 8 names its resample jobs "intraday_resample_1m_to_5m[4_indices]"


def _kind(operation: str) -> str:
    return "ingestion" if operation.startswith("ingest_") else "query" if operation.startswith("query_") else "aggregation"


def _scaling_view(docs: list[dict]) -> dict:
    """Group timings for charts: job -> technology -> points over the data size (the newest timing for each combination)."""
    newest: dict = {}
    for d in docs:                                                       # docs are newest first
        newest.setdefault((_SIZE_SUFFIX.sub("", d["operation"]), d["technology"], d["record_count"]), d)
    jobs: dict = {}
    for (operation, technology, records), d in newest.items():
        jobs.setdefault(operation, {}).setdefault(technology, []).append({
            "records": records, "seconds": d["duration_seconds"], "records_per_second": d.get("records_per_second"),
            "cpu_percent": d.get("cpu_percent"), "memory_mb": d.get("memory_mb"), "storage_mb": d.get("storage_mb"),
            "answers_match": d.get("answers_match", "DIFFERENT" not in d.get("notes", "")), "run_id": d["run_id"],
            "measured_at": d["created_at"]})
    operations = [{"operation": op, "kind": _kind(op),
                   "series": [{"technology": tech, "points": sorted(points, key=lambda p: p["records"])} for tech, points in sorted(series.items())]}
                  for op, series in jobs.items()]
    operations.sort(key=lambda o: (("ingestion", "query", "aggregation").index(o["kind"]), o["operation"]))
    return {"sizes": sorted({d["record_count"] for d in docs}), "operations": operations}


@router.get("/benchmarks/scaling", summary="Benchmark results grouped for charts, real and synthetic kept apart")
def benchmark_scaling(db: Database = Depends(get_database)):
    """The newest timing of every job, technology and data size, in two separate sections: `synthetic` (generated data, the
    100,000 to 10,000,000 record ladder) and `real` (the market data in the database). `synthetic.runs` also lists the jobs
    that failed or were skipped (they have no timing) and the machine each size ran on."""
    docs = list(db["benchmark_results"].find({}, {"_id": 0}).sort("created_at", -1).limit(5000))
    runs = list(db["benchmark_runs"].find({"is_synthetic": True}, {"_id": 0}).sort([("records", 1), ("created_at", -1)]))
    newest_runs: dict = {}
    for r in runs:
        newest_runs.setdefault(r["records"], r)
    return {
        "synthetic": {**_scaling_view([d for d in docs if d.get("is_synthetic")]),
                      "runs": [{"run_id": r["run_id"], "records": r["records"], "created_at": r["created_at"], "dataset": r.get("dataset"),
                                "environment": r.get("environment"), "storage": r.get("storage"),
                                "not_measured": [o for o in r["outcomes"] if o["status"] != "ok"]} for r in newest_runs.values()]},
        "real": _scaling_view([d for d in docs if not d.get("is_synthetic")]),
        "notes": ["Synthetic results come from generated 1-minute bars in a separate database; real results are Phase 8 timings on the actual market data.",
                  "Timings are from one machine and one session: compare technologies with each other, not with other computers."]}


@router.get("/quality/reports", summary="Data-quality reports")
def quality_reports(dataset: Optional[str] = Query(None, max_length=120), include_issues: bool = False,
                    page: Page = Depends(page_params), db: Database = Depends(get_database)):
    query = {"dataset": dataset} if dataset else {}
    return paginate(db["data_quality_reports"], query, page, sort=[("created_at", -1)],
                    projection=None if include_issues else {"issues": 0})
