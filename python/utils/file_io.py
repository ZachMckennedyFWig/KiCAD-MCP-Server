"""File I/O helpers for KiCad text files."""

from __future__ import annotations

import os
from pathlib import Path


def read_text_preserve_newline(path: Path) -> tuple[str, str]:
    """Read UTF-8 text as LF internally and return the original newline style."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def write_text_atomic(path: Path, text: str, newline: str) -> None:
    """Atomically write UTF-8 text while preserving the caller's newline style."""
    tmp = path.with_name(path.name + ".mcp-tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically replace *path* with *data* (temp file in the same dir + ``os.replace``)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".mcp-tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class FileTransaction:
    """Snapshot a file and restore it byte-for-byte if the guarded block raises.

    A mutation that fails must leave the file exactly as it was: no orphan
    symbol instance, no half-injected ``lib_symbols`` entry (#5c)::

        with FileTransaction(sch_path):
            step_that_writes(sch_path)
            step_that_may_raise(sch_path)

    ``rollback()`` restores the snapshot explicitly and is a no-op while the
    file still matches it. A file that did not exist beforehand is removed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.snapshot: bytes | None = self.path.read_bytes()
        except FileNotFoundError:
            self.snapshot = None

    def _current(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def rollback(self) -> None:
        if self._current() == self.snapshot:
            return
        if self.snapshot is None:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        else:
            write_bytes_atomic(self.path, self.snapshot)

    def __enter__(self) -> "FileTransaction":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.rollback()
        return False


def _utf8_open(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
    """``open`` that defaults text-mode I/O to UTF-8 instead of the locale codec."""
    if encoding is None and "b" not in mode:
        encoding = "utf-8"
    return open(file, mode, buffering, encoding, *args, **kwargs)


def patch_kicad_skip_utf8() -> bool:
    """Make kicad-skip read and write schematics as UTF-8 (#2).

    ``skip.sexp.util.loadTree`` / ``writeTree`` call a bare ``open(path, "r")``
    / ``open(path, "w")``, which uses the locale codec: cp1252 on Windows. KiCad
    files are UTF-8 by specification, so non-ASCII text in a sheet (an
    easyeda2kicad ``"Manufacturer" "XKB Connection(中国星坤)"`` field) made every
    skip-based call on that sheet fail with ``UnicodeDecodeError: 'charmap'
    codec can't decode byte 0x9d``.

    Both functions resolve ``open`` through their module globals, so shadowing
    it there fixes them however they were imported. Idempotent; returns False
    when kicad-skip is not installed.
    """
    try:
        from skip.sexp import util as skip_util  # type: ignore
    except Exception:
        return False
    if getattr(skip_util, "open", None) is not _utf8_open:
        skip_util.open = _utf8_open  # type: ignore[attr-defined]
    return True
