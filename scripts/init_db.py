"""Create or update BharatMarketDB (collections, validators, indexes, seed data).

Run from the project root:
    .venv\\Scripts\\python scripts\\init_db.py            # create/update + seed
    .venv\\Scripts\\python scripts\\init_db.py --no-seed  # skip seed data

Safe to run repeatedly. It never deletes data.
"""
import argparse
import sys
from pathlib import Path

# Make `import app...` work when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.db.mongo import check_connection, get_db  # noqa: E402
from app.db.setup import setup_database  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up BharatMarketDB")
    parser.add_argument("--no-seed", action="store_true", help="do not insert sources and instruments")
    args = parser.parse_args()

    settings = get_settings()
    info = check_connection()
    if not info["connected"]:
        print(f"ERROR: cannot reach MongoDB ({info.get('error')}). Is the MongoDB service running?")
        return 1
    print(f"Connected to MongoDB {info['server_version']}; database: {settings.mongo_db_name}\n")

    report = setup_database(get_db(), seed=not args.no_seed)

    print(f"Collections created ({len(report['created'])}): {', '.join(report['created']) or 'none (already existed)'}")
    print(f"Validators refreshed: {len(report['validators_refreshed'])}")
    n_idx = sum(len(v) for v in report["indexes"].values())
    print(f"Indexes ensured: {n_idx} across {len(report['indexes'])} collections")
    for name, counts in report["seeded"].items():
        print(f"Seed {name}: {counts['inserted']} inserted, {counts['existing']} already present")
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
