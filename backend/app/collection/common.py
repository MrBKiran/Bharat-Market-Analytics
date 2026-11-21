"""Shared helpers for data collection: raw-data folders and metadata sidecars.

Every file that lands in `data/raw/` gets a `<file>.meta.json` sidecar that
records where it came from and when, so the importer (Phase 5) can store
`source` and `retrieved_at` for every dataset.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.config import get_settings


def raw_dir(*parts: str) -> Path:
    """`data/raw/<parts...>` (created if missing)."""
    path = get_settings().data_dir / "raw"
    for part in parts:
        path = path / part
    path.mkdir(parents=True, exist_ok=True)
    return path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def sidecar_path(data_file: Path) -> Path:
    return data_file.with_name(data_file.name + ".meta.json")


def write_sidecar(data_file: Path, meta: dict) -> Path:
    """Write metadata next to `data_file`, adding file name, size and SHA-256."""
    record = {
        **meta,
        "file": data_file.name,
        "size_bytes": data_file.stat().st_size,
        "sha256": sha256_file(data_file),
    }
    target = sidecar_path(data_file)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    tmp.replace(target)
    return target


def read_sidecar(data_file: Path) -> Optional[dict]:
    target = sidecar_path(data_file)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def inventory(root: Optional[Path] = None) -> list[dict]:
    """List every raw file that has a sidecar (what we have downloaded so far)."""
    root = root or (get_settings().data_dir / "raw")
    rows = []
    for meta_file in sorted(root.rglob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta["path"] = str(meta_file.with_name(meta_file.name[: -len(".meta.json")]).relative_to(root))
        rows.append(meta)
    return rows
