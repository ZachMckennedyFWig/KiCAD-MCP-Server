"""Real-pcbnew tests for route_connection / route_bus / length_match /
fanout_to_planes and (when Java + freerouting.jar are installed) the
orchestrated async autoroute with job_apply.

Requires KICAD_USE_REAL_PCBNEW=1 and KiCad's stock Resistor_SMD library::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_routing_workflows_real.py
"""

import math
import os
import sys
from pathlib import Path

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


def _footprint_dir(lib: str) -> Path:
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


def _v(pcbnew, x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _track(pcbnew, board, net, a, b, layer, width=0.25):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(_v(pcbnew, *a))
    t.SetEnd(_v(pcbnew, *b))
    t.SetWidth(pcbnew.FromMM(width))
    t.SetLayer(layer)
    t.SetNet(net)
    board.Add(t)
    return t


def _build(pcbnew, tmp_path, wall=None, gnd_zone=True, name="t.kicad_pcb"):
    """40 x 30 mm two-layer board: R1 (left) and R2 (right), SIG between them.

    R1.2 / R2.1 = SIG, R1.1 / R2.2 = GND, R3 (bottom middle) = BUS1/BUS2.
    ``wall`` = (y0, y1) adds an OTHER-net track at x = 20 on F.Cu.
    """
    from utils.footprint_swap import load_library_footprint

    lib = str(_footprint_dir("Resistor_SMD"))
    board = pcbnew.BOARD()
    for x1, y1, x2, y2 in [(0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_v(pcbnew, x1, y1))
        seg.SetEnd(_v(pcbnew, x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)
    nets = {}
    for n in ("SIG", "GND", "OTHER", "BUS1", "BUS2"):
        item = pcbnew.NETINFO_ITEM(board, n)
        board.Add(item)
        nets[n] = item
    placements = [
        ("R1", 8, 15, {"1": "GND", "2": "SIG"}),
        ("R2", 32, 15, {"1": "SIG", "2": "GND"}),
        ("R3", 8, 25, {"1": "BUS1", "2": "BUS2"}),
        ("R4", 32, 25, {"1": "BUS1", "2": "BUS2"}),
    ]
    for ref, x, y, padnets in placements:
        fp = load_library_footprint(lib, "R_0805_2012Metric")
        fp.SetReference(ref)
        fp.SetFPID(pcbnew.LIB_ID("Resistor_SMD", "R_0805_2012Metric"))
        fp.SetPosition(_v(pcbnew, x, y))
        board.Add(fp)
        for pad in fp.Pads():
            pad.SetNet(nets[padnets[pad.GetNumber()]])
    if wall:
        _track(pcbnew, board, nets["OTHER"], (20, wall[0]), (20, wall[1]), pcbnew.F_Cu)
    if gnd_zone:
        zone = pcbnew.ZONE(board)
        zone.SetLayer(pcbnew.B_Cu)
        zone.SetNet(nets["GND"])
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in [(1, 1), (39, 1), (39, 29), (1, 29)]:
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(zone)
    path = tmp_path / name
    pcbnew.SaveBoard(str(path), board)
    return pcbnew.LoadBoard(str(path)), path


def _items(board, net=None):
    return [t for t in board.GetTracks() if net is None or t.GetNetname() == net]


def _seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy
    t = 0 if l2 == 0 else max(0, min(1, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))


# ------------------------------------------------------------- route_connection


def test_route_connection_detours_around_a_wall(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path, wall=(2, 24))
    rc = RouterCommands(board)
    req = {
        "net": "SIG",
        "from": {"ref": "R1", "pad": "2"},
        "to": {"ref": "R2", "pad": "1"},
        "layersAllowed": ["F.Cu"],
    }
    dry = rc.route_connection({**req, "dryRun": True})
    assert dry["success"] is True, dry
    assert dry["written"] is False
    assert _items(board, "SIG") == []
    straight = 32 - 1 - (8 + 1)  # pad centres are ~1 mm off the footprint centre
    assert dry["lengthMm"] > straight + 5  # it has to go around the wall end

    res = rc.route_connection({**req, "locked": True})
    assert res["success"] is True, res
    assert res["written"] is True
    assert res["layersUsed"] == ["F.Cu"]
    tracks = _items(board, "SIG")
    assert len(tracks) == res["segments"] and all(t.IsLocked() for t in tracks)
    # every new segment keeps >= clearance + half widths from the wall
    clr = 0.2
    for t in tracks:
        a = (pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetStart().y))
        b = (pcbnew.ToMM(t.GetEnd().x), pcbnew.ToMM(t.GetEnd().y))
        for k in range(21):
            p = (a[0] + (b[0] - a[0]) * k / 20, a[1] + (b[1] - a[1]) * k / 20)
            if p[1] <= 24 + 1:
                d = abs(p[0] - 20) if 2 <= p[1] <= 24 else _seg_dist(p, (20, 2), (20, 24))
                assert d >= clr + 0.125 + 0.125 - 1e-3
    # path is 45-degree only (apart from the stubs into the pad centres)
    angles = []
    for t in tracks[1:-1]:
        dx = t.GetEnd().x - t.GetStart().x
        dy = t.GetEnd().y - t.GetStart().y
        angles.append(round(math.degrees(math.atan2(dy, dx))) % 45)
    assert all(a == 0 for a in angles)

    again = rc.route_connection(req)
    assert again["success"] is True and again["alreadyConnected"] is True


def test_route_connection_uses_vias_when_allowed(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path, wall=(0.5, 29.5), gnd_zone=False)
    res = RouterCommands(board).route_connection(
        {"net": "SIG", "from": {"ref": "R1", "pad": "2"}, "to": {"ref": "R2", "pad": "1"}}
    )
    assert res["success"] is True, res
    assert res["vias"] >= 2 and set(res["layersUsed"]) == {"F.Cu", "B.Cu"}
    vias = [t for t in _items(board, "SIG") if t.GetClass() == "PCB_VIA"]
    assert len(vias) == res["vias"]


def test_route_connection_boxed_in_fails_and_leaves_board_unchanged(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path, wall=(0.5, 29.5))
    before = len(_items(board))
    res = RouterCommands(board).route_connection(
        {
            "net": "SIG",
            "from": {"ref": "R1", "pad": "2"},
            "to": {"ref": "R2", "pad": "1"},
            "layersAllowed": ["F.Cu"],
            "searchMargin": 3,
        }
    )
    assert res["success"] is False
    assert "No route" in res["message"]
    assert len(_items(board)) == before


def test_route_connection_avoid_region_and_missing_connection_mode(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path, gnd_zone=False)
    region = {"x1": 15, "y1": 10, "x2": 25, "y2": 20}
    res = RouterCommands(board).route_connection(
        {"net": "SIG", "avoidRegions": [region], "layersAllowed": ["F.Cu"], "dryRun": True}
    )
    assert res["success"] is True, res
    assert {res["from"], res["to"]} == {"R1.2", "R2.1"}  # picked the missing connection
    pts = res["path"]
    for a, b in zip(pts, pts[1:]):
        for k in range(11):
            x = a["x"] + (b["x"] - a["x"]) * k / 10
            y = a["y"] + (b["y"] - a["y"]) * k / 10
            assert not (15 < x < 25 and 10 < y < 20)


def test_route_connection_input_errors(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path)
    rc = RouterCommands(board)
    assert rc.route_connection({"net": "NOPE"})["success"] is False
    one_end = rc.route_connection({"net": "SIG", "from": {"ref": "R1", "pad": "2"}})
    assert "both" in one_end["errorDetails"]
    wrong_net = rc.route_connection(
        {"net": "SIG", "from": {"ref": "R1", "pad": "1"}, "to": {"ref": "R2", "pad": "1"}}
    )
    assert "not 'SIG'" in wrong_net["errorDetails"]
    bad_layer = rc.route_connection({"net": "SIG", "layersAllowed": ["In7.Cu"]})
    assert "unknown copper layers" in bad_layer["errorDetails"]


def test_route_bus_and_length_match(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path, gnd_zone=False)
    rc = RouterCommands(board)
    bus = rc.route_bus(
        {
            "connections": [
                {"net": "BUS1", "from": {"ref": "R3", "pad": "1"}, "to": {"ref": "R4", "pad": "1"}},
                {"net": "BUS2", "from": {"ref": "R3", "pad": "2"}, "to": {"ref": "R4", "pad": "2"}},
            ],
            "pitch": 0.6,
            "width": 0.2,
            "layersAllowed": ["F.Cu", "B.Cu"],
        }
    )
    assert bus["success"] is True, bus
    assert [r["net"] for r in bus["results"]] == ["BUS1", "BUS2"]
    assert _items(board, "BUS1") and _items(board, "BUS2")

    lm = rc.length_match({"nets": ["BUS1", "BUS2"], "pairs": [["BUS1", "BUS2"]], "tolerance": 50})
    assert lm["success"] is True
    lengths = {r["net"]: r["lengthMm"] for r in lm["nets"]}
    assert all(v > 20 for v in lengths.values())
    assert lm["targetMm"] == max(lengths.values())
    assert lm["allWithinTolerance"] is True
    tight = rc.length_match({"nets": ["BUS1", "BUS2"], "target": 100, "tolerance": 0.1})
    assert tight["allWithinTolerance"] is False
    assert all(r["addMm"] > 0 for r in tight["nets"])
    assert rc.length_match({"nets": ["NOPE"]})["success"] is False


# ------------------------------------------------------------- fanout_to_planes


def test_fanout_places_stub_and_via_in_plane_region(pcbnew, tmp_path):
    from commands.fanout import FanoutCommands

    board, _ = _build(pcbnew, tmp_path)
    fo = FanoutCommands(board)
    dry = fo.fanout_to_planes({"nets": ["GND"], "dryRun": True})
    assert dry["success"] is True, dry
    assert dry["placed"]["pads"] == 2 and dry["placed"]["vias"] >= 2
    assert _items(board) == []

    res = fo.fanout_to_planes({"nets": ["GND"]})
    assert res["placed"] == dry["placed"]
    gnd = _items(board, "GND")
    vias = [t for t in gnd if t.GetClass() == "PCB_VIA"]
    assert len(vias) == res["placed"]["vias"] and all(t.IsLocked() for t in gnd)
    for item in res["items"]:
        assert item["kind"] == "stub" and item["stub"]["layer"] == "F.Cu"
    # the vias land outside the pads and not on the SIG pads next to them
    for v in vias:
        p = v.GetPosition()
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() != "GND":
                    d = math.hypot(p.x - pad.GetPosition().x, p.y - pad.GetPosition().y) / 1e6
                    assert d > 1.0
    again = fo.fanout_to_planes({"nets": ["GND"]})
    assert again["placed"]["vias"] == 0 and again["skipped"]["alreadyServed"] == 2


def test_fanout_region_policy(pcbnew, tmp_path):
    from commands.fanout import FanoutCommands

    board, _ = _build(pcbnew, tmp_path, gnd_zone=False)
    fo = FanoutCommands(board)
    zone_only = fo.fanout_to_planes({"nets": ["GND"], "dryRun": True})
    assert zone_only["placed"]["vias"] == 0
    assert zone_only["skipped"]["notInPlaneRegion"] == 2
    anywhere = fo.fanout_to_planes({"nets": ["GND"], "regionPolicy": "any", "dryRun": True})
    assert anywhere["placed"]["pads"] == 2
    assert fo.fanout_to_planes({"nets": ["NOPE"]})["success"] is False
    assert fo.fanout_to_planes({"nets": ["GND"], "regionPolicy": "x"})["success"] is False


def test_routing_workflow_routes_follow_the_live_board(pcbnew, tmp_path):
    from commands.routing_workflows import BOARD_MUTATING, routing_workflow_routes

    holder = {"board": None}
    routes = routing_workflow_routes(lambda: holder["board"])
    assert routes["route_connection"]({"net": "SIG"})["message"] == "No board is loaded"
    holder["board"], _ = _build(pcbnew, tmp_path)
    assert routes["length_match"]({"nets": ["SIG"]})["success"] is True
    assert set(BOARD_MUTATING) <= set(routes)


# ----------------------------------------------- orchestrated Freerouting (opt.)


def _freerouting_ready():
    from commands.freerouting import FreeroutingCommands

    try:
        return FreeroutingCommands(None).check_freerouting({})["ready"]
    except Exception:
        return False


def test_async_autoroute_job_then_apply(pcbnew, tmp_path):
    if not _freerouting_ready():
        pytest.skip("Java + freerouting.jar not available")
    from commands.freerouting import FreeroutingCommands
    from utils.jobs import JOBS

    board, path = _build(pcbnew, tmp_path, wall=(2, 24))
    fc = FreeroutingCommands(board)
    started = fc.autoroute(
        {"async": True, "nets": ["SIG"], "passes": 5, "stallTimeout": 120, "timeout": 300}
    )
    assert started["success"] is True and started["async"] is True, started
    job = JOBS.wait(started["jobId"], timeout=600)
    snap = job.snapshot(log_lines=5)
    assert snap["state"] == "succeeded", snap
    result = snap["result"]
    assert result["pipeline"] == "orchestrated"
    assert result["dsn"]["netFilter"]["routedNets"] == ["SIG"]
    assert result["kicad"]["before"]["unconnected"] >= 1
    assert result["kicad"]["after"]["unconnected"] < result["kicad"]["before"]["unconnected"]
    assert result["decision"]["import"] is True
    assert snap["applyStep"]["applied"] is False
    assert _items(board, "SIG") == []  # nothing written before job_apply

    applied = JOBS.apply(started["jobId"], {})
    assert applied["success"] is True, applied
    assert applied["_boardFileWritten"] == {"mode": "disk", "path": str(path)}
    reloaded = pcbnew.LoadBoard(str(path))
    assert _items(reloaded, "SIG")
    again = JOBS.apply(started["jobId"], {})
    assert again["success"] is False
