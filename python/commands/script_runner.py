"""``run_pcbnew_script``: the scripting escape hatch (MCP_ISSUES I3).

Runs caller-supplied Python against a fresh disk load of the board in a
**separate process** (``pcbnew_script_child.py``) so the server's own board,
SWIG state and event loop are never exposed to it, and a hang is ended by a
hard timeout. The script gets helpers for the KiCad 10 SWIG traps of #5r (see
the child module's docstring).

With ``save``, the child saves into a temporary file; the server then checks
that the board on disk is still the version the script started from, swaps
the new file in with ``os.replace`` and reports ``_boardFileWritten`` so the
dispatcher reloads its in-memory board from it (#5h / #5n).

This executes arbitrary code with the server's permissions. It is excluded
from the readonly and manufacturing operating modes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("kicad_interface")

CHILD = Path(__file__).with_name("pcbnew_script_child.py")
PYTHON_ENV = "KICAD_MCP_SCRIPT_PYTHON"
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 1800


def _digest(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _interpreter() -> str:
    return os.environ.get(PYTHON_ENV) or sys.executable


def _loaded_pcbnew_dir() -> Optional[str]:
    """Directory of the pcbnew the server itself imported, so the child gets the same one."""
    module = sys.modules.get("pcbnew")
    path = getattr(module, "__file__", None)
    if isinstance(path, str) and os.path.isfile(path):
        return os.path.dirname(path)
    return None


class ScriptRunnerCommands:
    def __init__(self, interface: Any = None):
        self.iface = interface

    def _loaded_board(self) -> Optional[str]:
        if self.iface is None:
            return None
        try:
            return self.iface._current_board_path()
        except Exception:
            return None

    def run_pcbnew_script(self, params: Dict[str, Any]) -> Dict[str, Any]:
        code = params.get("code")
        if not isinstance(code, str) or not code.strip():
            return {"success": False, "message": "code is required"}
        board_arg = params.get("boardPath")
        loaded = self._loaded_board()
        board = (
            board_arg
            or loaded
            or (self.iface._authoritative_board_path() if self.iface is not None else None)
        )
        if not board:
            return {"success": False, "message": "boardPath is required (no board is loaded)"}
        board_path = Path(board).expanduser().resolve()
        if not board_path.is_file():
            return {"success": False, "message": f"Board not found: {board_path}"}
        save = bool(params.get("save", False))
        try:
            timeout = max(1, min(int(params.get("timeout", DEFAULT_TIMEOUT)), MAX_TIMEOUT))
        except (TypeError, ValueError):
            return {"success": False, "message": "timeout must be a number of seconds"}
        preload = params.get("preloadFootprints") or []
        if not isinstance(preload, list):
            return {"success": False, "message": "preloadFootprints must be a list"}
        script_args = params.get("args") or {}

        same_as_loaded = bool(
            loaded
            and os.path.normcase(str(Path(loaded).resolve())) == os.path.normcase(str(board_path))
        )
        if save and self.iface is not None:
            if getattr(self.iface, "session_backend", None) == "ipc" and same_as_loaded:
                return {
                    "success": False,
                    "message": (
                        "The board is owned by the live KiCad GUI (IPC session); a script save "
                        "would be overwritten by it. Close it in KiCad or run without save."
                    ),
                }
            if same_as_loaded and not params.get("discardUnsaved"):
                dirty = self.iface._dirty_state(str(board_path)).get("dirty")
                if dirty:
                    return {
                        "success": False,
                        "message": (
                            "The loaded board has unsaved in-memory changes; the script runs on "
                            "the file on disk and saving would discard them. Run save_project "
                            "first or pass discardUnsaved=true."
                        ),
                    }

        before = _digest(board_path)
        work = Path(tempfile.mkdtemp(prefix="kicad_mcp_script_"))
        started = time.time()
        try:
            request = {
                "code": code,
                "boardPath": str(board_path),
                "save": save,
                "savePath": str(work / board_path.name),
                "resultPath": str(work / "result.json"),
                "args": script_args,
                "preloadFootprints": [str(p) for p in preload],
                "sysPath": [str(Path(__file__).resolve().parents[1])]
                + [p for p in sys.path if p and os.path.isdir(p)],
                "pcbnewDir": _loaded_pcbnew_dir(),
            }
            req_file = work / "request.json"
            req_file.write_text(json.dumps(request), encoding="utf-8")
            cmd = [_interpreter(), str(CHILD), str(req_file)]
            try:
                proc = subprocess.run(
                    cmd, cwd=str(work), capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "success": False,
                    "message": f"Script timed out after {timeout}s and was killed; nothing saved",
                    "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                    "timedOut": True,
                }
            result_file = work / "result.json"
            if not result_file.is_file():
                return {
                    "success": False,
                    "message": f"Script process exited ({proc.returncode}) without a result",
                    "processStderr": (proc.stderr or "")[-4000:],
                }
            child = json.loads(result_file.read_text(encoding="utf-8"))
            out: Dict[str, Any] = {
                "success": bool(child.get("ok")),
                "boardPath": str(board_path),
                "stdout": child.get("stdout", ""),
                "stderr": child.get("stderr", ""),
                "returnValue": child.get("returnValue"),
                "saved": False,
                "durationSec": round(time.time() - started, 2),
                "timing": child.get("timing"),
                "interpreter": cmd[0],
            }
            for key in ("error", "traceback", "preloadErrors"):
                if child.get(key):
                    out[key] = child[key]
            if not child.get("ok"):
                out["message"] = f"Script failed: {child.get('error', 'unknown error')}"
                return out
            if save:
                if not child.get("saved"):
                    out["success"] = False
                    out["message"] = child.get("error") or "Script ran but the board was not saved"
                    return out
                if _digest(board_path) != before:
                    out["success"] = False
                    out["message"] = (
                        "The board file changed on disk while the script ran; the script's "
                        "result was not written. Re-run it."
                    )
                    return out
                staged = board_path.with_name(board_path.name + ".mcp-script-tmp")
                shutil.copyfile(work / board_path.name, staged)
                os.replace(staged, board_path)
                out["saved"] = True
                out["_boardFileWritten"] = {"mode": "disk", "path": str(board_path)}
                out["message"] = f"Script ran and saved {board_path.name}"
            else:
                out["message"] = "Script ran (board not saved)"
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)
