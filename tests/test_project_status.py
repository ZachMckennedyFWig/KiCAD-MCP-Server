"""project_status: a compact re-orientation report (MCP_ISSUES I2).

Built from files on disk plus the DRC/ERC history that run_drc/run_erc record;
it must never re-run a check.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands import project_status as ps  # noqa: E402
from utils import check_summary  # noqa: E402

pytestmark = pytest.mark.unit


def _symbol(lib_id, ref, extra="", instances=None):
    inst = ""
    if instances:
        paths = "".join(f'(path "/root/{i}" (reference "{r}") (unit 1))' for i, r in instances)
        inst = f'(instances (project "p" {paths}))'
    return (
        f'  (symbol (lib_id "{lib_id}") (at 0 0 0) (unit 1) {extra}\n'
        f'    (property "Reference" "{ref}" (at 0 0 0))\n'
        f"    {inst}\n  )\n"
    )


def _project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "proj.kicad_pro").write_text("{}")
    sub = (
        "(kicad_sch (version 20250114)\n"
        + _symbol("Device:R", "R10", instances=[("a", "R10"), ("b", "R11")])
        + ")\n"
    )
    (root / "sub.kicad_sch").write_text(sub)
    main = (
        "(kicad_sch (version 20250114)\n"
        '  (lib_symbols (symbol "Device:R" (pin passive line)))\n'
        + _symbol("Device:R", "R1")
        + _symbol("Device:C", "C1", extra="(dnp yes)")
        + _symbol("Device:LED", "D1", extra="(on_board no)")
        + _symbol("power:GND", "#PWR01")
        + _symbol("Device:R", "R?")
        + '  (sheet (at 0 0) (size 10 10) (uuid "s1")\n'
        '    (property "Sheetname" "Sub" (at 0 0 0))\n'
        '    (property "Sheetfile" "sub.kicad_sch" (at 0 0 0))\n  )\n'
        ")\n"
    )
    (root / "proj.kicad_sch").write_text(main)
    pcb = (
        "(kicad_pcb (version 20241229)\n"
        '  (footprint "R:R_0603" (layer "F.Cu") (property "Reference" "R1" (at 0 0)))\n'
        '  (footprint "C:C_0603" (layer "F.Cu") (property "Reference" "C1" (at 0 0)))\n'
        '  (footprint "R:R_0603" (layer "F.Cu") (property "Reference" "R10" (at 0 0)))\n'
        '  (footprint "R:R_0603" (layer "F.Cu") (property "Reference" "R99" (at 0 0)))\n'
        '  (footprint "MH:Hole" (layer "F.Cu") (attr board_only) '
        '(property "Reference" "H1" (at 0 0)))\n'
        ")\n"
    )
    (root / "proj.kicad_pcb").write_text(pcb)
    return root


def test_inventory_and_parity_from_files(tmp_path):
    root = _project(tmp_path)
    status = ps.build_project_status(str(root), None)
    assert status["schematic"] == {"sheets": 2, "components": 5, "dnp": 1, "unannotated": 1}
    assert status["board"]["footprints"] == 5
    assert status["board"]["boardOnly"] == 1
    assert status["board"]["source"] == "file"
    # R11 (second instance of the reused sheet) is missing on the board; R99 has
    # no symbol. D1 is on_board=no and H1 is board-only, so neither counts.
    assert status["parity"]["missingOnBoard"] == ["R11"]
    assert status["parity"]["missingInSchematic"] == ["R99"]
    assert len(status["lines"]) <= 12
    assert "Parity: 1 not on board (R11); 1 not in schematic (R99)" in status["text"]
    assert "DRC: never run" in status["text"]
    assert "Last snapshot: none" in status["text"]


def test_reports_cached_checks_without_rerunning(tmp_path):
    root = _project(tmp_path)
    viol = [
        check_summary.normalize_violation(
            {"type": "clearance", "severity": "error", "description": "x", "items": []}
        ),
        check_summary.normalize_violation(
            {
                "type": "unconnected_items",
                "severity": "error",
                "description": "Missing connection",
                "items": [{"description": "Pad 1 of R1", "uuid": "u1"}],
            },
            "unconnected_items",
        ),
    ]
    check_summary.record_run("drc", str(root / "proj.kicad_pcb"), viol)
    check_summary.record_run("erc", str(root / "proj.kicad_sch"), viol[:1])

    with patch("subprocess.run", side_effect=AssertionError("must not run kicad-cli")):
        status = ps.build_project_status(str(root / "proj.kicad_pro"), None)

    assert status["lastDrc"]["total"] == 2
    assert status["lastErc"]["total"] == 1
    # Board not loaded: the unrouted count comes from the last DRC.
    assert status["board"]["unrouted"] == 1
    assert status["board"]["unroutedSource"] == "last_drc"
    assert "DRC: 2 (2 err, 0 warn)" in status["text"]


def test_in_memory_board_dirty_flags_and_snapshot(tmp_path):
    root = _project(tmp_path)
    snap = root / "snapshots" / "proj_snapshot_step1_ok_20260101_000000"
    snap.mkdir(parents=True)

    class _FP:
        def __init__(self, ref, board_only=False):
            self.ref, self.board_only = ref, board_only

        def GetReference(self):
            return self.ref

        def IsBoardOnly(self):
            return self.board_only

    class _Conn:
        def RecalculateRatsnest(self):
            pass

        def GetUnconnectedCount(self, visible_only):
            return 7

    board = SimpleNamespace(
        GetFootprints=lambda: [_FP("R1"), _FP("C1"), _FP("R10"), _FP("R11")],
        GetConnectivity=lambda: _Conn(),
    )
    status = ps.build_project_status(
        str(root),
        None,
        loaded_board=board,
        dirty={"dirty": True, "diskChangedExternally": False},
    )
    assert status["board"]["unrouted"] == 7 and status["board"]["source"] == "memory"
    assert status["parity"]["ok"] is True
    assert "Board in memory: UNSAVED changes" in status["text"]
    assert status["lastSnapshot"]["name"] == snap.name
    assert "Last snapshot: proj_snapshot_step1_ok" in status["text"]


def test_missing_project_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError):
        ps.build_project_status(None, None)


def test_handler_is_routed_and_uses_projectPath(tmp_path):
    root = _project(tmp_path)
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    iface.board = None
    iface.session_backend = None
    iface.session_board_path = None
    iface._last_auto_save_status = None
    res = iface._handle_project_status({"projectPath": str(root)})
    assert res["success"], res
    assert res["message"].startswith("Project: proj")

    no_project = iface._handle_project_status({})
    assert no_project["success"] is False
