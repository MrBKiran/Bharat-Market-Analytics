"""Create / update BharatMarketDB: collections, validators, indexes and seed data.

Safe to run any number of times (idempotent). It never drops data:
  * missing collections are created,
  * existing regular collections get their validator refreshed,
  * indexes are created if absent,
  * seed rows are upserted without overwriting values you changed.
"""
import logging
from datetime import datetime, timezone

from pymongo import UpdateOne
from pymongo.database import Database

from app.db.schemas import COLLECTIONS, OBSOLETE_INDEXES, CollectionSpec
from app.db.seed_data import INSTRUMENTS, SOURCES

logger = logging.getLogger(__name__)


def _collection_options(db: Database, name: str) -> dict:
    found = list(db.list_collections(filter={"name": name}))
    return found[0].get("options", {}) if found else {}


def _ensure_collection(db: Database, spec: CollectionSpec, existing: set[str], report: dict) -> None:
    if spec.name not in existing:
        kwargs: dict = {}
        if spec.timeseries:
            kwargs["timeseries"] = spec.timeseries
        if spec.validator:
            kwargs.update(validator=spec.validator, validationLevel="strict", validationAction="error")
        db.create_collection(spec.name, **kwargs)
        report["created"].append(spec.name)
        logger.info("Created collection %s", spec.name)
        return

    if spec.validator:
        # Refresh the validator so schema changes in code reach existing databases.
        db.command("collMod", spec.name, validator=spec.validator,
                   validationLevel="strict", validationAction="error")
        report["validators_refreshed"].append(spec.name)

    if spec.timeseries:
        actual = _collection_options(db, spec.name).get("timeseries", {})
        if actual.get("timeField") != spec.timeseries["timeField"]:
            msg = f"{spec.name} exists but is not the expected time-series collection"
            report["warnings"].append(msg)
            logger.warning(msg)


def _drop_obsolete_indexes(db: Database, collection: str, report: dict) -> None:
    present = set(db[collection].index_information())
    for coll_name, index_name in OBSOLETE_INDEXES:
        if coll_name == collection and index_name in present:
            db[collection].drop_index(index_name)
            report.setdefault("indexes_dropped", []).append(f"{collection}.{index_name}")
            logger.info("Dropped obsolete index %s.%s", collection, index_name)


def _ensure_indexes(db: Database, spec: CollectionSpec, report: dict) -> None:
    coll = db[spec.name]
    for idx in spec.indexes:
        coll.create_index(list(idx.keys), name=idx.name, unique=idx.unique)
    report["indexes"][spec.name] = [i.name for i in spec.indexes]


def _seed(db: Database, report: dict) -> None:
    now = datetime.now(timezone.utc)

    source_ops = []
    for s in SOURCES:
        static = {k: v for k, v in s.items() if k != "source_id"}
        source_ops.append(UpdateOne(
            {"source_id": s["source_id"]},
            {"$set": static, "$setOnInsert": {"active": True, "created_at": now}},
            upsert=True,
        ))
    res = db["market_sources"].bulk_write(source_ops, ordered=False)
    report["seeded"]["market_sources"] = {"inserted": res.upserted_count, "existing": res.matched_count}

    inst_ops = []
    for i in INSTRUMENTS:
        key = {"exchange": i["exchange"], "symbol": i["symbol"], "instrument_type": i["instrument_type"]}
        static = {k: v for k, v in i.items() if k not in key}
        inst_ops.append(UpdateOne(
            key,
            {"$set": static, "$setOnInsert": {"active": True, "created_at": now}},
            upsert=True,
        ))
    res = db["instruments"].bulk_write(inst_ops, ordered=False)
    report["seeded"]["instruments"] = {"inserted": res.upserted_count, "existing": res.matched_count}


def setup_database(db: Database, seed: bool = True) -> dict:
    """Create everything in `db`. Returns a report of what happened."""
    report: dict = {
        "database": db.name,
        "created": [],
        "validators_refreshed": [],
        "indexes": {},
        "seeded": {},
        "warnings": [],
    }
    existing = set(db.list_collection_names())
    for spec in COLLECTIONS:
        _ensure_collection(db, spec, existing, report)
        _drop_obsolete_indexes(db, spec.name, report)
        _ensure_indexes(db, spec, report)
    if seed:
        _seed(db, report)
    return report
