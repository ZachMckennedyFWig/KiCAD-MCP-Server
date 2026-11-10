"""Real-pcbnew tests for the board-editing primitives, zones/rule areas, custom DRC
rules and the standards profile (MCP_ISSUES E1-E3, C2, C3, §2.9).

Requires KICAD_USE_REAL_PCBNEW=1 and KiCad 10 with the stock footprint
libraries; the add_drc_rule test also needs kicad-cli::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_board_edit_real.py
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

pytestmark = [pytest.mark.integration, pytest.mark.real_pcbnew]


@pytest.fixture(autouse=True)
def require_real_pcbnew() -> None:
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")


@pytest.fixture
def pcbnew():
    import pcbnew as real

    return real


def _lib(name: str) -> Path:
    for base in [
        os.environ.get("KICAD10_FOOTPRINT_DIR") or "",
        r"C:\Program Files\KiCad\10.0\share\kicad\footprints",
        "/usr/share/kicad/footprints",
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    ]:
        if base and (Path(base) / f"{name}.pretty").is_dir():
            return Path(base) / f"{name}.pretty"
    pytest.skip(f"KiCad footprint library {name} not found")


def _v(pcbnew, x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


@pytest.fixture
def board(pcbnew, tmp_path):
    """60 x 40 mm, 4 layers; R1 (A/B) at (20,20), R2 (A/B) at (35,20), U1 and C1
    (+3V3/GND) at (20,30) and (45,30)."""
    from utils.footprint_swap import load_library_footprint

    lib = _lib("Resistor_SMD")
    b = pcbnew.BOARD()
    b.SetCopperLayerCount(4)
    for x1, y1, x2, y2 in [(0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(b)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_v(pcbnew, x1, y1))
        seg.SetEnd(_v(pcbnew, x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.1))
        b.Add(seg)
    nets = {}
    for name in ("A", "B", "GND", "+3V3"):
        nets[name] = pcbnew.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    for ref, x, y, n1, n2 in [
        ("R1", 20, 20, "A", "B"),
        ("R2", 35, 20, "A", "B"),
        ("U1", 20, 30, "+3V3", "GND"),
        ("C1", 45, 30, "+3V3", "GND"),
    ]:
        fp = load_library_footprint(str(lib), "R_0603_1608Metric")
        fp.SetReference(ref)
        fp.SetPosition(_v(pcbnew, x, y))
        b.Add(fp)
        for pad in fp.Pads():
            pad.SetNet(nets[n1 if pad.GetNumber() == "1" else n2])
    path = tmp_path / "t.kicad_pcb"
    pcbnew.SaveBoard(str(path), b)
    loaded = pcbnew.LoadBoard(str(path))
    assert isinstance(loaded, pcbnew.BOARD)
    return loaded


def _tracks(board, kind="PCB_TRACK"):
    return [t for t in board.GetTracks() if t.GetClass() == kind]


def _cmd(board, **kw):
    from commands.board_edit import BoardEditCommands

    return BoardEditCommands(board, **kw)


# --------------------------------------------------------------------------- E1


def test_add_copper_is_all_or_nothing(pcbnew, board):
    cmd = _cmd(board)
    bad = cmd.add_copper(
        {
            "items": [
                {"track": {"net": "A", "layer": "F.Cu", "width": 0.3, "points": [[1, 1], [5, 1]]}},
                {"via": {"net": "NOPE", "at": [5, 1]}},
            ]
        }
    )
    assert bad["success"] is False and bad["errors"][0]["index"] == 1
    assert _tracks(board) == []

    dry = cmd.add_copper({"items": ["T A F 0.25 1 1 5 1 5 5"], "dryRun": True})
    assert dry["success"] and dry["_skipAutoSave"] and _tracks(board) == []

    res = cmd.add_copper(
        {
            "items": [
                {
                    "track": {
                        "net": "A",
                        "layer": "B",
                        "width": 0.3,
                        "points": [[1, 1], [5, 1], [5, 5]],
                    }
                },
                {"via": {"net": "A", "at": [5, 5], "size": 0.6, "drill": 0.3}},
                {"via": {"net": "B", "at": [8, 8], "layers": ["F.Cu", "In1.Cu"]}},
            ],
            "locked": True,
        }
    )
    assert res["success"], res
    assert res["added"] == {"trackSegments": 2, "vias": 2}
    tracks = _tracks(board)
    assert len(tracks) == 2 and all(t.IsLocked() for t in tracks)
    assert {board.GetStandardLayerName(t.GetLayer()) for t in tracks} == {"B.Cu"}
    vias = {v.GetNetname(): v for v in _tracks(board, "PCB_VIA")}
    assert vias["B"].GetViaType() == pcbnew.VIATYPE_BLIND
    assert board.GetStandardLayerName(vias["B"].BottomLayer()) == "In1.Cu"
    assert vias["A"].GetDrillValue() == pcbnew.FromMM(0.3)


def test_remove_copper_and_unroute_respect_locks(board):
    cmd = _cmd(board)
    cmd.add_copper(
        {
            "items": [
                {
                    "track": {
                        "net": "A",
                        "layer": "F.Cu",
                        "points": [[1, 1], [5, 1]],
                        "locked": True,
                    }
                },
                {"track": {"net": "A", "layer": "F.Cu", "points": [[1, 3], [5, 3]]}},
                {"track": {"net": "B", "layer": "F.Cu", "points": [[1, 10], [50, 10]]}},
            ]
        }
    )
    assert cmd.remove_copper({"filter": {"layer": "F.Cu"}})["success"] is False  # too broad
    res = cmd.remove_copper({"filter": {"region": [0, 0, 6, 6]}, "dryRun": True})
    assert res["removed"]["count"] == 1 and res["skippedLocked"] == 1
    # inside (default) keeps the long B track that only crosses the box
    assert (
        cmd.remove_copper({"filter": {"region": [0, 8, 6, 12]}, "dryRun": True})["removed"]["count"]
        == 0
    )
    touch = cmd.remove_copper(
        {"filter": {"region": [0, 8, 6, 12], "regionMode": "touch"}, "dryRun": True}
    )
    assert touch["removed"]["count"] == 1
    near = cmd.remove_copper({"filter": {"endpointNear": {"at": [50, 10], "radius": 0.1}}})
    assert near["removed"]["byNet"] == {"B": 1}

    res = cmd.unroute({"nets": ["A"]})
    assert res["removed"]["count"] == 1 and res["skippedLocked"] == 1
    res = cmd.unroute({"nets": ["A"], "includeLocked": True})
    assert res["removed"]["count"] == 1
    assert _tracks(board) == []
    assert cmd.unroute({"nets": ["NOPE*"]})["success"] is False


def test_drc_delta_saves_first_and_reports_the_delta(board):
    calls = []

    def save():
        calls.append("save")
        return {"saved": True}

    def run_drc(params):
        calls.append("drc")
        return {
            "success": True,
            "summary": {"total": 3},
            "counts": {"by_severity": {"error": 3}},
            "delta": {
                "newCountAll": 1,
                "resolvedCountAll": 0,
                "new": [{"type": "clearance"}],
                "resolved": [],
            },
        }

    cmd = _cmd(board, save_board=save, run_drc=run_drc)
    res = cmd.add_copper({"items": ["T A F 0.2 1 1 5 1"], "drcDelta": True, "refillZones": False})
    assert res["_autoSaveStatus"] == {"saved": True}
    assert res["drc"]["newCount"] == 1 and res["drc"]["new"][0]["type"] == "clearance"
    assert calls[-2:] == ["save", "drc"]

    refused = _cmd(
        board, save_board=lambda: {"saved": False, "warning": "external"}, run_drc=run_drc
    )
    res = refused.unroute({"nets": ["A"], "drcDelta": True})
    assert "skipped" in res["drc"]


# --------------------------------------------------------------------------- E2


def test_move_components_reports_collisions_and_restores_dry_runs(pcbnew, board):
    cmd = _cmd(board)
    r1 = board.FindFootprintByReference("R1")
    before = r1.GetPosition()
    res = cmd.move_components({"moves": [{"ref": "R1", "x": 35, "y": 20}], "dryRun": True})
    part = res["parts"][0]
    assert part["newOverlaps"] == ["R2"] and part["collisionBasis"] == "courtyard"
    assert r1.GetPosition() == before

    out = cmd.move_components({"moves": [{"ref": "R1", "x": 59.8, "y": 20}], "dryRun": True})
    assert out["parts"][0]["outsideBoard"] is True

    rej = cmd.move_components(
        {"moves": [{"ref": "R1", "x": 35.3, "y": 20}], "rejectOnCollision": True}
    )
    assert rej["success"] is False and r1.GetPosition() == before

    ok = cmd.move_components({"moves": [{"ref": "R1", "dx": 1, "rotation": 90, "side": "back"}]})
    assert ok["success"] and ok["parts"][0]["side"] == "back"
    assert r1.GetPosition() == _v(pcbnew, 21, 20) and r1.GetLayer() == pcbnew.B_Cu
    assert ok["parts"][0]["airwireMm"]["after"] > 0

    # Keepout: a rule area forbidding footprints around (10, 10).
    cmd.add_rule_area(
        {
            "layers": ["F.Cu"],
            "polygon": [[8, 8], [12, 8], [12, 12], [8, 12]],
            "forbid": ["footprints"],
        }
    )
    hit = cmd.move_components({"moves": [{"ref": "R2", "x": 10, "y": 10}], "dryRun": True})
    assert hit["parts"][0]["keepouts"][0]["rule"] == "footprints"

    bad = cmd.move_components({"moves": [{"ref": "NOPE", "x": 1, "y": 1}]})
    assert bad["success"] is False


def test_place_relative_puts_a_pad_on_the_target(pcbnew, board):
    cmd = _cmd(board)
    res = cmd.place_relative(
        {
            "ref": "C1",
            "anchorRef": "U1",
            "anchorPad": "1",
            "offset": [0, -1.5],
            "pad": "1",
            "rotation": 90,
        }
    )
    assert res["success"], res
    u1_pad = next(p for p in board.FindFootprintByReference("U1").Pads() if p.GetNumber() == "1")
    c1_pad = next(p for p in board.FindFootprintByReference("C1").Pads() if p.GetNumber() == "1")
    want = (u1_pad.GetPosition().x, u1_pad.GetPosition().y - pcbnew.FromMM(1.5))
    got = (c1_pad.GetPosition().x, c1_pad.GetPosition().y)
    assert abs(got[0] - want[0]) <= 2 and abs(got[1] - want[1]) <= 2
    assert round(board.FindFootprintByReference("C1").GetOrientationDegrees()) == 90


# --------------------------------------------------------------------------- E3


def test_rule_area_edit_zone_split_plane_persist(pcbnew, board, tmp_path):
    cmd = _cmd(board)
    ra = cmd.add_rule_area(
        {
            "layers": ["F.Cu", "B.Cu"],
            "polygon": [[1, 1], [4, 1], [4, 4]],
            "forbid": ["vias", "tracks"],
            "name": "ko",
        }
    )
    assert ra["ruleArea"]["layers"] == ["F.Cu", "B.Cu"]  # not the fresh-zone GetLayerName() lie
    ed = cmd.edit_zone(
        {
            "zoneName": "ko",
            "forbid": ["footprints", "pads"],
            "polygon": {"x1": 1, "y1": 1, "x2": 6, "y2": 6},
        }
    )
    assert ed["success"] and ed["zones"][0]["forbid"] == ["footprints", "pads"]
    assert ed["zones"][0]["areaMm2"] == pytest.approx(25.0)

    sp = cmd.split_plane(
        {
            "layer": "In2",
            "regions": {"+3V3": [[0, 0], [30, 0], [30, 40], [0, 40]], "GND": "board"},
            "clearance": 0.3,
            "padConnection": "solid",
            "layerType": "power",
        }
    )
    assert sp["success"], sp
    prios = {z["net"]: z["priority"] for z in sp["zones"]}
    assert prios == {"+3V3": 2, "GND": 1}
    assert any("+3V3 overlaps GND" in w for w in sp["warnings"])
    again = cmd.split_plane(
        {
            "layer": "In2.Cu",
            "regions": [{"net": "GND", "polygon": "board"}],
            "replaceExisting": True,
        }
    )
    assert len(again["removed"]) == 2
    gnd = again["zones"][0]["uuid"]
    pr = cmd.edit_zone({"zone": gnd, "priority": 5, "newNet": "+3V3", "minWidth": 0.3})
    assert pr["zones"][0]["priority"] == 5 and pr["zones"][0]["net"] == "+3V3"
    many = cmd.edit_zone({"net": "+3V3", "clearance": 0.2})
    assert many["success"] and len(many["zones"]) == 1
    assert cmd.edit_zone({"net": "NOPE", "clearance": 0.2})["success"] is False

    pz = cmd.set_pad_zone_connection({"refs": ["U1"], "nets": ["GND"], "connection": "none"})
    assert pz["pads"] == [{"ref": "U1", "pad": "2", "net": "GND", "from": "inherit", "to": "none"}]

    path = tmp_path / "saved.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    re = pcbnew.LoadBoard(str(path))
    areas = [z for z in re.Zones() if z.GetIsRuleArea()]
    assert len(areas) == 1 and areas[0].GetZoneName() == "ko"
    assert areas[0].GetDoNotAllowFootprints() and not areas[0].GetDoNotAllowVias()
    assert areas[0].GetNumCorners() == 4
    plane = [z for z in re.Zones() if not z.GetIsRuleArea()]
    assert [
        (re.GetStandardLayerName(z.GetLayer()), z.GetNetname(), z.GetAssignedPriority())
        for z in plane
    ] == [("In2.Cu", "+3V3", 5)]
    assert re.GetLayerType(pcbnew.In2_Cu) == pcbnew.LT_POWER
    pad = next(p for p in re.FindFootprintByReference("U1").Pads() if p.GetNumber() == "2")
    assert pad.GetLocalZoneConnection() == pcbnew.ZONE_CONNECTION_NONE


# --------------------------------------------------------------------------- standards


def test_check_standards_inline_profile(pcbnew, board, tmp_path):
    from commands.standards import StandardsCommands

    cmd = _cmd(board)
    # In1 plane: +3V3 left of x=30, GND right. A FAST track on F.Cu crosses it.
    cmd.split_plane(
        {
            "layer": "In1.Cu",
            "regions": {
                "+3V3": {"x1": 0, "y1": 0, "x2": 30, "y2": 40},
                "GND": {"x1": 30, "y1": 0, "x2": 60, "y2": 40},
            },
        }
    )
    cmd.add_copper(
        {
            "items": [
                {
                    "track": {
                        "net": "A",
                        "layer": "F.Cu",
                        "width": 0.08,
                        "points": [[20, 10], [40, 10]],
                    }
                },
                {
                    "track": {
                        "net": "B",
                        "layer": "B.Cu",
                        "width": 0.2,
                        "points": [[50, 2], [50, 8]],
                    }
                },
            ]
        }
    )
    profile = {
        "fab": {"min_track": 0.1, "layers": 4},
        "planes": {"In1": {"split": True}},
        "regions": {
            "corner": {
                "polygon": [[45, 0], [60, 0], [60, 10], [45, 10]],
                "allowed_nets": ["A", "GND"],
            }
        },
        "netclasses": {"FAST": {"nets": ["A"], "no_plane_split_crossing": True, "prefer": "B.Cu"}},
        "checks": {
            "every_gnd_smd_pad_has_via_within": 1.0,
            "decoupling_within_mm": 3,
            "unknown_check": 1,
        },
    }
    res = StandardsCommands(board).check_standards(
        {"profile": profile, "record": False, "detail": "items"}
    )
    assert res["success"], res
    types = res["counts"]["by_type"]
    assert types.get("fab_track_width") == 1
    assert types.get("region_intrusion") == 1  # the B track in 'corner'
    assert types.get("plane_split_crossing") == 1
    assert types.get("netclass_layer_preference") == 1
    assert types.get("gnd_pad_without_via") == 2  # U1 and C1 pad 2
    assert types.get("decoupling_too_far") == 1  # U1 pad 1 -> C1 is 25 mm away
    assert res["skipped"] == ["unknown_check"]
    crossing = next(i for i in res["items"] if i["type"] == "plane_split_crossing")
    assert "+3V3 -> GND" in crossing["message"] and crossing["location"]["x"] == pytest.approx(
        30, abs=0.3
    )

    # A via within 1 mm of the GND pads clears that check.
    for ref in ("U1", "C1"):
        pad = next(p for p in board.FindFootprintByReference(ref).Pads() if p.GetNumber() == "2")
        x, y = pad.GetPosition().x / 1e6, pad.GetPosition().y / 1e6
        cmd.add_copper({"items": [{"via": {"net": "GND", "at": [x + 0.9, y]}}]})
    res = StandardsCommands(board).check_standards(
        {"profile": profile, "record": False, "checks": ["every_gnd_smd_pad_has_via_within"]}
    )
    assert res["counts"]["total"] == 0

    (tmp_path / "standards.yaml").write_text("fab: { min_track: 0.1 }\n")
    board.SetFileName(str(tmp_path / "t2.kicad_pcb"))
    res = StandardsCommands(board).check_standards({"record": False})
    assert res["profile"].endswith("standards.yaml") and res["checksRun"] == ["fab"]
    assert (
        StandardsCommands(board).check_standards({"profilePath": str(tmp_path / "x.yaml")})[
            "success"
        ]
        is False
    )


# --------------------------------------------------------------------------- C2 (kicad-cli)


def test_add_drc_rule_measures_with_kicad_cli(pcbnew, board, tmp_path):
    from commands.drc_rules import DrcRuleCommands
    from utils.kicad_cli import resolve_kicad_cli

    if not resolve_kicad_cli():
        pytest.skip("kicad-cli not found")
    _cmd(board).add_copper({"items": ["T A F 0.2 20 15 30 15", "T B F 0.2 20 17 30 17"]})
    path = tmp_path / "p.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    (tmp_path / "p.kicad_pro").write_text("{}")
    cmd = DrcRuleCommands(pcbnew.LoadBoard(str(path)))
    rule = '(rule "wide_A" (condition "A.NetName == \'A\'") (constraint track_width (min 1mm)))'
    res = cmd.add_drc_rule({"ruleText": rule, "refillZones": False, "timeoutSec": 120})
    assert res["success"] and res["kicadAccepted"] is True, res
    assert res["written"] and "wide_A" in (tmp_path / "p.kicad_dru").read_text()
    assert res["matched"]["wide_A"]["count"] >= 3  # track + two pads
    assert res["drc"]["citingRule"]["wide_A"] == 1 and res["drc"]["newCount"] >= 1

    bad = '(rule "oops" (condition "A.NoSuchProperty == \'x\'") (constraint track_width (min 1mm)))'
    res = cmd.add_drc_rule({"ruleText": bad, "refillZones": False, "timeoutSec": 120})
    assert res["success"] is False and res["kicadAccepted"] is False
    assert "oops" not in (tmp_path / "p.kicad_dru").read_text()
