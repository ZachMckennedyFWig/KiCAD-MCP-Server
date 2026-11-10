"""Real-pcbnew regression tests for MCP_ISSUES #5i, #5j, #5k/#5q and #5l.

Requires KICAD_USE_REAL_PCBNEW=1 and a KiCad 10 install with the stock
footprint libraries (verified against 10.0.8). Run with a Python that has
KiCad's ``pcbnew`` module::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_board_tool_fixes_real.py
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

pytestmark = [pytest.mark.integration, pytest.mark.real_pcbnew]


@pytest.fixture(autouse=True)
def require_real_pcbnew() -> None:
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")


@pytest.fixture
def pcbnew():
    import pcbnew as real

    return real


def _footprint_dir(pcbnew, lib: str) -> Path:
    candidates = []
    env = os.environ.get("KICAD10_FOOTPRINT_DIR") or os.environ.get("KICAD9_FOOTPRINT_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path(r"C:\Program Files\KiCad\10.0\share\kicad\footprints"),
        Path("/usr/share/kicad/footprints"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
    ]
    for base in candidates:
        if (base / f"{lib}.pretty").is_dir():
            return base / f"{lib}.pretty"
    pytest.skip(f"KiCad footprint library {lib} not found")


def _mm(pcbnew, v):
    return pcbnew.FromMM(v)


def _outline(pcbnew, board):
    for x1, y1, x2, y2 in [(0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(_mm(pcbnew, x1), _mm(pcbnew, y1)))
        seg.SetEnd(pcbnew.VECTOR2I(_mm(pcbnew, x2), _mm(pcbnew, y2)))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(_mm(pcbnew, 0.1))
        board.Add(seg)


def _nets(pcbnew, board, names):
    out = {}
    for name in names:
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        out[name] = item
    return out


# --------------------------------------------------------------------------- #
# #5i replace_component
# --------------------------------------------------------------------------- #


def test_replace_component_keeps_placement_side_and_nets(pcbnew, tmp_path):
    from commands.component import ComponentCommands
    from utils.footprint_swap import load_library_footprint

    lib = _footprint_dir(pcbnew, "Resistor_SMD")
    board = pcbnew.BOARD()
    _outline(pcbnew, board)
    nets = _nets(pcbnew, board, ["A", "B"])
    fp = load_library_footprint(str(lib), "R_0603_1608Metric")
    fp.SetReference("R1")
    fp.SetValue("10k")
    fp.SetFPID(pcbnew.LIB_ID("Resistor_SMD", "R_0603_1608Metric"))
    fp.SetPosition(pcbnew.VECTOR2I(_mm(pcbnew, 20), _mm(pcbnew, 30)))
    board.Add(fp)
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    fp.SetOrientationDegrees(90)
    fp.SetField("LCSC", "C22775")
    for pad in fp.Pads():
        pad.SetNet(nets["A" if pad.GetNumber() == "1" else "B"])

    lm = MagicMock()
    lm.get_library_path.side_effect = lambda nick: str(lib) if nick == "Resistor_SMD" else None
    cmd = ComponentCommands(board, lm)
    result = cmd.replace_component(
        {"reference": "R1", "newFootprint": "Resistor_SMD:R_0805_2012Metric"}
    )
    assert result["success"] is True, result
    assert result["component"]["previousFootprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert result["padNetsKept"] == 2

    path = tmp_path / "r.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    reloaded = pcbnew.LoadBoard(str(path))
    new = reloaded.FindFootprintByReference("R1")
    assert new.GetFPIDAsString() == "Resistor_SMD:R_0805_2012Metric"
    assert new.GetValue() == "10k"
    assert new.IsFlipped()
    assert new.GetPosition() == pcbnew.VECTOR2I(_mm(pcbnew, 20), _mm(pcbnew, 30))
    assert round(new.GetOrientationDegrees()) == 90
    assert new.GetFieldText("LCSC") == "C22775"
    assert {p.GetNumber(): p.GetNetname() for p in new.Pads()} == {"1": "A", "2": "B"}
    assert len(list(reloaded.GetFootprints())) == 1


# --------------------------------------------------------------------------- #
# #5j add_zone (and add_copper_pour options)
# --------------------------------------------------------------------------- #


def test_add_zone_round_trips_its_settings(pcbnew, tmp_path):
    from commands.routing import RoutingCommands

    board = pcbnew.BOARD()
    _outline(pcbnew, board)
    _nets(pcbnew, board, ["GND", "/Power/+5V"])
    cmds = RoutingCommands(board)
    square = [{"x": 5, "y": 5}, {"x": 25, "y": 5}, {"x": 25, "y": 25}, {"x": 5, "y": 25}]
    result = cmds.add_zone(
        {
            "net": "GND",
            "layers": ["F.Cu", "B.Cu"],
            "points": square,
            "priority": 3,
            "clearance": 0.3,
            "minWidth": 0.2,
            "padConnection": "solid",
            "thermalGap": 0.4,
            "thermalSpokeWidth": 0.5,
            "islandRemoval": "area",
            "minIslandArea": 1.0,
            "name": "gnd_plane",
        }
    )
    assert result["success"] is True, result
    assert result["zone"]["layers"] == ["F.Cu", "B.Cu"]
    assert result["zone"]["pointCount"] == 4

    back = cmds.add_zone(
        {"net": "/Power/+5V", "layer": "B.Cu", "points": [[30, 5], [50, 5], [50, 25]]}
    )
    assert back["success"] is True, back
    assert back["zone"]["layers"] == ["B.Cu"]

    pour = cmds.add_copper_pour({"net": "GND", "layer": "F.Cu", "padConnection": "none"})
    assert pour["success"] is True, pour
    assert pour["pour"]["padConnection"] == "none"

    path = tmp_path / "z.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    reloaded = pcbnew.LoadBoard(str(path))
    zones = {z.GetZoneName(): z for z in reloaded.Zones()}
    gnd = zones["gnd_plane"]
    assert gnd.GetNetname() == "GND"
    assert [reloaded.GetLayerName(l) for l in gnd.GetLayerSet().Seq()] == ["F.Cu", "B.Cu"]
    assert gnd.GetAssignedPriority() == 3
    assert gnd.GetPadConnection() == pcbnew.ZONE_CONNECTION_FULL
    assert gnd.GetLocalClearance() == _mm(pcbnew, 0.3)
    assert gnd.GetMinThickness() == _mm(pcbnew, 0.2)
    assert gnd.GetThermalReliefGap() == _mm(pcbnew, 0.4)
    assert gnd.GetThermalReliefSpokeWidth() == _mm(pcbnew, 0.5)
    assert gnd.GetIslandRemovalMode() == pcbnew.ISLAND_REMOVAL_MODE_AREA
    assert gnd.Outline().TotalVertices() == 4
    others = [z for z in reloaded.Zones() if z.GetZoneName() != "gnd_plane"]
    plus5 = next(z for z in others if z.GetNetname() == "/Power/+5V")
    assert [reloaded.GetLayerName(l) for l in plus5.GetLayerSet().Seq()] == ["B.Cu"]
    poured = next(z for z in others if z.GetNetname() == "GND")
    assert poured.GetPadConnection() == pcbnew.ZONE_CONNECTION_NONE


def test_add_zone_rejects_non_copper_layer(pcbnew):
    from commands.routing import RoutingCommands

    board = pcbnew.BOARD()
    result = RoutingCommands(board).add_zone(
        {"layer": "F.Silkscreen", "points": [[0, 0], [1, 0], [1, 1]]}
    )
    assert result["success"] is False
    assert "copper" in result["errorDetails"]


# --------------------------------------------------------------------------- #
# #5k / #5q create_netclass, #5l export_dsn
# --------------------------------------------------------------------------- #


def _project_board(pcbnew, tmp_path: Path):
    from utils.kicad_project import new_project_settings

    board = pcbnew.BOARD()
    _outline(pcbnew, board)
    nets = _nets(pcbnew, board, ["/Power/+5V", "GND", "SIG"])
    for ref, y, a, b in [("R1", 10, "/Power/+5V", "GND"), ("R2", 25, "SIG", "GND")]:
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(_mm(pcbnew, 10), _mm(pcbnew, y)))
        for i, net in enumerate([a, b], start=1):
            pad = pcbnew.PAD(fp)
            pad.SetNumber(str(i))
            pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
            pad.SetSize(pcbnew.VECTOR2I(_mm(pcbnew, 1.6), _mm(pcbnew, 1.6)))
            pad.SetDrillSize(pcbnew.VECTOR2I(_mm(pcbnew, 0.8), _mm(pcbnew, 0.8)))
            pad.SetLayerSet(pad.PTHMask())
            pad.SetPosition(pcbnew.VECTOR2I(_mm(pcbnew, 10 + (i - 1) * 5), _mm(pcbnew, y)))
            pad.SetNet(nets[net])
            fp.Add(pad)
        board.Add(fp)
    pcb = tmp_path / "p.kicad_pcb"
    pcbnew.SaveBoard(str(pcb), board)
    pro = tmp_path / "p.kicad_pro"
    pro.write_text(json.dumps(new_project_settings("p"), indent=2))
    return pcbnew.LoadBoard(str(pcb)), pcb, pro


def test_create_netclass_persists_patterns_and_assigns_in_memory(pcbnew, tmp_path):
    from commands.freerouting import FreeroutingCommands
    from commands.routing import RoutingCommands

    board, pcb, pro = _project_board(pcbnew, tmp_path)
    cmds = RoutingCommands(board)

    wrong = cmds.create_netclass({"name": "Power", "traceWidth": 1.0, "nets": ["+5V"]})
    assert wrong["success"] is True
    assert wrong["assignments"][0]["matchCount"] == 0
    assert wrong["assignments"][0]["suggestions"] == ["/Power/+5V"]
    assert "'/Power/+5V'" in wrong["warning"]

    result = cmds.create_netclass(
        {"name": "Power", "traceWidth": 1.0, "clearance": 0.3, "nets": ["/Power/+5V"]}
    )
    assert result["success"] is True, result
    assert result["appliedInMemory"] is True, result.get("warning")
    assert "SetClass" not in result.get("warning", "")
    assert result["assignments"][0]["matchedNets"] == ["/Power/+5V"]

    data = json.loads(pro.read_text())
    assert {"netclass": "Power", "pattern": "/Power/+5V"} in data["net_settings"][
        "netclass_patterns"
    ]
    net = board.GetNetInfo().NetsByName()["/Power/+5V"]
    assert net.GetNetClassName() == "Power"

    dsn = tmp_path / "p.dsn"
    export = FreeroutingCommands(board).export_dsn({"outputPath": str(dsn)})
    assert export["success"] is True, export
    text = dsn.read_text()
    assert "(class Power" in text and "/Power/+5V" in text.split("(class Power", 1)[1][:200]


def test_export_dsn_reports_an_illegal_file_name(pcbnew, tmp_path):
    """Verified on 10.0.8: ExportSpecctraDSN returns False for 'a>b.dsn' on Windows."""
    from commands.freerouting import FreeroutingCommands

    if os.name != "nt":
        pytest.skip("Windows file-name rules")
    board, _pcb, _pro = _project_board(pcbnew, tmp_path)
    result = FreeroutingCommands(board).export_dsn({"outputPath": str(tmp_path / "a>b.dsn")})
    assert result["success"] is False
    assert "ExportSpecctraDSN returned: False" in result["errorDetails"]
    assert "characters Windows does not allow" in result["errorDetails"]


def test_export_dsn_reports_a_missing_output_directory(pcbnew, tmp_path):
    from commands.freerouting import FreeroutingCommands

    board, _pcb, _pro = _project_board(pcbnew, tmp_path)
    result = FreeroutingCommands(board).export_dsn(
        {"outputPath": str(tmp_path / "missing" / "p.dsn")}
    )
    assert result["success"] is False
    assert "output directory does not exist" in result["errorDetails"]
