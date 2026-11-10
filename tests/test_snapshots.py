"""Snapshots / undo (MCP_ISSUES I1): list, restore, diff, automatic snapshots."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bom_fixtures import make_project  # noqa: E402
from commands import snapshots as snap  # noqa: E402
from commands.bom_tools import apply_property_map  # noqa: E402
from commands.snapshots import (  # noqa: E402
    SnapshotCommands,
    create_snapshot,
    maybe_auto_snapshot,
)

pytestmark = pytest.mark.unit

BOARD = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(footprint "Resistor_SMD:R_0603_1608Metric"
\t\t(layer "F.Cu")
\t\t(uuid "f1")
\t\t(at {r1x} 10 90)
\t\t(property "Reference" "R1"
\t\t\t(at 0 0 0)
\t\t\t(layer "F.SilkS")
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 0 0 0)
\t\t\t(layer "F.Fab")
\t\t)
\t)
{extra_fp}\t(segment
\t\t(start 0 0)
\t\t(end 3 4)
\t\t(width 0.2)
\t\t(layer "F.Cu")
\t\t(net "GND")
\t\t(uuid "s1")
\t)
{extra_track}\t(via
\t\t(at 1 1)
\t\t(size 0.6)
\t\t(drill 0.3)
\t\t(layers "F.Cu" "B.Cu")
\t\t(net "GND")
\t\t(uuid "v1")
\t)
\t(zone
\t\t(net "GND")
\t\t(layer "B.Cu")
\t\t(uuid "z1")
\t\t(name "gnd")
\t\t(polygon
\t\t\t(pts
\t\t\t\t(xy 0 0) (xy {zx} 0) (xy 10 10)
\t\t\t)
\t\t)
\t)
)
"""

C1_FP = (
    '\t(footprint "Capacitor_SMD:C_0402_1005Metric"\n\t\t(layer "B.Cu")\n\t\t(uuid "f2")\n'
    '\t\t(at 5 5)\n\t\t(property "Reference" "C1"\n\t\t\t(at 0 0 0)\n\t\t)\n\t)\n'
)
TRACK2 = (
    '\t(segment\n\t\t(start 0 0)\n\t\t(end 0 10)\n\t\t(width 0.2)\n\t\t(layer "B.Cu")\n'
    '\t\t(net "+3V3")\n\t\t(uuid "s2")\n\t)\n'
)


def board_text(r1x=10, extra_fp="", extra_track="", zx=10):
    return BOARD.format(r1x=r1x, extra_fp=extra_fp, extra_track=extra_track, zx=zx)


@pytest.fixture
def project(tmp_path):
    make_project(tmp_path)
    (tmp_path / "proj.kicad_pcb").write_text(board_text(), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a design file", encoding="utf-8")
    return tmp_path


class FakeIface:
    _BOARD_MUTATING_COMMANDS = {"move_component"}
    _SCHEMATIC_MUTATING_COMMANDS = {"apply_property_map", "add_schematic_wire"}

    def __init__(self, board=None, dirty=False):
        self.board_path = str(board) if board else None
        self.dirty = dirty
        self.session_backend = "swig"

    def _current_board_path(self):
        return self.board_path

    def _authoritative_board_path(self):
        return self.board_path

    def _dirty_state(self, path):
        return {"dirty": self.dirty}

    @staticmethod
    def _schematic_write_targets(command, params):
        if params.get("dryRun"):
            return []
        return [params["schematicPath"]] if params.get("schematicPath") else []


def test_create_and_list(project):
    m = create_snapshot(project, label="first")
    assert set(f["path"] for f in m["files"]) == {
        "proj.kicad_pcb",
        "proj.kicad_pro",
        "proj.kicad_sch",
        "power.kicad_sch",
    }
    r = SnapshotCommands().list_snapshots({"projectPath": str(project)})
    assert r["success"] and r["count"] == 1
    assert r["snapshots"][0]["label"] == "first"
    assert r["snapshots"][0]["files"] == 4


def test_restore_round_trip_with_safety_snapshot(project):
    original = (project / "proj.kicad_pcb").read_bytes()
    create_snapshot(project, label="good")
    (project / "proj.kicad_pcb").write_text(board_text(r1x=20), encoding="utf-8")
    cmds = SnapshotCommands()
    dry = cmds.restore_snapshot({"projectPath": str(project), "snapshot": "good", "dryRun": True})
    assert dry["success"] and dry["changedFiles"] == 1
    assert (project / "proj.kicad_pcb").read_bytes() != original
    r = cmds.restore_snapshot({"projectPath": str(project), "snapshot": "good"})
    assert r["success"], r
    assert (project / "proj.kicad_pcb").read_bytes() == original
    assert r["safetySnapshot"].startswith("safety_")
    # The safety snapshot can undo the restore.
    r2 = cmds.restore_snapshot({"projectPath": str(project), "snapshot": r["safetySnapshot"]})
    assert r2["success"]
    assert "20 10 90" in (project / "proj.kicad_pcb").read_text(encoding="utf-8")


def test_restore_reloads_loaded_board_and_guards_unsaved(project):
    create_snapshot(project, label="good")
    (project / "proj.kicad_pcb").write_text(board_text(r1x=20), encoding="utf-8")
    board = project / "proj.kicad_pcb"
    refused = SnapshotCommands(FakeIface(board, dirty=True)).restore_snapshot({"snapshot": "good"})
    assert not refused["success"] and "unsaved" in refused["message"]
    r = SnapshotCommands(FakeIface(board, dirty=True)).restore_snapshot(
        {"snapshot": "good", "force": True}
    )
    assert r["success"]
    assert r["_boardFileWritten"] == {"mode": "disk", "path": str(board)}


def test_legacy_full_folder_snapshot_is_listed_and_restorable(project):
    legacy = project / "snapshots" / "proj_snapshot_step1_20260101_120000"
    legacy.mkdir(parents=True)
    for name in ("proj.kicad_pcb", "proj.kicad_sch", "power.kicad_sch", "notes.txt"):
        (legacy / name).write_bytes((project / name).read_bytes())
    (legacy / "backup").mkdir()
    (legacy / "backup" / "old.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    (project / "power.kicad_sch").write_text("(kicad_sch broken)", encoding="utf-8")
    cmds = SnapshotCommands()
    listed = cmds.list_snapshots({"projectPath": str(project)})
    assert listed["snapshots"][0]["kind"] == "legacy"
    r = cmds.restore_snapshot({"projectPath": str(project), "snapshot": "step1"})
    assert r["success"], r
    paths = {f["path"]: f["action"] for f in r["files"]}
    assert paths["power.kicad_sch"] == "overwrite"
    assert "backup/old.kicad_sch" not in paths and "notes.txt" not in paths
    assert "power.kicad_sch" in r["message"] or r["changedFiles"] == 1


def test_diff_board_and_schematic(project):
    create_snapshot(project, label="before")
    (project / "proj.kicad_pcb").write_text(
        board_text(r1x=12.5, extra_fp=C1_FP, extra_track=TRACK2, zx=12), encoding="utf-8"
    )
    apply_property_map(
        {
            "schematicPath": str(project / "proj.kicad_sch"),
            "rules": [{"match": {"value": "10k"}, "set": {"LCSC": "C25804", "dnp": True}}],
        }
    )
    r = SnapshotCommands().diff_snapshots({"projectPath": str(project), "a": "before"})
    assert r["success"], r
    assert r["b"] == "current"
    board = r["board"]
    assert board["footprints"]["added"] == ["C1"]
    moved = board["footprints"]["moved"]
    assert moved[0]["ref"] == "R1" and moved[0]["dx"] == 2.5
    assert board["tracks"]["delta"] == 1
    assert board["tracks"]["byNet"][0]["net"] == "+3V3"
    assert board["vias"]["delta"] == 0
    assert board["zones"]["changed"] == ["zone gnd [B.Cu] p0"]
    changed = {c["ref"]: c["fields"] for c in r["schematic"]["symbols"]["changed"]}
    assert changed["R1"]["LCSC"] == [None, "C25804"]
    assert changed["R2"]["(dnp)"] == ["no", "yes"]
    assert "footprints +1 -0 moved 1" in r["message"]


def test_auto_snapshot_is_opt_in_and_pruned(project, monkeypatch):
    iface = FakeIface(project / "proj.kicad_pcb")
    monkeypatch.delenv(snap.AUTO_ENV, raising=False)
    assert maybe_auto_snapshot(iface, "move_component", {}) is None
    monkeypatch.setenv(snap.AUTO_ENV, "1")
    monkeypatch.setenv(snap.AUTO_KEEP_ENV, "2")
    ids = [maybe_auto_snapshot(iface, "move_component", {}) for _ in range(3)]
    assert all(ids)
    autos = sorted(p.name for p in (project / "snapshots").iterdir())
    assert autos == sorted(ids[1:])
    manifest = json.loads((project / "snapshots" / ids[-1] / "snapshot.json").read_text())
    assert [f["path"] for f in manifest["files"]] == ["proj.kicad_pcb"]
    # Read-only and dry-run calls take no snapshot.
    assert maybe_auto_snapshot(iface, "get_board_info", {}) is None
    sch = str(project / "proj.kicad_sch")
    assert (
        maybe_auto_snapshot(iface, "add_schematic_wire", {"schematicPath": sch, "dryRun": 1})
        is None
    )
    # A hierarchy-wide tool snapshots every sheet.
    sid = maybe_auto_snapshot(iface, "apply_property_map", {"schematicPath": sch})
    files = json.loads((project / "snapshots" / sid / "snapshot.json").read_text())["files"]
    assert {f["path"] for f in files} == {"proj.kicad_sch", "power.kicad_sch"}


def test_auto_snapshot_never_raises(project, monkeypatch):
    monkeypatch.setenv(snap.AUTO_ENV, "1")

    class Broken(FakeIface):
        def _current_board_path(self):
            raise RuntimeError("boom")

    assert maybe_auto_snapshot(Broken(), "move_component", {}) is None


def test_dispatcher_hook_records_auto_snapshot(project, monkeypatch):
    monkeypatch.setenv(snap.AUTO_ENV, "1")
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
    r = iface.handle_command(
        "apply_property_map",
        {
            "schematicPath": str(project / "proj.kicad_sch"),
            "rules": [{"match": {"value": "10k"}, "set": {"LCSC": "C25804"}}],
        },
    )
    assert r["success"], r
    assert r["autoSnapshot"].startswith("auto_")
    listed = iface.handle_command("list_snapshots", {"projectPath": str(project)})
    assert listed["snapshots"][0]["id"] == r["autoSnapshot"]
    restored = iface.handle_command(
        "restore_snapshot", {"projectPath": str(project), "snapshot": r["autoSnapshot"]}
    )
    assert restored["success"], restored
    assert "C25804" not in (project / "proj.kicad_sch").read_text(encoding="utf-8")


def test_errors(project):
    cmds = SnapshotCommands()
    assert not cmds.list_snapshots({})["success"]
    assert not cmds.restore_snapshot({"projectPath": str(project)})["success"]
    r = cmds.restore_snapshot({"projectPath": str(project), "snapshot": "nope"})
    assert not r["success"] and "No snapshots" in r["message"]
    assert not cmds.diff_snapshots({"projectPath": str(project)})["success"]
