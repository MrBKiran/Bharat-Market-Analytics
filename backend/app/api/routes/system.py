"""System information: database statistics, performance benchmarks and data-quality reports (read-only)."""
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


@router.get("/quality/reports", summary="Data-quality reports")
def quality_reports(dataset: Optional[str] = Query(None, max_length=120), include_issues: bool = False,
                    page: Page = Depends(page_params), db: Database = Depends(get_database)):
    query = {"dataset": dataset} if dataset else {}
    return paginate(db["data_quality_reports"], query, page, sort=[("created_at", -1)],
                    projection=None if include_issues else {"issues": 0})
