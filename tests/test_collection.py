"""Phase 4 tests: downloaders. No network and no real Kaggle login are used."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.collection import kaggle_downloader as kg
from app.collection import yfinance_downloader as yfd
from app.collection.common import inventory, read_sidecar, sha256_file

INSTRUMENT = {"symbol": "RELIANCE", "exchange": "NSE", "instrument_type": "equity",
              "yfinance_symbol": "RELIANCE.NS"}


def fake_frame(rows: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2026-01-05", periods=rows, freq="D", tz="Asia/Kolkata", name="Date")
    return pd.DataFrame({"Open": 100.0, "High": 105.0, "Low": 99.0, "Close": 104.0, "Adj Close": 104.0,
                         "Volume": 1000, "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)


def no_sleep(_seconds):
    pass


def test_download_writes_csv_and_sidecar(tmp_path):
    res = yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=lambda *a, **k: fake_frame(),
                                  sleep=no_sleep)
    assert res["status"] == "downloaded" and res["rows"] == 3
    csv = tmp_path / "1d" / "RELIANCE_NSE.csv"
    meta = read_sidecar(csv)
    assert meta["source"] == "yfinance" and meta["data_class"] == "delayed"
    assert meta["rows"] == 3 and meta["is_synthetic"] is False
    assert meta["retrieved_at"].endswith("+00:00")            # UTC retrieval timestamp
    assert meta["sha256"] == sha256_file(csv)
    assert not list(tmp_path.rglob("*.part"))                  # no half-written files left


def test_second_run_skips_existing_unless_forced(tmp_path):
    calls = []

    def fetcher(*a, **k):
        calls.append(1)
        return fake_frame()

    yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=fetcher, sleep=no_sleep)
    again = yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=fetcher, sleep=no_sleep)
    assert again["status"] == "skipped" and len(calls) == 1
    forced = yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=fetcher, sleep=no_sleep, force=True)
    assert forced["status"] == "downloaded" and len(calls) == 2


def test_retries_then_succeeds(tmp_path):
    attempts = {"n": 0}
    waits = []

    def flaky(*a, **k):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("rate limited")
        return fake_frame()

    res = yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=flaky, sleep=waits.append, backoff=2.0)
    assert res["status"] == "downloaded" and attempts["n"] == 3
    assert waits == [2.0, 4.0]                                 # exponential back-off


def test_empty_response_fails_after_retries(tmp_path):
    with pytest.raises(yfd.NoDataError):
        yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=lambda *a, **k: pd.DataFrame(),
                                sleep=no_sleep, retries=2)
    assert not (tmp_path / "1d" / "RELIANCE_NSE.csv").exists()


def test_missing_ticker_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        yfd.download_instrument({"symbol": "X", "exchange": "NSE"}, "1d", tmp_path, sleep=no_sleep)


def test_download_many_continues_after_a_failure(tmp_path):
    bad = {**INSTRUMENT, "symbol": "BADCO", "yfinance_symbol": "BADCO.NS"}

    def fetcher(ticker, *a, **k):
        return fake_frame() if ticker == "RELIANCE.NS" else pd.DataFrame()

    out = yfd.download_many([bad, INSTRUMENT], "1d", tmp_path, fetcher=fetcher, retries=1,
                            sleep=no_sleep, progress=lambda m: None)
    assert [r["status"] for r in out] == ["failed", "downloaded"]


def test_verify_tickers_reports_ok_and_fail():
    def fetcher(ticker, *a, **k):
        return fake_frame() if ticker == "RELIANCE.NS" else pd.DataFrame()

    report = yfd.verify_tickers([INSTRUMENT, {**INSTRUMENT, "symbol": "BAD", "yfinance_symbol": "BAD.NS"}],
                                fetcher=fetcher, sleep=no_sleep)
    assert [r["ok"] for r in report] == [True, False]


def test_unsupported_interval_rejected():
    with pytest.raises(ValueError):
        yfd.fetch_history("RELIANCE.NS", "3m")


def test_inventory_lists_downloads(tmp_path):
    yfd.download_instrument(INSTRUMENT, "1d", tmp_path, fetcher=lambda *a, **k: fake_frame(), sleep=no_sleep)
    rows = inventory(tmp_path)
    assert len(rows) == 1 and rows[0]["path"].replace("\\", "/") == "1d/RELIANCE_NSE.csv"


# ---------------------------------------------------------------- Kaggle
class FakeKaggle:
    def __init__(self, license_name="CC0-1.0"):
        self.license_name = license_name

    def dataset_list_files(self, slug, page_token=None):
        files = [SimpleNamespace(name="nifty.csv", total_bytes=2048), SimpleNamespace(name="readme.txt", total_bytes=10)]
        return SimpleNamespace(files=files, next_page_token="")

    def dataset_download_files(self, slug, path, unzip, force, quiet):
        (pd.DataFrame({"a": [1, 2]})).to_csv(f"{path}/nifty.csv", index=False)
        (open(f"{path}/readme.txt", "w")).write("notes")

    def dataset_metadata(self, slug, path):
        with open(f"{path}/dataset-metadata.json", "w") as fh:
            json.dump({"licenses": [{"name": self.license_name}]}, fh)

    downloads: list = []

    def dataset_download_file(self, slug, file_name, path, force, quiet):
        """Single-file download: Kaggle sends 'nifty.csv' zipped, anything else as-is."""
        import zipfile
        FakeKaggle.downloads.append(file_name)
        content = "a,b\n1,2\n3,4\n5,6\n"
        if file_name == "nifty.csv":
            with zipfile.ZipFile(f"{path}/{file_name}.zip", "w") as zf:
                zf.writestr(file_name, content)
        else:
            with open(f"{path}/{file_name}", "w") as fh:
                fh.write(content)


def test_selective_download_fetches_only_requested_files_and_unzips(tmp_path):
    FakeKaggle.downloads = []
    res = kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path, files=["nifty.csv"])
    assert FakeKaggle.downloads == ["nifty.csv"] and res["files"] == ["nifty.csv"]
    folder = tmp_path / "owner__data"
    assert (folder / "nifty.csv").exists() and not (folder / "nifty.csv.zip").exists()   # unzipped, zip removed
    meta = read_sidecar(folder / "nifty.csv")
    assert meta["rows"] == 3 and meta["license"] == "CC0-1.0"                            # header not counted


def test_selective_download_resumes_and_force_redownloads(tmp_path):
    FakeKaggle.downloads = []
    kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path, files=["nifty.csv"])
    again = kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path, files=["nifty.csv"])
    assert again["skipped"] == ["nifty.csv"] and again["files"] == [] and FakeKaggle.downloads == ["nifty.csv"]
    kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path, files=["nifty.csv"], force=True)
    assert FakeKaggle.downloads == ["nifty.csv", "nifty.csv"]


class SpacedNames(FakeKaggle):
    """Like the real library: a file with a space in its name is saved URL-encoded."""
    CONTENT = b"a,b\n1,2\n3,4\n"          # bytes: text mode would turn \n into \r\n on Windows

    def dataset_list_files(self, slug, page_token=None):
        f = SimpleNamespace(name="NIFTY 50_minute.csv", total_bytes=len(self.CONTENT))
        return SimpleNamespace(files=[f], next_page_token="")

    def dataset_download_file(self, slug, file_name, path, force, quiet):
        import urllib.parse
        FakeKaggle.downloads.append(file_name)
        with open(f"{path}/{urllib.parse.quote(file_name)}", "wb") as fh:    # 'NIFTY%2050_minute.csv'
            fh.write(self.CONTENT)


def test_url_encoded_file_name_is_renamed_back(tmp_path):
    FakeKaggle.downloads = []
    res = kg.download_dataset("owner/data", api=SpacedNames(), out_root=tmp_path, files=["NIFTY 50_minute.csv"])
    folder = tmp_path / "owner__data"
    assert res["files"] == ["NIFTY 50_minute.csv"]
    assert (folder / "NIFTY 50_minute.csv").exists() and not (folder / "NIFTY%2050_minute.csv").exists()
    meta = read_sidecar(folder / "NIFTY 50_minute.csv")
    assert meta["rows"] == 2 and meta["size_matches_listing"] is True


def test_complete_leftover_file_is_reused_not_downloaded_again(tmp_path):
    """Simulates the interrupted run: the encoded file is on disk, complete, but has no sidecar."""
    FakeKaggle.downloads = []
    folder = tmp_path / "owner__data"
    folder.mkdir()
    (folder / "NIFTY%2050_minute.csv").write_bytes(SpacedNames.CONTENT)
    res = kg.download_dataset("owner/data", api=SpacedNames(), out_root=tmp_path, files=["NIFTY 50_minute.csv"])
    assert FakeKaggle.downloads == []                       # no second download
    assert res["files"] == ["NIFTY 50_minute.csv"] and (folder / "NIFTY 50_minute.csv").exists()


def test_wrong_size_file_is_downloaded_again(tmp_path):
    FakeKaggle.downloads = []
    folder = tmp_path / "owner__data"
    folder.mkdir()
    (folder / "NIFTY 50_minute.csv").write_text("a,b\n1")  # truncated
    kg.download_dataset("owner/data", api=SpacedNames(), out_root=tmp_path, files=["NIFTY 50_minute.csv"])
    assert FakeKaggle.downloads == ["NIFTY 50_minute.csv"]


@pytest.mark.parametrize("bad", ["../secret.csv", "sub/dir.csv", "..", "not_in_dataset.csv", ""])
def test_selective_download_rejects_bad_or_unknown_names(tmp_path, bad):
    with pytest.raises(ValueError):
        kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path, files=[bad])


def test_kaggle_list_files_does_not_download(tmp_path):
    files = kg.list_files("owner/data", api=FakeKaggle())
    assert files == [{"name": "nifty.csv", "size_bytes": 2048}, {"name": "readme.txt", "size_bytes": 10}]
    assert not list(tmp_path.iterdir())


def test_kaggle_download_records_license_in_sidecar(tmp_path):
    res = kg.download_dataset("owner/data", api=FakeKaggle(), out_root=tmp_path)
    assert res["license"] == "CC0-1.0" and res["files"] == ["nifty.csv"]   # txt file gets no sidecar
    meta = read_sidecar(tmp_path / "owner__data" / "nifty.csv")
    assert meta["source"] == "kaggle" and meta["license"] == "CC0-1.0"
    assert meta["dataset_url"] == "https://www.kaggle.com/datasets/owner/data"


def test_kaggle_license_read_from_nested_info_format(tmp_path):
    """The real Kaggle API returns {"info": {"licenses": [...]}}."""
    class Nested(FakeKaggle):
        def dataset_metadata(self, slug, path):
            with open(f"{path}/dataset-metadata.json", "w") as fh:
                json.dump({"info": {"licenses": [{"name": "CC0-1.0"}]}}, fh)

    res = kg.download_dataset("owner/data", api=Nested(), out_root=tmp_path)
    assert res["license"] == "CC0-1.0"


def test_kaggle_unknown_license_is_flagged(tmp_path):
    class NoMeta(FakeKaggle):
        def dataset_metadata(self, slug, path):
            raise RuntimeError("boom")

    res = kg.download_dataset("owner/data", api=NoMeta(), out_root=tmp_path)
    assert res["license"].startswith("unknown")


def test_missing_library_dependency_is_not_reported_as_bad_token(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "kaggle", None)     # makes `import kaggle` fail with ModuleNotFoundError
    with pytest.raises(kg.KaggleAuthError) as err:
        kg.get_api()
    assert "pip install" in str(err.value) and "token" not in str(err.value).lower()


@pytest.mark.parametrize("slug", ["../etc/passwd", "no-slash", "a/b/c", "owner/..", "ow ner/data", ""])
def test_kaggle_slug_validation(slug):
    with pytest.raises(ValueError):
        kg.validate_slug(slug)
