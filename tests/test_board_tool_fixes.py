"""Unit tests for the board-side fixes in MCP_ISSUES #5h-#5q.

* #5h / #5n -- a tool that writes the ``.kicad_pcb`` itself (sync, autoroute,
  SES import, set_board_origin, ...) must not trip the auto-save guard with
  "changed externally" afterwards.
* #5h / B1 -- sync_schematic_to_board reports (and swaps) footprints whose
  schematic Footprint field changed.
* #5i -- replace_component is routed and keeps placement and nets.
* #5k / #5q -- create_netclass persists netclass_patterns, works without
  NETINFO_ITEM.SetClass, and warns on patterns that match no net.
* #5l -- export_dsn falls back to the saved file and explains failures.

Real-SWIG coverage is in ``test_board_tool_fixes_real.py``.
"""

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

pytestmark = pytest.mark.unit

import kicad_interface  # noqa: E402
from commands.freerouting import FreeroutingCommands  # noqa: E402
from commands.routing import (  # noqa: E402
    RoutingCommands,
    apply_netclass_patterns_to_project_settings,
    match_netclass_pattern,
    netclass_via_problem,
    persist_netclass_to_project,
    suggest_net_names,
    zone_points_nm,
)
from kicad_interface import KiCADInterface  # noqa: E402

pcbnew_stub = sys.modules["pcbnew"]

# --------------------------------------------------------------------------- #
# #5h / #5n: a tool's own board-file write is adopted, not refused
# --------------------------------------------------------------------------- #


class _FakeBoard:
    def __init__(self, filename: str):
        self._filename = str(filename)

    def GetFileName(self) -> str:
        return self._filename


def _iface(board_path: Path, routes: dict) -> KiCADInterface:
    iface = KiCADInterface.__new__(KiCADInterface)
    iface.use_ipc = False
    iface.ipc_backend = None
    iface.ipc_board_api = None
    iface.board = _FakeBoard(board_path)
    iface.command_routes = routes
    iface._board_disk_signature = None
    iface._current_project_path = None
    iface._last_auto_save_status = None
    iface._auto_save_backup_keep = 2
    iface.session_backend = "swig"
    iface.session_board_path = None
    iface._is_board_healthy = lambda *a, **k: True
    iface._update_command_handlers = lambda: None
    iface.project_commands = MagicMock()
    iface._record_board_signature()
    return iface


@pytest.fixture
def board_file(tmp_path: Path) -> Path:
    f = tmp_path / "b.kicad_pcb"
    f.write_text("(kicad_pcb (version 1))\n")
    return f


@pytest.fixture
def fake_save(monkeypatch):
    saves = []

    def save(path, board):
        saves.append(path)
        Path(path).write_text("(kicad_pcb (version 1) (saved %d))\n" % len(saves))

    monkeypatch.setattr(kicad_interface.pcbnew, "SaveBoard", save)
    return saves


def _writing_handler(board_file: Path, flag: Any):
    def handler(params):
        board_file.write_text("(kicad_pcb (version 1) (written by the tool))\n")
        result = {"success": True, "message": "wrote the board"}
        if flag is not None:
            result["_boardFileWritten"] = flag
        return result

    return handler


def test_unflagged_self_write_is_refused_as_external(board_file, fake_save):
    """Control: the bug shape. Without adoption the guard refuses our own write."""
    iface = _iface(board_file, {"add_zone": _writing_handler(board_file, None)})
    result = iface.handle_command("add_zone", {})
    assert result["autoSave"]["diskChangedExternally"] is True
    assert fake_save == []


def test_memory_write_is_adopted_and_autosave_proceeds(board_file, fake_save):
    iface = _iface(
        board_file,
        {"add_zone": _writing_handler(board_file, {"mode": "memory", "path": str(board_file)})},
    )
    result = iface.handle_command("add_zone", {})
    assert "_boardFileWritten" not in result
    assert "autoSave" not in result, result.get("autoSave")
    assert fake_save == [str(board_file)]
    assert iface._dirty_state(str(board_file))["dirty"] is False


def test_disk_write_reloads_the_board(board_file, fake_save):
    iface = _iface(
        board_file,
        {"add_zone": _writing_handler(board_file, {"mode": "disk", "path": str(board_file)})},
    )
    reloaded = _FakeBoard(board_file)
    iface._safe_load_board = lambda path: reloaded
    result = iface.handle_command("add_zone", {})
    assert iface.board is reloaded
    assert "autoSave" not in result
    assert iface._board_disk_signature == iface._disk_signature(str(board_file))


def test_disk_write_reports_failed_reload(board_file, fake_save):
    iface = _iface(
        board_file,
        {"add_zone": _writing_handler(board_file, {"mode": "disk", "path": str(board_file)})},
    )
    iface._safe_load_board = lambda path: None
    result = iface.handle_command("add_zone", {})
    assert any("could not be reloaded" in w for w in result.get("warnings", []))


def test_write_to_another_file_leaves_the_session_alone(board_file, tmp_path, fake_save):
    other = tmp_path / "other.kicad_pcb"
    other.write_text("x")
    iface = _iface(board_file, {})
    before = iface._board_disk_signature
    assert iface._adopt_board_file_write({"mode": "disk", "path": str(other)}) is True
    assert iface._board_disk_signature == before


def test_adoption_clears_a_previous_refusal(board_file):
    iface = _iface(board_file, {})
    iface._last_auto_save_status = {"memChangesUnsaved": True}
    board_file.write_text("changed by the tool")
    iface._adopt_board_file_write("memory")
    assert iface._last_auto_save_status is None
    assert iface._dirty_state(str(board_file))["dirty"] is False


def test_tools_that_write_the_file_flag_it():
    """Every non-auto-save writer either flags its write or adopts it itself."""
    root = Path(__file__).parent.parent / "python"
    for rel in (
        "commands/freerouting.py",
        "commands/board/origin.py",
        "commands/hierarchical_place.py",
    ):
        assert "_boardFileWritten" in (root / rel).read_text(encoding="utf-8"), rel
    handlers = (root / "commands" / "schematic_handlers.py").read_text(encoding="utf-8")
    assert "_adopt_board_file_write" in handlers


# --------------------------------------------------------------------------- #
# #5h / B1: footprint changes in the schematic
# --------------------------------------------------------------------------- #


def _sync_iface():
    iface = KiCADInterface.__new__(KiCADInterface)
    lm = MagicMock()
    lm.libraries = {"Resistor_SMD": "/libs/Resistor_SMD.pretty"}
    iface._get_project_library_manager = lambda project_dir: lm
    return iface


def _board_with(fps):
    board = MagicMock()
    board.GetFootprints.return_value = fps
    return board


def _fp(ref, lib_id):
    fp = MagicMock(name=ref)
    fp.GetReference.return_value = ref
    fp.GetFPIDAsString.return_value = lib_id
    return fp


def test_changed_footprint_is_reported_when_updates_are_off(tmp_path):
    iface = _sync_iface()
    board = _board_with([_fp("L2", "Inductor_SMD:L_0603"), _fp("R1", "Resistor_SMD:R_0603")])
    comps = [
        {"reference": "L2", "footprint": "Inductor_SMD:L_1210"},
        {"reference": "R1", "footprint": "Resistor_SMD:R_0603"},
        {"reference": "#PWR01", "footprint": ""},
    ]
    out = iface._sync_changed_footprints(board, str(tmp_path / "x.kicad_sch"), comps, apply=False)
    assert out["replaced"] == []
    assert out["mismatched"] == [
        {
            "reference": "L2",
            "boardFootprint": "Inductor_SMD:L_0603",
            "schematicFootprint": "Inductor_SMD:L_1210",
            "reason": "updateFootprints=false",
        }
    ]


def test_changed_footprint_is_swapped(tmp_path):
    iface = _sync_iface()
    old = _fp("R1", "Resistor_SMD:R_0603")
    board = _board_with([old])
    new = MagicMock(name="new")
    with (
        patch("utils.footprint_swap.load_library_footprint", return_value=new) as load,
        patch(
            "utils.footprint_swap.swap_footprint",
            return_value=(new, {"padNetsKept": 2}),
        ) as swap,
    ):
        out = iface._sync_changed_footprints(
            board,
            str(tmp_path / "x.kicad_sch"),
            [{"reference": "R1", "footprint": "Resistor_SMD:R_0805"}],
        )
    load.assert_called_once_with("/libs/Resistor_SMD.pretty", "R_0805")
    swap.assert_called_once_with(board, old, new, "Resistor_SMD", "R_0805")
    assert out["replaced"][0]["reference"] == "R1"
    assert out["replaced"][0]["padNetsKept"] == 2


def test_changed_footprint_from_unknown_library_is_reported(tmp_path):
    iface = _sync_iface()
    board = _board_with([_fp("U1", "A:B")])
    out = iface._sync_changed_footprints(
        board, str(tmp_path / "x.kicad_sch"), [{"reference": "U1", "footprint": "Nope:C"}]
    )
    assert out["replaced"] == []
    assert "not in fp-lib-table" in out["mismatched"][0]["reason"]


# --------------------------------------------------------------------------- #
# #5i: replace_component
# --------------------------------------------------------------------------- #


def test_replace_component_is_routed():
    source = Path(kicad_interface.__file__).read_text(encoding="utf-8")
    assert '"replace_component": self.component_commands.replace_component' in source
    assert '"add_zone": self.routing_commands.add_zone' in source
    assert "replace_component" in KiCADInterface._BOARD_MUTATING_COMMANDS
    assert "add_zone" in KiCADInterface._BOARD_MUTATING_COMMANDS


def test_replace_component_requires_a_footprint():
    from commands.component import ComponentCommands

    cmd = ComponentCommands.__new__(ComponentCommands)
    cmd.board = MagicMock()
    cmd.library_manager = MagicMock()
    result = cmd.replace_component({"reference": "R1"})
    assert result["success"] is False
    assert "newFootprint" in result["errorDetails"]


def test_replace_component_reports_a_broken_swig_proxy():
    """#5r: a loader that hands back a bare SwigPyObject must fail clearly."""
    from commands.component import ComponentCommands
    from utils.footprint_swap import FootprintLoadError

    cmd = ComponentCommands.__new__(ComponentCommands)
    cmd.board = MagicMock()
    cmd.library_manager = MagicMock()
    cmd.library_manager.get_library_path.return_value = "/libs/R.pretty"
    with patch(
        "commands.component.load_library_footprint",
        side_effect=FootprintLoadError("returned SwigPyObject, not a pcbnew.FOOTPRINT"),
    ):
        result = cmd.replace_component({"reference": "R1", "newFootprint": "R:R_0805"})
    assert result["success"] is False
    assert "SwigPyObject" in result["errorDetails"]
    cmd.board.Add.assert_not_called()


# --------------------------------------------------------------------------- #
# #5k / #5q / C1: create_netclass patterns
# --------------------------------------------------------------------------- #

BOARD_NETS = ["", "GND", "/Power/+5V", "/Power/+12V", "/MCU/SDA", "VBUS"]


def test_match_netclass_pattern_is_anchored_wildcard():
    assert match_netclass_pattern("GND", BOARD_NETS) == ["GND"]
    assert match_netclass_pattern("+5V", BOARD_NETS) == []
    assert match_netclass_pattern("/Power/*", BOARD_NETS) == ["/Power/+5V", "/Power/+12V"]
    assert match_netclass_pattern("*+5V", BOARD_NETS) == ["/Power/+5V"]
    assert match_netclass_pattern("VBU?", BOARD_NETS) == ["VBUS"]


def test_suggest_net_names_offers_the_hierarchical_name():
    assert suggest_net_names("+5V", BOARD_NETS)[0] == "/Power/+5V"
    assert suggest_net_names("sda", BOARD_NETS)[0] == "/MCU/SDA"


def test_patterns_are_written_and_repointed():
    data = {"net_settings": {"netclass_patterns": [{"netclass": "Old", "pattern": "GND"}]}}
    moved = apply_netclass_patterns_to_project_settings(data, "Power", ["GND", "/Power/*"])
    assert moved == [{"pattern": "GND", "from": "Old"}]
    assert data["net_settings"]["netclass_patterns"] == [
        {"netclass": "Power", "pattern": "GND"},
        {"netclass": "Power", "pattern": "/Power/*"},
    ]


def test_patterns_handle_null_key():
    data = {"net_settings": {"netclass_patterns": None}}
    apply_netclass_patterns_to_project_settings(data, "P", ["X"])
    assert data["net_settings"]["netclass_patterns"] == [{"netclass": "P", "pattern": "X"}]


def _project(tmp_path: Path) -> Path:
    pro = tmp_path / "p.kicad_pro"
    pro.write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.2}],
                    "netclass_patterns": [],
                }
            }
        )
    )
    return pro


def _board_with_nets(tmp_path: Path, names):
    board = MagicMock()
    board.GetFileName.return_value = str(tmp_path / "p.kicad_pcb")
    netinfo = MagicMock()
    netinfo.GetNetCount.return_value = len(names)
    items = []
    for n in names:
        item = MagicMock()
        item.GetNetname.return_value = n
        items.append(item)
    netinfo.GetNetItem.side_effect = lambda code: items[code]
    board.GetNetInfo.return_value = netinfo
    return board


def test_persist_netclass_with_patterns(tmp_path):
    pro = _project(tmp_path)
    out = persist_netclass_to_project(str(pro), "Power", {"track_width": 1.0}, ["/Power/+5V"])
    assert out["persisted"] is True
    assert out["class"]["track_width"] == 1.0
    data = json.loads(pro.read_text())
    assert data["net_settings"]["netclass_patterns"] == [
        {"netclass": "Power", "pattern": "/Power/+5V"}
    ]


def test_create_netclass_persists_nets_and_reports_matches(tmp_path):
    pro = _project(tmp_path)
    board = _board_with_nets(tmp_path, BOARD_NETS)
    result = RoutingCommands(board).create_netclass(
        {"name": "Power", "traceWidth": 1.0, "nets": ["/Power/+5V", "+12V"]}
    )
    assert result["success"] is True, result
    data = json.loads(pro.read_text())
    assert {"netclass": "Power", "pattern": "/Power/+5V"} in data["net_settings"][
        "netclass_patterns"
    ]
    by_pattern = {a["pattern"]: a for a in result["assignments"]}
    assert by_pattern["/Power/+5V"]["matchedNets"] == ["/Power/+5V"]
    assert by_pattern["+12V"]["matchCount"] == 0
    assert by_pattern["+12V"]["suggestions"][0] == "/Power/+12V"
    assert "'+12V' matches no net" in result["warning"]
    assert "/Power/+12V" in result["warning"]


def test_create_netclass_uses_net_settings_without_setclass(tmp_path):
    """KiCad 10: NETINFO_ITEM.SetClass is gone; membership goes through NET_SETTINGS."""
    _project(tmp_path)
    board = _board_with_nets(tmp_path, ["GND"])
    pcbnew_stub.reset_mock()
    result = RoutingCommands(board).create_netclass({"name": "Gnd", "nets": ["GND"]})
    assert result["success"] is True
    assert result["appliedInMemory"] is True
    settings = board.GetDesignSettings.return_value.m_NetSettings
    settings.SetNetclassPatternAssignment.assert_any_call("GND", "Gnd")
    assert "SetClass" not in result.get("warning", "")


def test_create_netclass_refuses_drill_larger_than_via(tmp_path):
    _project(tmp_path)
    board = _board_with_nets(tmp_path, ["GND"])
    result = RoutingCommands(board).create_netclass(
        {"name": "Bad", "viaDiameter": 0.4, "viaDrill": 0.6}
    )
    assert result["success"] is False
    assert "drill" in result["errorDetails"]


def test_netclass_via_problem():
    assert netclass_via_problem({"via_diameter": 0.6, "via_drill": 0.3}) is None
    assert "via drill" in netclass_via_problem({"via_diameter": 0.4, "via_drill": 0.6})
    assert netclass_via_problem({"via_drill": 0.6}) is None


# --------------------------------------------------------------------------- #
# #5j: zone helpers
# --------------------------------------------------------------------------- #


def test_zone_points_accept_objects_and_pairs():
    pts, err = zone_points_nm([{"x": 0, "y": 0}, [10, 0], {"x": 10, "y": 5, "unit": "mil"}])
    assert err is None
    assert pts == [(0, 0), (10000000, 0), (254000, 127000)]


def test_zone_points_need_three_vertices():
    _, err = zone_points_nm([{"x": 0, "y": 0}])
    assert err["success"] is False


def test_zone_points_drop_explicit_closing_vertex():
    pts, _ = zone_points_nm([[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]])
    assert len(pts) == 4


def test_add_zone_rejects_unknown_net(tmp_path):
    board = _board_with_nets(tmp_path, BOARD_NETS)
    board.GetLayerID.return_value = 0
    nets_map = MagicMock()
    nets_map.has_key.return_value = False
    board.GetNetInfo.return_value.NetsByName.return_value = nets_map
    result = RoutingCommands(board).add_zone(
        {"net": "+5V", "layer": "F.Cu", "points": [[0, 0], [1, 0], [1, 1]]}
    )
    assert result["success"] is False
    assert "/Power/+5V" in result["errorDetails"]
    board.Add.assert_not_called()


# --------------------------------------------------------------------------- #
# #5l: export_dsn
# --------------------------------------------------------------------------- #


@pytest.fixture
def dsn_cmds(tmp_path):
    board_path = tmp_path / "t.kicad_pcb"
    board_path.write_text("(kicad_pcb)")
    board = MagicMock()
    board.GetFileName.return_value = str(board_path)
    board.GetFootprints.return_value = []
    pcbnew_stub.reset_mock()
    pcbnew_stub.ExportSpecctraDSN.side_effect = None
    yield FreeroutingCommands(board=board), board, board_path
    pcbnew_stub.ExportSpecctraDSN.side_effect = None
    pcbnew_stub.LoadBoard.side_effect = None


def test_export_dsn_falls_back_to_the_saved_board(dsn_cmds, tmp_path):
    cmds, board, board_path = dsn_cmds
    disk_board = MagicMock(name="disk_board")
    pcbnew_stub.LoadBoard.side_effect = lambda path: disk_board
    out = tmp_path / "t.dsn"

    def export(b, path):
        if b is board:
            return False
        Path(path).write_text("(pcb)")
        return True

    pcbnew_stub.ExportSpecctraDSN.side_effect = export
    result = cmds.export_dsn({"outputPath": str(out)})
    assert result["success"] is True, result
    assert result["source"] == "disk"
    assert any("in-memory board could not be exported" in w for w in result["warnings"])


def test_export_dsn_explains_a_missing_output_directory(dsn_cmds, tmp_path):
    cmds, _board, _ = dsn_cmds
    pcbnew_stub.ExportSpecctraDSN.return_value = False
    result = cmds.export_dsn({"outputPath": str(tmp_path / "nope" / "t.dsn")})
    assert result["success"] is False
    assert "ExportSpecctraDSN returned: False" in result["errorDetails"]
    assert "output directory does not exist" in result["errorDetails"]


def test_export_dsn_explains_a_windows_illegal_file_name(dsn_cmds, tmp_path, monkeypatch):
    cmds, _board, _ = dsn_cmds
    monkeypatch.setattr("commands.freerouting.os.name", "nt")
    pcbnew_stub.ExportSpecctraDSN.return_value = False
    result = cmds.export_dsn({"outputPath": str(tmp_path / "a>b.dsn")})
    assert result["success"] is False
    assert "characters Windows does not allow" in result["errorDetails"]
    pcbnew_stub.LoadBoard.assert_not_called()


def test_export_dsn_failure_on_both_boards_lists_every_attempt(dsn_cmds, tmp_path):
    cmds, _board, board_path = dsn_cmds
    pcbnew_stub.LoadBoard.side_effect = lambda path: MagicMock(name="disk")
    pcbnew_stub.ExportSpecctraDSN.return_value = False
    result = cmds.export_dsn({"outputPath": str(tmp_path / "t.dsn")})
    assert result["success"] is False
    assert "in-memory board: ExportSpecctraDSN returned: False" in result["errorDetails"]
    assert "saved board reloaded from disk" in result["errorDetails"]


def test_autoroute_and_ses_import_flag_their_board_write():
    import inspect

    src = inspect.getsource(FreeroutingCommands)
    # legacy autoroute + import_ses set it; the orchestrated autoroute forwards
    # the apply step's flag (one line, two literals)
    assert src.count('"_boardFileWritten"') == 4
    assert 'result["_boardFileWritten"] = applied["_boardFileWritten"]' in src
