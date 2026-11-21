"""Download Kaggle datasets into `data/raw/kaggle/<owner>__<dataset>/`.

Credentials (never stored in code or committed):
  * put `KAGGLE_API_TOKEN=...` (new token) or `KAGGLE_USERNAME=...` and
    `KAGGLE_KEY=...` (legacy token) in the project `.env`, or use
    `~/.kaggle/kaggle.json`. See Kaggle > Settings > API for the current method.

Each dataset has its OWN license. We read it from Kaggle and store it in the
sidecar of every file, so it is never lost.
"""
import json
import logging
import os
import re
import urllib.parse
import zipfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from app.collection.common import raw_dir, read_sidecar, utc_now_iso, write_sidecar
from app.core.config import BASE_DIR

logger = logging.getLogger(__name__)

SOURCE_ID = "kaggle"
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DATA_SUFFIXES = {".csv", ".xlsx", ".xls", ".parquet"}

AUTH_HELP = (
    "Kaggle sign-in failed. Create a token at https://www.kaggle.com/settings (API section), then add "
    "KAGGLE_API_TOKEN=<token> (or KAGGLE_USERNAME and KAGGLE_KEY) to the project .env file."
)


class KaggleAuthError(RuntimeError):
    pass


def get_api():
    """Return an authenticated Kaggle API client.

    `import kaggle` signs in as a side effect (and fails without credentials),
    so it is imported lazily here and any failure becomes a clear message.
    """
    load_dotenv(BASE_DIR / ".env", override=False)   # exports KAGGLE_* from .env into os.environ
    try:
        import kaggle
        return kaggle.api
    except ModuleNotFoundError as exc:               # a missing package is NOT a sign-in problem
        raise KaggleAuthError(
            f"The Kaggle library needs a Python package that is not installed ({exc.name}). "
            f"Run: .venv\\Scripts\\python -m pip install {exc.name}"
        ) from exc
    except (Exception, SystemExit) as exc:           # the library may call sys.exit()
        # Only the exception TYPE is shown: its message could echo credentials.
        raise KaggleAuthError(f"{AUTH_HELP} (cause: {type(exc).__name__})") from exc


def validate_slug(slug: str) -> str:
    if not SLUG_PATTERN.match(slug) or ".." in slug:
        raise ValueError(f"Invalid Kaggle dataset id {slug!r}; expected 'owner/dataset-name'")
    return slug


def list_files(slug: str, api=None) -> list[dict]:
    """Files in a dataset with sizes, WITHOUT downloading anything."""
    validate_slug(slug)
    api = api or get_api()
    files: list[dict] = []
    token = None
    while True:
        result = api.dataset_list_files(slug, page_token=token) if token else api.dataset_list_files(slug)
        for f in getattr(result, "files", None) or []:
            size = getattr(f, "total_bytes", None) or getattr(f, "size", None) or 0
            files.append({"name": f.name, "size_bytes": int(size)})
        token = getattr(result, "next_page_token", None) or None
        if not token:
            return files


def _read_license(api, slug: str, out_dir: Path) -> str:
    """License name from the dataset metadata, or a marker telling the user to check."""
    try:
        api.dataset_metadata(slug, str(out_dir))
        meta = json.loads((out_dir / "dataset-metadata.json").read_text(encoding="utf-8"))
        # Kaggle's API nests the data under "info"; older/CLI files have "licenses" at the top.
        licenses = (meta.get("info") or {}).get("licenses") or meta.get("licenses") or []
        if licenses and licenses[0].get("name"):
            return str(licenses[0]["name"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read license for %s: %s", slug, exc)
    return "unknown - check the dataset page on kaggle.com"


def _safe_file_name(name: str) -> str:
    """A dataset file name must be a plain name: no folders, no '..'."""
    if not name or name != Path(name).name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"Invalid file name {name!r}")
    return name


def _normalise_downloaded_name(out_dir: Path, name: str) -> None:
    """The Kaggle library saves files with URL-encoded names ('NIFTY 50.csv' -> 'NIFTY%2050.csv').
    Rename them back to the real dataset file name."""
    quoted = urllib.parse.quote(name)
    if quoted == name:
        return
    for suffix in ("", ".zip"):
        encoded = out_dir / (quoted + suffix)
        if encoded.exists():
            encoded.replace(out_dir / (name + suffix))


def _unzip_if_needed(out_dir: Path, name: str) -> None:
    """Kaggle may send a single file as '<name>.zip'. Extract only the expected member."""
    _normalise_downloaded_name(out_dir, name)
    zip_path = out_dir / (name + ".zip")
    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            if name not in zf.namelist():
                raise RuntimeError(f"{zip_path.name} does not contain {name}")
            zf.extract(name, out_dir)          # zipfile strips any '..' from member paths
        zip_path.unlink()
    if not (out_dir / name).exists():
        raise RuntimeError(f"Download finished but {name} is missing in {out_dir}")


def _count_csv_rows(path: Path) -> int:
    with open(path, "rb") as fh:
        lines = sum(1 for _ in fh)
    return max(lines - 1, 0)                     # minus the header line


def download_dataset(slug: str, api=None, out_root: Optional[Path] = None, force: bool = False,
                     files: Optional[list[str]] = None) -> dict:
    """Download a dataset (or only `files` from it) and write a sidecar for every data file.

    With `files`, only those files are fetched, and files that already have a sidecar are skipped
    (resume) unless `force=True`.
    """
    validate_slug(slug)
    api = api or get_api()
    owner, name = slug.split("/", 1)
    out_dir = (out_root or raw_dir("kaggle")) / f"{owner}__{name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    skipped: list[str] = []
    listed_sizes: dict[str, int] = {}
    if files:
        listed_sizes = {f["name"]: f["size_bytes"] for f in list_files(slug, api=api)}
        for fname in files:
            _safe_file_name(fname)
            if fname not in listed_sizes:
                raise ValueError(f"{fname!r} is not a file in {slug}")
        for fname in files:
            _normalise_downloaded_name(out_dir, fname)       # pick up a file from an interrupted run
            target = out_dir / fname
            if target.exists() and read_sidecar(target) and not force:
                skipped.append(fname)                        # finished earlier
                continue
            if target.exists() and target.stat().st_size == listed_sizes[fname] and not force:
                continue                                     # complete but no sidecar yet: reuse, do not re-download
            api.dataset_download_file(slug, fname, path=str(out_dir), force=force, quiet=False)
            _unzip_if_needed(out_dir, fname)
    else:
        api.dataset_download_files(slug, path=str(out_dir), unzip=True, force=force, quiet=False)

    license_name = _read_license(api, slug, out_dir)
    retrieved_at = utc_now_iso()

    written = []
    for f in sorted(out_dir.rglob("*")):
        if not (f.is_file() and f.suffix.lower() in DATA_SUFFIXES):
            continue
        if files and (f.name not in files or f.name in skipped):
            continue
        meta = {
            "source": SOURCE_ID,
            "data_class": "historical",
            "dataset": slug,
            "dataset_url": f"https://www.kaggle.com/datasets/{slug}",
            "license": license_name,
            "retrieved_at": retrieved_at,
            "is_synthetic": False,
        }
        if f.name in listed_sizes:
            meta["listed_size_bytes"] = listed_sizes[f.name]
            meta["size_matches_listing"] = f.stat().st_size == listed_sizes[f.name]
            if not meta["size_matches_listing"]:
                logger.warning("%s: size %d differs from Kaggle's listing %d",
                               f.name, f.stat().st_size, listed_sizes[f.name])
        if f.suffix.lower() == ".csv":
            meta["rows"] = _count_csv_rows(f)
        write_sidecar(f, meta)
        written.append(str(f.relative_to(out_dir)))
    return {"dataset": slug, "license": license_name, "directory": str(out_dir),
            "files": written, "skipped": skipped}


def has_credentials() -> bool:
    """True if some Kaggle credential is configured (does not validate it, prints nothing)."""
    load_dotenv(BASE_DIR / ".env", override=False)
    if os.environ.get("KAGGLE_API_TOKEN") or (os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY")):
        return True
    return (Path.home() / ".kaggle" / "kaggle.json").exists()
