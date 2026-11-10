"""JLCPCB database swap survives open handles (MCP_ISSUES #1, WinError 32).

``download_jlcpcb_database`` downloaded the catalog fine but failed to replace
``jlcpcb_parts.db`` because the file was still held open. These tests pin the
fix:

* the manager's read paths (stats/search/part lookup) release the file handle
  on ``close()`` -- no lingering cursor keeps it alive;
* the converter builds into a temp file and swaps it in with ``os.replace``,
  retrying with backoff on ``PermissionError``;
* a lock that never clears leaves the old database intact, parks the new one
  at ``<db>.new``, and reports the cache path plus a recovery hint;
* the next manager open installs the parked database.
"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).resolve().parent.parent / "python"
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

from commands import jlcpcb_downloader, jlcpcb_parts  # noqa: E402
from commands.jlcpcb_parts import (  # noqa: E402
    DatabaseLockedError,
    JLCPCBPartsManager,
    replace_with_retry,
)

from tests.test_jlcpcb_downloader import _make_cdfer_like_source  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Backoff sleeps are real time; tests don't need to wait."""
    monkeypatch.setattr(jlcpcb_parts.time, "sleep", lambda s: None)


def _old_db(path: Path) -> None:
    mgr = JLCPCBPartsManager(db_path=str(path))
    mgr.conn.execute(
        "INSERT INTO components (lcsc, description, library_type, stock) "
        "VALUES ('C1', 'old part', 'Basic', 1)"
    )
    mgr.conn.commit()
    mgr.close()


def _lock_target(monkeypatch, target: Path, fail_times: int = 10**6):
    """Make os.replace onto ``target`` raise like Windows does for a held file."""
    real_replace = os.replace
    calls = {"n": 0}

    def _replace(src, dst):
        if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(target)):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise PermissionError(
                    32, "The process cannot access the file because it is being used", str(dst)
                )
        return real_replace(src, dst)

    monkeypatch.setattr(jlcpcb_parts.os, "replace", _replace)
    monkeypatch.setattr(jlcpcb_downloader.os, "replace", _replace)
    return calls


def test_manager_close_releases_handle_after_reads(tmp_path):
    """stats + search + lookup, then close(): the file must be replaceable."""
    db = tmp_path / "jlcpcb_parts.db"
    _old_db(db)
    mgr = JLCPCBPartsManager(db_path=str(db))
    assert mgr.get_database_stats()["total_parts"] == 1
    mgr.search_parts(query="old")
    mgr.get_part_info("C1")
    mgr.close()
    mgr.close()  # idempotent

    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).close()
    os.replace(other, db)  # WinError 32 here if a handle leaked
    assert not other.exists()


def test_replace_with_retry_recovers_from_transient_lock(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new")
    dst.write_text("old")
    calls = _lock_target(monkeypatch, dst, fail_times=2)
    replace_with_retry(src, dst)
    assert dst.read_text() == "new"
    assert calls["n"] == 3


def test_convert_keeps_old_db_and_parks_new_one_when_locked(tmp_path, monkeypatch):
    src = tmp_path / "cdfer.sqlite3"
    _make_cdfer_like_source(src)
    target = tmp_path / "jlcpcb_parts.db"
    _old_db(target)
    _lock_target(monkeypatch, target)

    with pytest.raises(DatabaseLockedError) as excinfo:
        jlcpcb_downloader.convert_source_sqlite(src, target)

    pending = Path(excinfo.value.pending)
    assert pending == tmp_path / "jlcpcb_parts.db.new"
    assert pending.exists()
    assert not (tmp_path / "jlcpcb_parts.db.tmp").exists()
    # The live database is untouched (old part still there).
    con = sqlite3.connect(str(target))
    try:
        assert con.execute("SELECT COUNT(*) FROM components").fetchone()[0] == 1
    finally:
        con.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-sharing semantics")
def test_real_open_handle_on_windows_is_reported_not_raised(tmp_path, monkeypatch):
    """A genuinely open sqlite handle (like a stale server's) blocks the swap."""
    src = tmp_path / "cdfer.sqlite3"
    _make_cdfer_like_source(src)
    target = tmp_path / "jlcpcb_parts.db"
    _old_db(target)
    holder = sqlite3.connect(str(target))
    holder.execute("SELECT COUNT(*) FROM components").fetchone()
    try:
        with pytest.raises(DatabaseLockedError):
            jlcpcb_downloader.convert_source_sqlite(src, target)
    finally:
        holder.close()
    # Once the holder is gone, the next manager open installs the parked DB.
    mgr = JLCPCBPartsManager(db_path=str(target))
    try:
        assert mgr.get_database_stats()["total_parts"] == 2
    finally:
        mgr.close()
    assert not (tmp_path / "jlcpcb_parts.db.new").exists()


def test_download_database_reports_cache_path_and_recovery_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        jlcpcb_downloader.PlatformHelper, "get_data_dir", staticmethod(lambda: tmp_path)
    )
    monkeypatch.setattr(jlcpcb_downloader, "find_processes_holding", lambda p: [])

    def _fake_cdfer(cache_dir, progress=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        s = cache_dir / "cdfer.sqlite3"
        _make_cdfer_like_source(s)
        return s, None

    monkeypatch.setattr(jlcpcb_downloader, "download_cdfer", _fake_cdfer)
    target = tmp_path / "jlcpcb_parts.db"
    _old_db(target)
    _lock_target(monkeypatch, target)

    result = jlcpcb_downloader.download_database(prefer_source="cdfer")

    assert result["success"] is False
    assert result["locked"] is True
    cache = tmp_path / "jlcparts_cache"
    assert result["cache_path"] == str(cache)
    assert str(cache) in result["message"]
    assert str(target) in result["message"]
    assert "stale" in result["message"]
    assert result["pending_db_path"] == str(tmp_path / "jlcpcb_parts.db.new")
    # Source cache is kept so a re-run doesn't download 400 MB again.
    assert (cache / "cdfer.sqlite3").exists()


def test_download_database_names_holding_process(tmp_path, monkeypatch):
    monkeypatch.setattr(
        jlcpcb_downloader.PlatformHelper, "get_data_dir", staticmethod(lambda: tmp_path)
    )
    monkeypatch.setattr(
        jlcpcb_downloader,
        "find_processes_holding",
        lambda p: [{"pid": 4242, "cmdline": "python.exe kicad_interface.py"}],
    )

    def _fake_cdfer(cache_dir, progress=None):
        cache_dir.mkdir(parents=True, exist_ok=True)
        s = cache_dir / "cdfer.sqlite3"
        _make_cdfer_like_source(s)
        return s, None

    monkeypatch.setattr(jlcpcb_downloader, "download_cdfer", _fake_cdfer)
    target = tmp_path / "jlcpcb_parts.db"
    _old_db(target)
    _lock_target(monkeypatch, target)

    result = jlcpcb_downloader.download_database(prefer_source="cdfer")
    assert "PID 4242" in result["message"]
    assert result["holders"][0]["pid"] == 4242


def test_manager_installs_pending_database_on_open(tmp_path):
    target = tmp_path / "jlcpcb_parts.db"
    _old_db(target)
    src = tmp_path / "cdfer.sqlite3"
    _make_cdfer_like_source(src)
    # Build a converted DB elsewhere and park it as the pending file.
    built = tmp_path / "built.db"
    jlcpcb_downloader.convert_source_sqlite(src, built)
    os.replace(built, tmp_path / "jlcpcb_parts.db.new")

    mgr = JLCPCBPartsManager(db_path=str(target))
    try:
        assert mgr.get_database_stats()["total_parts"] == 2
    finally:
        mgr.close()
    assert not (tmp_path / "jlcpcb_parts.db.new").exists()


def test_interface_handler_closes_connection_before_download(monkeypatch):
    """The handler must close its manager before download_database runs."""
    import kicad_interface

    events = []

    class _Mgr:
        def __init__(self, *a, **k):
            events.append("open")

        def get_database_stats(self):
            return {
                "total_parts": 0,
                "basic_parts": 0,
                "extended_parts": 0,
                "in_stock": 0,
                "db_path": "x",
            }

        def close(self):
            events.append("close")

    def _download(**kwargs):
        events.append("download")
        return {"success": False, "message": "nope"}

    monkeypatch.setattr(kicad_interface, "JLCPCBPartsManager", _Mgr)
    monkeypatch.setattr(jlcpcb_downloader, "download_database", _download)
    iface = kicad_interface.KiCADInterface.__new__(kicad_interface.KiCADInterface)
    iface.jlcpcb_parts = _Mgr()
    events.clear()
    result = iface._handle_download_jlcpcb_database({"force": True})
    assert result["success"] is False
    assert events[:2] == ["close", "download"]
    assert events[2] == "open"
