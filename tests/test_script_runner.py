"""run_pcbnew_script (MCP_ISSUES I3): subprocess isolation, timeout, atomic adopted save."""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from commands import script_runner as sr  # noqa: E402
from commands.script_runner import ScriptRunnerCommands  # noqa: E402

pytestmark = pytest.mark.unit

FAKE_CHILD = textwrap.dedent("""
    import json, os, sys, time
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    code = req["code"]
    res = {"ok": True, "stdout": "hello\\n", "stderr": "", "returnValue": {"n": 3}, "saved": False}
    if code == "sleep":
        time.sleep(30)
    if code == "fail":
        res = {"ok": False, "error": "ValueError: bad", "traceback": "Traceback..."}
    if code == "touch-board":
        with open(req["boardPath"], "a", encoding="utf-8") as fh:
            fh.write("\\n")
    if req["save"] and res["ok"]:
        with open(req["savePath"], "w", encoding="utf-8") as fh:
            fh.write("(kicad_pcb (version 2) (saved by script))\\n")
        res["saved"] = True
    json.dump(res, open(req["resultPath"], "w", encoding="utf-8"))
""")


class FakeIface:
    def __init__(self, board=None, dirty=False, backend="swig"):
        self.board = str(board) if board else None
        self.dirty = dirty
        self.session_backend = backend

    def _current_board_path(self):
        return self.board

    def _authoritative_board_path(self):
        return self.board

    def _dirty_state(self, path):
        return {"dirty": self.dirty}


@pytest.fixture
def board(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text("(kicad_pcb (version 1))\n", encoding="utf-8")
    return p


@pytest.fixture
def fake_child(tmp_path, monkeypatch):
    child = tmp_path / "fake_child.py"
    child.write_text(FAKE_CHILD, encoding="utf-8")
    monkeypatch.setattr(sr, "CHILD", child)
    return child


def test_runs_and_returns_json(board, fake_child):
    r = ScriptRunnerCommands(FakeIface(board)).run_pcbnew_script({"code": "x"})
    assert r["success"], r
    assert r["returnValue"] == {"n": 3}
    assert r["stdout"] == "hello\n"
    assert r["saved"] is False and "_boardFileWritten" not in r


def test_failure_reports_traceback(board, fake_child):
    r = ScriptRunnerCommands(FakeIface(board)).run_pcbnew_script({"code": "fail"})
    assert not r["success"]
    assert "ValueError" in r["message"] and r["traceback"]


def test_timeout_kills_child(board, fake_child):
    r = ScriptRunnerCommands(FakeIface(board)).run_pcbnew_script({"code": "sleep", "timeout": 1})
    assert not r["success"] and r["timedOut"]
    assert board.read_text(encoding="utf-8") == "(kicad_pcb (version 1))\n"


def test_save_replaces_board_and_asks_dispatcher_to_reload(board, fake_child):
    r = ScriptRunnerCommands(FakeIface(board)).run_pcbnew_script({"code": "x", "save": True})
    assert r["success"] and r["saved"], r
    assert "saved by script" in board.read_text(encoding="utf-8")
    assert r["_boardFileWritten"] == {"mode": "disk", "path": str(board.resolve())}
    assert not list(board.parent.glob("*.mcp-script-tmp"))


def test_save_refused_when_board_changed_during_run(board, fake_child):
    r = ScriptRunnerCommands(FakeIface(board)).run_pcbnew_script(
        {"code": "touch-board", "save": True}
    )
    assert not r["success"]
    assert "changed on disk" in r["message"]
    assert "saved by script" not in board.read_text(encoding="utf-8")


def test_save_guards(board, fake_child):
    dirty = ScriptRunnerCommands(FakeIface(board, dirty=True))
    r = dirty.run_pcbnew_script({"code": "x", "save": True})
    assert not r["success"] and "unsaved" in r["message"]
    assert dirty.run_pcbnew_script({"code": "x", "save": True, "discardUnsaved": True})["success"]
    ipc = ScriptRunnerCommands(FakeIface(board, backend="ipc"))
    r = ipc.run_pcbnew_script({"code": "x", "save": True})
    assert not r["success"] and "IPC" in r["message"]
    # Without save, neither guard applies.
    assert ipc.run_pcbnew_script({"code": "x"})["success"]


def test_argument_errors(board):
    cmds = ScriptRunnerCommands(FakeIface(None))
    assert not cmds.run_pcbnew_script({})["success"]
    assert "boardPath" in cmds.run_pcbnew_script({"code": "x"})["message"]
    r = cmds.run_pcbnew_script({"code": "x", "boardPath": str(board.with_name("no.kicad_pcb"))})
    assert "not found" in r["message"]


# --------------------------------------------------------------------------- #
# Real pcbnew in the child (skipped without KiCad)
# --------------------------------------------------------------------------- #


def _child_has_pcbnew() -> bool:
    try:
        code = "import pcbnew; pcbnew.BOARD"
        proc = subprocess.run(
            [sr._interpreter(), "-c", code, str(Path(sr.__file__).resolve().parents[1])],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _demo_board(tmp_path: Path):
    from utils.kicad_roots import kicad_install_roots

    for root in kicad_install_roots():
        demo = root / "share" / "kicad" / "demos" / "ecc83"
        if (demo / "ecc83-pp.kicad_pcb").is_file():
            for name in ("ecc83-pp.kicad_pcb", "ecc83-pp.kicad_pro"):
                shutil.copy(demo / name, tmp_path / name)
            return tmp_path / "ecc83-pp.kicad_pcb"
    return None


@pytest.mark.integration
def test_real_pcbnew_script_reads_moves_and_saves(tmp_path):
    board = _demo_board(tmp_path)
    if board is None or not _child_has_pcbnew():
        pytest.skip("KiCad demos or pcbnew for the script interpreter not available")
    cmds = ScriptRunnerCommands(FakeIface(board))
    r = cmds.run_pcbnew_script(
        {
            "code": (
                "fps = list(board.GetFootprints())\n"
                "print('count', len(fps))\n"
                "first = sorted(fps, key=lambda f: f.GetReference())[0]\n"
                "return {'n': len(fps), 'ref': first.GetReference(), 'fpid': fpid_of(first),"
                " 'layers': [zone_layers(z) for z in board.Zones()][:1]}\n"
            ),
            "timeout": 120,
        }
    )
    assert r["success"], r
    assert r["returnValue"]["n"] > 5
    assert ":" in r["returnValue"]["fpid"]
    assert r["stdout"].startswith("count")
    ref = r["returnValue"]["ref"]

    moved = cmds.run_pcbnew_script(
        {
            "code": (
                f"fp = footprint({ref!r})\n"
                "fp.SetPosition(pcbnew.VECTOR2I(mm(args['x']), mm(args['y'])))\n"
                "result = [to_mm(fp.GetPosition().x), to_mm(fp.GetPosition().y)]\n"
            ),
            "args": {"x": 12.5, "y": 34.25},
            "save": True,
            "timeout": 120,
        }
    )
    assert moved["success"] and moved["saved"], moved
    assert moved["returnValue"] == [12.5, 34.25]
    check = cmds.run_pcbnew_script(
        {"code": f"p = footprint({ref!r}).GetPosition()\nresult = [to_mm(p.x), to_mm(p.y)]\n"}
    )
    assert check["returnValue"] == [12.5, 34.25]
