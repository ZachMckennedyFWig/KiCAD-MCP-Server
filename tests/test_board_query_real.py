"""Real-pcbnew tests for the board query tools (MCP_ISSUES Part 2, 2.4 Q1-Q4).

query_region, net_report, connected_items, why_unconnected and the
region/highlight mode of get_board_2d_view, run against a small 4-layer board
built in memory:

* R1 (9,10)/(11,10) and R2 (29,10)/(31,10): pads 1 on SIG, R1.2 on PWR, R2.2 on GND.
* SIG is routed on F.Cu (9,10)->(18,10) and (21,10)->(29,10), w 0.25: two
  islands with a 2.75 mm copper gap on F.Cu.
* Net A runs on F.Cu (5,20)->(35,20) over In1.Cu, which is split into a GND
  zone (x 0..19.8) and a PWR zone (x 20.2..40): one GND -> PWR split crossing.
* A GND via at (25,25) and a silkscreen text "HELLO" at (5,5).

Requires KICAD_USE_REAL_PCBNEW=1::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_board_query_real.py
"""

import base64
import io
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


def _mm(v):
    import pcbnew

    return pcbnew.FromMM(v)


def _pt(x, y):
    import pcbnew

    return pcbnew.VECTOR2I(_mm(x), _mm(y))


def _build_board():
    import pcbnew

    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    for x1, y1, x2, y2 in [(0, 0, 40, 0), (40, 0, 40, 30), (40, 30, 0, 30), (0, 30, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_pt(x1, y1))
        seg.SetEnd(_pt(x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(_mm(0.1))
        board.Add(seg)
    nets = {}
    for name in ("GND", "SIG", "PWR", "A"):
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net

    def footprint(ref, x, y, pads):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        fp.SetPosition(_pt(x, y))
        board.Add(fp)
        for number, dx, net in pads:
            pad = pcbnew.PAD(fp)
            pad.SetNumber(number)
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECT)
            pad.SetSize(pcbnew.F_Cu, _pt(1, 1))
            pad.SetLayerSet(pcbnew.PAD.SMDMask())
            pad.SetPosition(_pt(x + dx, y))
            pad.SetNet(nets[net])
            fp.Add(pad)

    footprint("R1", 10, 10, [("1", -1, "SIG"), ("2", 1, "PWR")])
    footprint("R2", 30, 10, [("1", -1, "SIG"), ("2", 1, "GND")])

    def track(net, layer, x1, y1, x2, y2, w):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(_pt(x1, y1))
        t.SetEnd(_pt(x2, y2))
        t.SetLayer(layer)
        t.SetWidth(_mm(w))
        t.SetNet(nets[net])
        board.Add(t)
        return t

    track("SIG", pcbnew.F_Cu, 9, 10, 18, 10, 0.25)
    track("SIG", pcbnew.F_Cu, 21, 10, 29, 10, 0.25)
    track("A", pcbnew.F_Cu, 5, 20, 35, 20, 0.2)

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(_pt(25, 25))
    via.SetWidth(_mm(0.6))
    via.SetDrill(_mm(0.3))
    via.SetNet(nets["GND"])
    board.Add(via)

    def zone(net, x0, x1):
        z = pcbnew.ZONE(board)
        z.SetLayer(pcbnew.In1_Cu)
        z.SetNet(nets[net])
        outline = z.Outline()
        outline.NewOutline()
        for x, y in [(x0, 0.5), (x1, 0.5), (x1, 29.5), (x0, 29.5)]:
            outline.Append(_mm(x), _mm(y))
        board.Add(z)

    zone("GND", 0.5, 19.8)
    zone("PWR", 20.2, 39.5)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    text = pcbnew.PCB_TEXT(board)
    text.SetText("HELLO")
    text.SetPosition(_pt(5, 5))
    text.SetLayer(pcbnew.F_SilkS)
    board.Add(text)
    board.BuildConnectivity()
    return board


@pytest.fixture(scope="module")
def board():
    return _build_board()


@pytest.fixture
def cmds(board):
    from commands.board_query import BoardQueryCommands

    return BoardQueryCommands(lambda: board)


def _rows_by_kind(result):
    out = {}
    for row in result["rows"]:
        out.setdefault(row[0], []).append(row)
    return out


# --------------------------------------------------------------------------- Q1


def test_query_region_lists_pads_and_tracks_in_window(cmds):
    r = cmds.query_region({"x0": 8, "y0": 8, "x1": 12, "y1": 12})
    assert r["success"], r
    assert r["columns"] == ["kind", "id", "net", "layer", "geom", "info", "locked"]
    rows = _rows_by_kind(r)
    assert sorted(row[1] for row in rows["pad"]) == ["R1.1", "R1.2"]
    pad = next(row for row in rows["pad"] if row[1] == "R1.1")
    assert pad[2] == "SIG" and pad[3] == "F.Cu" and pad[4] == [9.0, 10.0]
    assert pad[5].startswith("rect 1")
    (trk,) = rows["track"]
    assert trk[2] == "SIG" and trk[4] == [9.0, 10.0, 18.0, 10.0] and trk[5] == "w0.25"
    assert r["byKind"]["pad"] == 2
    # The In1.Cu GND zone covers the window.
    assert any(row[2] == "GND" and row[3] == "In1.Cu" for row in rows["zone"])
    assert "R1.1" not in str(rows.get("text"))  # footprint fields here are invisible


def test_query_region_filters_and_summary(cmds):
    only_sig = cmds.query_region({"x0": 0, "y0": 0, "x1": 40, "y1": 30, "nets": ["SIG"]})
    assert {row[2] for row in only_sig["rows"]} == {"SIG"}
    assert only_sig["byKind"] == {"pad": 2, "track": 2}

    inner = cmds.query_region(
        {"x0": 0, "y0": 0, "x1": 40, "y1": 30, "layers": ["In1.Cu"], "kinds": ["zones"]}
    )
    assert sorted(row[2] for row in inner["rows"]) == ["GND", "PWR"]

    text = cmds.query_region({"x0": 3, "y0": 3, "x1": 7, "y1": 7, "kinds": ["text"]})
    assert text["rows"][0][5].startswith("'HELLO'")
    assert text["rows"][0][3] == "F.Silkscreen"

    summary = cmds.query_region({"x0": 0, "y0": 0, "x1": 40, "y1": 30, "detail": "summary"})
    assert "rows" not in summary
    assert summary["byNet"]["SIG"] == 4
    assert summary["byKind"]["via"] == 1

    limited = cmds.query_region({"x0": 0, "y0": 0, "x1": 40, "y1": 30, "limit": 2})
    assert len(limited["rows"]) == 2 and limited["truncated"] is True
    assert limited["omitted"] == limited["count"] - 2

    full = cmds.query_region(
        {"x0": 24, "y0": 24, "x1": 26, "y1": 26, "kinds": ["via"], "detail": "full"}
    )
    via = full["rows"][0]
    assert via[0] == "via" and via[3] == "F.Cu-B.Cu" and via[5] == "0.6/0.3"
    assert len(via[-1]["uuid"]) == 36 and via[1] == via[-1]["uuid"][:8]


def test_query_region_rejects_bad_input(cmds):
    assert not cmds.query_region({"x0": 0, "y0": 0, "x1": 1})["success"]
    bad_kind = cmds.query_region({"x0": 0, "y0": 0, "x1": 1, "y1": 1, "kinds": ["blob"]})
    assert not bad_kind["success"] and "blob" in bad_kind["message"]
    bad_layer = cmds.query_region({"x0": 0, "y0": 0, "x1": 1, "y1": 1, "layers": ["Nope.Cu"]})
    assert not bad_layer["success"] and "Nope.Cu" in bad_layer["message"]


def test_query_region_reads_board_path_fresh_from_disk(tmp_path, board):
    import pcbnew
    from commands.board_query import BoardQueryCommands

    path = str(tmp_path / "fixture.kicad_pcb")
    pcbnew.SaveBoard(path, board)
    r = BoardQueryCommands(None).query_region(
        {"x0": 28, "y0": 8, "x1": 32, "y1": 12, "kinds": ["pad"], "boardPath": path}
    )
    assert r["success"], r
    assert r["boardSource"] == "disk"
    assert sorted(row[1] for row in r["rows"]) == ["R2.1", "R2.2"]
    missing = BoardQueryCommands(None).query_region(
        {"x0": 0, "y0": 0, "x1": 1, "y1": 1, "boardPath": str(tmp_path / "nope.kicad_pcb")}
    )
    assert not missing["success"] and "not found" in missing["message"]


# --------------------------------------------------------------------------- Q4


def test_why_unconnected_reports_islands_and_gap(cmds):
    r = cmds.why_unconnected({"net": "SIG"})
    assert r["success"], r
    assert r["fullyConnected"] is False
    assert r["islandCount"] == 2
    pads = sorted(p for isl in r["islands"] for p in isl["padList"])
    assert pads == ["R1.1", "R2.1"]
    (gap,) = r["gaps"]
    assert gap["gapMm"] == pytest.approx(2.75, abs=0.01)
    assert gap["layer"] == "F.Cu"
    xs = sorted([gap["from"]["at"][0], gap["to"]["at"][0]])
    assert xs == pytest.approx([18.125, 20.875], abs=0.01)
    assert gap["from"]["at"][1] == pytest.approx(10.0)


def test_why_unconnected_connected_net_and_unknown_net(cmds):
    gnd = cmds.why_unconnected({"net": "GND"})
    assert gnd["success"] and gnd["fullyConnected"] is False  # pad R2.2 and the via float
    one = cmds.why_unconnected({"net": "A"})
    assert one["fullyConnected"] is True and "gaps" not in one
    missing = cmds.why_unconnected({"net": "SIGG"})
    assert not missing["success"] and "SIG" in missing["message"]


def test_connected_items_from_pad_point_and_uuid(cmds, board):
    by_pad = cmds.connected_items({"ref": "R1", "pad": "1"})
    assert by_pad["success"], by_pad
    assert by_pad["net"] == "SIG" and by_pad["netIslandCount"] == 2
    assert by_pad["island"]["padList"] == ["R1.1"]
    assert by_pad["isWholeNet"] is False

    by_point = cmds.connected_items({"x": 25, "y": 10, "layer": "F.Cu", "detail": "items"})
    assert by_point["start"]["kind"] == "track"
    assert by_point["island"]["padList"] == ["R2.1"]
    assert {row[0] for row in by_point["rows"]} == {"pad", "track"}

    track = next(t for t in board.GetTracks() if t.GetNetname() == "SIG")
    prefix = track.m_Uuid.AsString()[:8]
    by_uuid = cmds.connected_items({"uuid": prefix})
    assert by_uuid["start"]["id"] == prefix

    zone_hit = cmds.connected_items({"x": 5, "y": 5, "layer": "In1.Cu"})
    assert zone_hit["start"]["kind"] == "zone" and zone_hit["net"] == "GND"

    nothing = cmds.connected_items({"x": 19.5, "y": 10, "layer": "F.Cu"})
    assert not nothing["success"] and "No copper" in nothing["message"]
    assert not cmds.connected_items({})["success"]


# --------------------------------------------------------------------------- Q3


def test_net_report_lengths_islands_and_split_crossing(cmds):
    r = cmds.net_report(
        {"nets": ["SIG", "A"], "regions": {"LEFT": {"x0": 0, "y0": 0, "x1": 8, "y1": 30}}}
    )
    assert r["success"], r
    by_net = {n["net"]: n for n in r["nets"]}
    sig, a = by_net["SIG"], by_net["A"]
    assert sig["lengthMm"] == pytest.approx(17.0)
    assert sig["lengthByLayer"] == {"F.Cu": pytest.approx(17.0)}
    assert sig["islands"] == 2 and sig["fullyConnected"] is False
    assert sig["unroutedConnections"] == 1
    assert sig["widthMm"] == [0.25, 0.25]
    assert sig["regions"] == []

    assert a["fullyConnected"] is True and a["lengthMm"] == pytest.approx(30.0)
    assert a["splitCrossings"] == 1
    (crossing,) = a["crossings"]
    assert crossing["from"] == "GND" and crossing["to"] == "PWR"
    assert crossing["refLayer"] == "In1.Cu" and crossing["layer"] == "F.Cu"
    assert 19.5 < crossing["at"][0] < 20.5
    assert a["regions"] == ["LEFT"]
    assert "In1.Cu" in r["planeLayers"]


def test_net_report_all_nets_summary_puts_problems_first(cmds):
    r = cmds.net_report({})
    assert r["success"], r
    assert r["netCount"] == 4
    assert r["notFullyConnected"] >= 1
    assert r["nets"][0]["fullyConnected"] is False
    assert "lengthByLayer" not in r["nets"][0]  # summary detail
    no_planes = cmds.net_report({"nets": ["A"], "planeCrossings": False})
    assert "splitCrossings" not in no_planes["nets"][0]


# --------------------------------------------------------------------------- Q2


def test_region_render_highlights_net_and_draws_ratsnest(board):
    from commands.board.view import BoardViewCommands
    from PIL import Image

    r = BoardViewCommands(board).get_board_2d_view(
        {
            "region": {"x0": 5, "y0": 5, "x1": 35, "y1": 15},
            "scale": 20,
            "highlightNets": ["SIG"],
            "showRatsnest": True,
            "grid": False,
        }
    )
    assert r["success"], r
    view = r["view"]
    assert (view["widthPx"], view["heightPx"]) == (600, 200)
    assert view["highlight"][0]["net"] == "SIG"
    colour = tuple(int(view["highlight"][0]["color"][i : i + 2], 16) for i in (1, 3, 5))
    (line,) = view["ratsnest"]
    assert line["gapMm"] == pytest.approx(2.75, abs=0.01)

    img = Image.open(io.BytesIO(base64.b64decode(r["imageData"]))).convert("RGB")
    assert img.size == (600, 200)
    # The SIG track at (12, 10) is drawn in its highlight colour ...
    px = img.getpixel((int((12 - 5) * 20), int((10 - 5) * 20)))
    assert sum(abs(a - b) for a, b in zip(px, colour)) < 60, px
    # ... and the A net is outside the region, the GND fill below is grey.
    grey = img.getpixel((int((12 - 5) * 20), int((13 - 5) * 20)))
    assert max(grey) - min(grey) < 25, grey


def test_region_render_validates_input(board):
    from commands.board.view import BoardViewCommands

    view = BoardViewCommands(board)
    assert not view.get_board_2d_view({"region": {"x0": 1, "y0": 1, "x1": 1, "y1": 5}})["success"]
    svg = view.get_board_2d_view({"region": {"x0": 0, "y0": 0, "x1": 5, "y1": 5}, "format": "svg"})
    assert not svg["success"] and "svg" in svg["message"]
    unknown = view.get_board_2d_view({"highlightNets": ["NOPE"]})
    assert not unknown["success"]
