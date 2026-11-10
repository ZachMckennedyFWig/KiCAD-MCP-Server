"""Real-pcbnew tests for the finishing tools and layout transplant (MCP_ISSUES F1, F2, F4, R4).

Requires KICAD_USE_REAL_PCBNEW=1 and KiCad's stock footprint libraries
(verified against 10.0.8)::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_finishing_real.py
"""

import math
import os
import sys
from pathlib import Path
from unittest.mock import patch

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


# --------------------------------------------------------------------------- #
# board building helpers
# --------------------------------------------------------------------------- #


def _lib(lib: str) -> Path:
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


def mm(pcbnew, v):
    return pcbnew.FromMM(v)


def pt(pcbnew, x, y):
    return pcbnew.VECTOR2I(mm(pcbnew, x), mm(pcbnew, y))


def outline(pcbnew, board, w=40.0, h=30.0):
    for x1, y1, x2, y2 in [(0, 0, w, 0), (w, 0, w, h), (w, h, 0, h), (0, h, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pt(pcbnew, x1, y1))
        seg.SetEnd(pt(pcbnew, x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(mm(pcbnew, 0.1))
        board.Add(seg)


def nets(pcbnew, board, names):
    out = {}
    for name in names:
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        out[name] = item
    return out


def footprint(pcbnew, board, lib, name, ref, x, y, angle=0.0, pad_nets=None, back=False):
    from utils.footprint_swap import load_library_footprint

    fp = load_library_footprint(str(_lib(lib)), name)
    fp.SetReference(ref)
    fp.SetPosition(pt(pcbnew, x, y))
    board.Add(fp)
    if back:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    fp.SetOrientationDegrees(angle)
    for pad in fp.Pads():
        net = (pad_nets or {}).get(str(pad.GetNumber()))
        if net is not None:
            pad.SetNet(net)
    return fp


def track(pcbnew, board, net, a, b, layer=None, width=0.25):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(pcbnew, *a))
    t.SetEnd(pt(pcbnew, *b))
    t.SetWidth(mm(pcbnew, width))
    t.SetLayer(pcbnew.F_Cu if layer is None else layer)
    t.SetNet(net)
    board.Add(t)
    return t


def via(pcbnew, board, net, x, y, size=0.6, drill=0.3):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pt(pcbnew, x, y))
    v.SetWidth(mm(pcbnew, size))
    v.SetDrill(mm(pcbnew, drill))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNet(net)
    board.Add(v)
    return v


def zone(pcbnew, board, net, layer, pts, rule=False):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    if rule:
        z.SetIsRuleArea(True)
        z.SetDoNotAllowZoneFills(True)
        z.SetDoNotAllowTracks(False)
        z.SetDoNotAllowVias(False)
        z.SetDoNotAllowPads(False)
        z.SetDoNotAllowFootprints(False)
    else:
        z.SetNet(net)
    poly = z.Outline()
    poly.NewOutline()
    for x, y in pts:
        poly.Append(mm(pcbnew, x), mm(pcbnew, y))
    board.Add(z)
    return z


def fill(pcbnew, board):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


class Iface:
    """Minimal stand-in for KiCADInterface: the tools only need .board."""

    def __init__(self, board):
        self.board = board


def vias_of(pcbnew, board):
    return [t for t in board.GetTracks() if isinstance(t, pcbnew.PCB_VIA)]


def tracks_of(pcbnew, board):
    return [t for t in board.GetTracks() if not isinstance(t, pcbnew.PCB_VIA)]


# --------------------------------------------------------------------------- #
# F1 stitch_ground
# --------------------------------------------------------------------------- #


@pytest.fixture
def pour_board(pcbnew):
    b = pcbnew.BOARD()
    outline(pcbnew, b)
    n = nets(pcbnew, b, ["GND", "SIG"])
    full = [(0, 0), (40, 0), (40, 30), (0, 30)]
    zone(pcbnew, b, n["GND"], pcbnew.F_Cu, full)
    zone(pcbnew, b, n["GND"], pcbnew.B_Cu, full)
    track(pcbnew, b, n["SIG"], (5, 15), (35, 15), width=0.3)
    footprint(
        pcbnew,
        b,
        "Resistor_SMD",
        "R_0603_1608Metric",
        "R1",
        20,
        8,
        pad_nets={"1": n["GND"], "2": n["SIG"]},
    )
    fill(pcbnew, b)
    return b, n


def test_stitch_ground_dry_run_respects_clearances(pcbnew, pour_board):
    from commands.finishing import FinishingCommands

    board, _ = pour_board
    before = len(vias_of(pcbnew, board))
    res = FinishingCommands(Iface(board)).stitch_ground(
        {"grid": 3.0, "edgePitch": 2.0, "dryRun": True, "limit": 1000}
    )
    assert res["success"] is True, res
    assert res["dryRun"] is True
    assert res["placed"]["grid"] > 10 and res["placed"]["edge"] > 10
    assert res["pourLayers"] == ["F.Cu", "B.Cu"]
    assert len(vias_of(pcbnew, board)) == before  # dry run: nothing added

    # every via clears the SIG track by radius + half width + clearance
    need = 0.3 + 0.15 + res["clearance"]
    for v in res["vias"]:
        if 5 <= v["x"] <= 35:
            assert abs(v["y"] - 15) >= need - 1e-6, v
    # and stays inside the board with the edge clearance
    for v in res["vias"]:
        assert 0.3 <= v["x"] <= 39.7 and 0.3 <= v["y"] <= 29.7
    # none inside R1's courtyard
    r1 = board.FindFootprintByReference("R1")
    court = r1.GetCourtyard(pcbnew.F_CrtYd)
    for v in res["vias"]:
        assert not court.Collide(pt(pcbnew, v["x"], v["y"]), mm(pcbnew, 0.3))


def test_stitch_ground_commit_adds_locked_net_vias(pcbnew, pour_board):
    from commands.finishing import FinishingCommands

    board, n = pour_board
    res = FinishingCommands(Iface(board)).stitch_ground({"grid": 4.0, "edgePitch": 0})
    assert res["success"] is True, res
    vias = vias_of(pcbnew, board)
    assert len(vias) == res["placed"]["total"] > 0
    assert all(v.GetNetname() == "GND" and v.IsLocked() for v in vias)
    assert res["zonesRefilled"] is True
    # a second pass finds nothing new on the same grid (spacing rule)
    again = FinishingCommands(Iface(board)).stitch_ground(
        {"grid": 4.0, "edgePitch": 0, "dryRun": True}
    )
    assert again["placed"]["total"] == 0
    assert again["rejected"].get("spacing", 0) > 0


def test_stitch_ground_fence_region_and_layer_requirements(pcbnew, pour_board):
    from commands.finishing import FinishingCommands

    board, _ = pour_board
    fc = FinishingCommands(Iface(board))
    res = fc.stitch_ground(
        {
            "grid": 0,
            "edgePitch": 0,
            "fencePolylines": [{"points": [[8, 22], [32, 22]]}],
            "fencePitch": 1.5,
            "dryRun": True,
        }
    )
    assert res["success"] is True, res
    assert res["placed"]["fence"] >= 15
    assert all(abs(v["y"] - 22) <= 0.41 for v in res["vias"])

    region = fc.stitch_ground(
        {
            "grid": 2.0,
            "edgePitch": 0,
            "region": {"x1": 2, "y1": 2, "x2": 12, "y2": 12},
            "dryRun": True,
        }
    )
    assert region["placed"]["total"] > 0
    assert all(2 <= v["x"] <= 12 and 2 <= v["y"] <= 12 for v in region["vias"])

    three = fc.stitch_ground({"grid": 3.0, "edgePitch": 0, "minLayers": 3, "dryRun": True})
    assert three["placed"]["total"] == 0  # only two layers carry the pour
    assert three["rejected"].get("noPour", 0) > 0


def test_stitch_ground_errors(pcbnew, pour_board):
    from commands.finishing import FinishingCommands

    board, _ = pour_board
    fc = FinishingCommands(Iface(board))
    assert fc.stitch_ground({"net": "SIG"})["success"] is False  # no SIG pours
    assert fc.stitch_ground({"net": "NOPE"})["success"] is False
    bad = fc.stitch_ground({"via": {"size": 0.3, "drill": 0.4}})
    assert bad["success"] is False and "via" in bad["message"].lower()
    assert FinishingCommands(Iface(None)).stitch_ground({})["success"] is False


# --------------------------------------------------------------------------- #
# F2 tidy_silkscreen / add_pin_labels
# --------------------------------------------------------------------------- #


@pytest.fixture
def silk_board(pcbnew):
    b = pcbnew.BOARD()
    outline(pcbnew, b)
    n = nets(pcbnew, b, ["A", "B"])
    for i, x in enumerate((10, 12, 14, 16)):
        fp = footprint(
            pcbnew,
            b,
            "Resistor_SMD",
            "R_0603_1608Metric",
            f"R{i + 1}",
            x,
            10,
            angle=90,
            pad_nets={"1": n["A"], "2": n["B"]},
        )
        # drop every reference on its neighbour's pads
        fp.Reference().SetPosition(pt(pcbnew, x + 2, 10))
        fp.Reference().SetTextAngleDegrees(0)
    return b


def _silk_collisions(pcbnew, board) -> int:
    """References whose stroke shape touches a pad or another reference."""
    refs = [f.Reference() for f in board.GetFootprints() if f.Reference().IsVisible()]
    pads = [(p, p.GetEffectiveShape(pcbnew.F_Cu)) for f in board.GetFootprints() for p in f.Pads()]
    hits = 0
    for r in refs:
        shape = r.GetEffectiveShape(pcbnew.F_SilkS)
        if any(ps.Collide(shape, 0) for _, ps in pads):
            hits += 1
            continue
        for other in refs:
            if other is not r and other.GetEffectiveShape(pcbnew.F_SilkS).Collide(shape, 0):
                hits += 1
                break
    return hits


def test_tidy_silkscreen_resolves_overlaps(pcbnew, silk_board):
    from commands.finishing import FinishingCommands

    assert _silk_collisions(pcbnew, silk_board) > 0
    fc = FinishingCommands(Iface(silk_board))
    dry = fc.tidy_silkscreen({"textHeight": 0.8, "dryRun": True})
    assert dry["success"] is True and dry["collidingBefore"] >= 3
    assert _silk_collisions(pcbnew, silk_board) > 0  # dry run restored everything

    res = fc.tidy_silkscreen({"textHeight": 0.8})
    assert res["success"] is True, res
    assert res["moved"] + len(res["hidden"]) >= res["collidingBefore"]
    assert _silk_collisions(pcbnew, silk_board) == 0
    for f in silk_board.GetFootprints():
        r = f.Reference()
        assert r.GetTextHeight() == mm(pcbnew, 0.8)
        assert round(r.GetTextAngleDegrees()) % 90 == 0
    # idempotent: nothing collides any more, so everything is kept
    again = fc.tidy_silkscreen({"dryRun": True})
    assert again["collidingBefore"] == 0 and again["moved"] == 0


def test_tidy_silkscreen_uses_strokes_not_boxes(pcbnew):
    """A reference inside an outline rectangle (clear of its strokes) is not a collision."""
    from commands.finishing import FinishingCommands

    b = pcbnew.BOARD()
    outline(pcbnew, b)
    fp = footprint(pcbnew, b, "Resistor_SMD", "R_0603_1608Metric", "R1", 20, 16)
    rect = pcbnew.PCB_SHAPE(b)
    rect.SetShape(pcbnew.SHAPE_T_RECT)
    rect.SetStart(pt(pcbnew, 12, 8))
    rect.SetEnd(pt(pcbnew, 28, 22))
    rect.SetLayer(pcbnew.F_SilkS)
    rect.SetWidth(mm(pcbnew, 0.12))
    rect.SetFilled(False)
    b.Add(rect)
    fp.Reference().SetPosition(pt(pcbnew, 20, 13))
    res = FinishingCommands(Iface(b)).tidy_silkscreen({"textHeight": 0.8, "dryRun": True})
    assert res["collidingBefore"] == 0 and res["kept"] == 1, res


def test_tidy_silkscreen_hides_when_nothing_fits(pcbnew):
    from commands.finishing import FinishingCommands

    b = pcbnew.BOARD()
    outline(pcbnew, b, 3.0, 3.0)  # far too small for any text beside the part
    fp = footprint(pcbnew, b, "Resistor_SMD", "R_0603_1608Metric", "R1", 1.5, 1.5)
    fp.Reference().SetPosition(pt(pcbnew, 1.5, 1.5))
    fc = FinishingCommands(Iface(b))
    keep = fc.tidy_silkscreen({"hideIfNoFit": False, "dryRun": True})
    assert keep["unresolved"] == ["R1"] and keep["hidden"] == []
    res = fc.tidy_silkscreen({})
    assert res["hidden"] == ["R1"]
    assert not fp.Reference().IsVisible()


def test_add_pin_labels(pcbnew):
    from commands.finishing import FinishingCommands

    b = pcbnew.BOARD()
    outline(pcbnew, b)
    footprint(
        pcbnew, b, "Connector_PinHeader_2.54mm", "PinHeader_1x04_P2.54mm_Vertical", "J1", 20, 10
    )
    fc = FinishingCommands(Iface(b))
    labels = {"1": "5V", "2": "GND", "3": "SDA", "4": "SCL"}
    dry = fc.add_pin_labels({"ref": "J1", "labels": labels, "textHeight": 0.8, "dryRun": True})
    assert dry["success"] is True and len(dry["placed"]) == 4, dry
    assert not [d for d in b.GetDrawings() if isinstance(d, pcbnew.PCB_TEXT)]

    res = fc.add_pin_labels({"ref": "J1", "labels": labels, "textHeight": 0.8})
    texts = [d for d in b.GetDrawings() if isinstance(d, pcbnew.PCB_TEXT)]
    assert sorted(t.GetText() for t in texts) == sorted(labels.values())
    assert all(t.GetLayer() == pcbnew.F_SilkS for t in texts)
    # labels sit beside their own pin (same y as the pad within half a pitch)
    j1 = b.FindFootprintByReference("J1")
    pad_y = {str(p.GetNumber()): pcbnew.ToMM(p.GetPosition().y) for p in j1.Pads()}
    for item in res["placed"]:
        if item["side"] in ("left", "right"):
            assert abs(item["y"] - pad_y[item["pad"]]) < 1.27
    shapes = [t.GetEffectiveShape(pcbnew.F_SilkS) for t in texts]
    for i, a in enumerate(shapes):
        for bshape in shapes[i + 1 :]:
            assert not a.Collide(bshape, 0)
    bad = fc.add_pin_labels({"ref": "J1", "labels": {"9": "X"}})
    assert bad["success"] is False and "9" in bad["message"]
    assert fc.add_pin_labels({"ref": "J9", "labels": {"1": "X"}})["success"] is False


# --------------------------------------------------------------------------- #
# F4 cleanup_dangling
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_board(pcbnew):
    b = pcbnew.BOARD()
    outline(pcbnew, b)
    n = nets(pcbnew, b, ["A", "GND"])
    r1 = footprint(
        pcbnew, b, "Resistor_SMD", "R_0603_1608Metric", "R1", 10, 10, pad_nets={"1": n["A"]}
    )
    r2 = footprint(
        pcbnew, b, "Resistor_SMD", "R_0603_1608Metric", "R2", 20, 10, pad_nets={"1": n["A"]}
    )
    p1 = [p for p in r1.Pads() if p.GetNumber() == "1"][0].GetPosition()
    p2 = [p for p in r2.Pads() if p.GetNumber() == "1"][0].GetPosition()
    x1, x2, y = pcbnew.ToMM(p1.x), pcbnew.ToMM(p2.x), pcbnew.ToMM(p1.y)
    # one segment runs from R1.1 through R2.1 and overshoots by 5 mm -> trim
    track(pcbnew, b, n["A"], (x1, y), (x2 + 5, y))
    # a two-segment stub chain hanging off R1.1 -> removed segment by segment
    track(pcbnew, b, n["A"], (x1, y), (x1, y + 5))
    track(pcbnew, b, n["A"], (x1, y + 5), (x1 - 4, y + 5))
    # an orphan via and a stitching via inside a GND pour
    via(pcbnew, b, n["A"], 30, 25)
    zone(pcbnew, b, n["GND"], pcbnew.F_Cu, [(25, 2), (38, 2), (38, 12), (25, 12)])
    zone(pcbnew, b, n["GND"], pcbnew.B_Cu, [(25, 2), (38, 2), (38, 12), (25, 12)])
    via(pcbnew, b, n["GND"], 31, 7)
    fill(pcbnew, b)
    return b, (x1, x2, y)


def test_cleanup_dangling_trims_and_removes(pcbnew, stub_board):
    from commands.finishing import FinishingCommands

    board, (x1, x2, y) = stub_board
    fc = FinishingCommands(Iface(board))
    n_tracks, n_vias = len(tracks_of(pcbnew, board)), len(vias_of(pcbnew, board))
    dry = fc.cleanup_dangling({"dryRun": True})
    assert dry["success"] is True, dry
    assert (dry["trimmedTracks"], dry["removedTracks"], dry["removedVias"]) == (1, 2, 1)
    assert len(tracks_of(pcbnew, board)) == n_tracks and len(vias_of(pcbnew, board)) == n_vias

    res = fc.cleanup_dangling({})
    assert res["success"] is True, res
    assert res["connectivity"]["netsWorse"] == {}
    remaining = tracks_of(pcbnew, board)
    assert len(remaining) == 1
    t = remaining[0]
    ends = sorted([pcbnew.ToMM(t.GetStart().x), pcbnew.ToMM(t.GetEnd().x)])
    assert ends[0] == pytest.approx(x1, abs=1e-3)
    assert ends[1] == pytest.approx(x2, abs=1e-3)  # trimmed back to R2.1, not deleted
    left = vias_of(pcbnew, board)
    assert [v.GetNetname() for v in left] == ["GND"]  # stitching via kept
    board.BuildConnectivity()
    assert board.GetConnectivity().GetUnconnectedCount(False) == 0


def test_cleanup_dangling_rolls_back_when_connectivity_worsens(pcbnew, stub_board):
    from commands.finishing import FinishingCommands

    board, _ = stub_board
    fc = FinishingCommands(Iface(board))
    snapshot = sorted(
        (pcbnew.ToMM(t.GetStart()), pcbnew.ToMM(t.GetEnd())) for t in tracks_of(pcbnew, board)
    )
    # Force a wrong plan: delete every dangling track outright, including the
    # one that connects R1 to R2.
    with patch.object(FinishingCommands, "_trim_plan", lambda *a, **k: ("remove", False, None)):
        res = fc.cleanup_dangling({"removeVias": False})
    assert res["success"] is False and res.get("rolledBack") is True, res
    assert res["connectivity"]["netsWorse"]
    after = sorted(
        (pcbnew.ToMM(t.GetStart()), pcbnew.ToMM(t.GetEnd())) for t in tracks_of(pcbnew, board)
    )
    assert after == snapshot


# --------------------------------------------------------------------------- #
# R4 transplant_layout
# --------------------------------------------------------------------------- #


def _make_pair(pcbnew, tmp_path, angle=90.0, back=False, swap_cap=False):
    """Source board saved to disk + target board in memory with the same parts moved."""

    def populate(board, ox, oy, ang, flip, swap):
        n = nets(pcbnew, board, ["VCC", "GND", "SIG"])
        u = footprint(
            pcbnew,
            board,
            "Package_SO",
            "SOIC-8_3.9x4.9mm_P1.27mm",
            "U1",
            ox,
            oy,
            angle=ang,
            pad_nets={"1": n["SIG"], "4": n["GND"], "8": n["VCC"]},
            back=flip,
        )
        # Place the cap in the anchor's own frame, read from its pads (local x:
        # pin 1 -> pin 8, local y: pin 1 -> pin 4), so the fixture does not
        # depend on how KiCad composes flip and orientation.
        pp = {str(p.GetNumber()): p.GetPosition() for p in u.Pads()}
        ex = ((pp["8"].x - pp["1"].x) / 4.95e6, (pp["8"].y - pp["1"].y) / 4.95e6)
        ey = ((pp["4"].x - pp["1"].x) / 3.81e6, (pp["4"].y - pp["1"].y) / 3.81e6)
        org = u.GetPosition()

        def local(lx, ly):
            return (
                org.x + (lx * ex[0] + ly * ey[0]) * 1e6,
                org.y + (lx * ex[1] + ly * ey[1]) * 1e6,
            )

        cx, cy = local(7.0, 0.0)
        want1 = local(7.0, 0.775)  # C1 pad 1 sits at local +y in the source
        pads = {"1": n["GND"], "2": n["VCC"]} if swap else {"1": n["VCC"], "2": n["GND"]}
        cap = footprint(
            pcbnew,
            board,
            "Capacitor_SMD",
            "C_0603_1608Metric",
            "C1",
            cx / 1e6,
            cy / 1e6,
            pad_nets=pads,
            back=flip,
        )
        for orient in (0, 90, 180, 270):
            cap.SetOrientationDegrees(orient)
            p1 = [p for p in cap.Pads() if str(p.GetNumber()) == "1"][0].GetPosition()
            if math.hypot(p1.x - want1[0], p1.y - want1[1]) < 1000:
                break
        else:
            raise AssertionError("could not orient C1 in the anchor frame")
        return n, u

    src = pcbnew.BOARD()
    outline(pcbnew, src, 60, 60)
    n, u = populate(src, 20, 20, 0.0, False, False)
    c1 = src.FindFootprintByReference("C1")
    pad = {(f.GetReference(), str(p.GetNumber())): p for f in (u, c1) for p in f.Pads()}

    def xy(key):
        p = pad[key].GetPosition()
        return pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)

    # VCC: U1.8 -> C1.1 as an L on F.Cu (3 segments)
    (ax, ay), (bx, by) = xy(("U1", "8")), xy(("C1", "1"))
    for a, b in (((ax, ay), (25.5, ay)), ((25.5, ay), (25.5, by)), ((25.5, by), (bx, by))):
        track(pcbnew, src, n["VCC"], a, b)
    # GND: U1.4 down, via, across on B.Cu, via, up on F.Cu to C1.2 (4 segments)
    (gx, gy), (cx2, cy2) = xy(("U1", "4")), xy(("C1", "2"))
    track(pcbnew, src, n["GND"], (gx, gy), (gx, 24.0))
    via(pcbnew, src, n["GND"], gx, 24.0)
    track(pcbnew, src, n["GND"], (gx, 24.0), (28.5, 24.0), layer=pcbnew.B_Cu)
    via(pcbnew, src, n["GND"], 28.5, 24.0)
    track(pcbnew, src, n["GND"], (28.5, 24.0), (28.5, cy2))
    track(pcbnew, src, n["GND"], (28.5, cy2), (cx2, cy2))
    zone(pcbnew, src, n["GND"], pcbnew.F_Cu, [(27.6, 22.6), (30, 22.6), (30, 25.5), (27.6, 25.5)])
    zone(pcbnew, src, None, pcbnew.F_Cu, [(18, 15), (19, 15), (19, 16), (18, 16)], rule=True)
    path = tmp_path / "source.kicad_pcb"
    pcbnew.SaveBoard(str(path), src)

    tgt = pcbnew.BOARD()
    outline(pcbnew, tgt, 80, 80)
    populate(tgt, 50, 45, angle, back, swap_cap)
    tgt.BuildConnectivity()
    return str(path), tgt


def _pad_xy(pcbnew, board, ref, num):
    fp = board.FindFootprintByReference(ref)
    p = [p for p in fp.Pads() if str(p.GetNumber()) == num][0].GetPosition()
    return p.x, p.y


@pytest.mark.parametrize("angle,back", [(90.0, False), (180.0, False), (0.0, True)])
def test_transplant_layout_copies_with_transform(pcbnew, tmp_path, angle, back):
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=angle, back=back)
    before = tgt.GetConnectivity().GetUnconnectedCount(False)
    assert before > 0
    tc = TransplantCommands(Iface(tgt))
    params = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
    }
    dry = tc.transplant_layout(dict(params, dryRun=True))
    assert dry["success"] is True, dry
    assert dry["verification"]["mismatchCount"] == 0
    assert not tracks_of(pcbnew, tgt)  # dry run copied nothing

    res = tc.transplant_layout(params)
    assert res["success"] is True, res
    assert res["transform"]["mirrored"] is back
    assert res["copied"]["tracks"] == 7 and res["copied"]["vias"] == 2
    assert res["copied"]["zones"] == 1 and res["copied"]["ruleAreas"] == 1
    assert res["verification"]["padsMatched"] == res["verification"]["padsChecked"] > 0
    assert res["netMap"]["count"] == 3 and res["netMap"]["renamed"] == {}

    # copied VCC copper starts and ends exactly on the target pads
    vcc = [t for t in tracks_of(pcbnew, tgt) if t.GetNetname() == "VCC"]
    ends = [(p.x, p.y) for t in vcc for p in (t.GetStart(), t.GetEnd())]
    for ref, num in (("U1", "8"), ("C1", "1")):
        x, y = _pad_xy(pcbnew, tgt, ref, num)
        assert min(math.hypot(x - ex, y - ey) for ex, ey in ends) < 1000  # < 1 um
    on_f = sum(1 for t in tracks_of(pcbnew, tgt) if t.GetLayer() == pcbnew.F_Cu)
    on_b = sum(1 for t in tracks_of(pcbnew, tgt) if t.GetLayer() == pcbnew.B_Cu)
    assert (on_f, on_b) == ((1, 6) if back else (6, 1))
    zones = [z for z in tgt.Zones() if not z.GetIsRuleArea()]
    assert [z.GetLayerSet().CuStack()[0] for z in zones] == [pcbnew.B_Cu if back else pcbnew.F_Cu]
    tgt.BuildConnectivity()
    assert tgt.GetConnectivity().GetUnconnectedCount(False) == 0
    assert res["report"]["shorts"] == []
    assert res["report"]["ruleAreas"][0]["pourOnly"] is True


def test_transplant_layout_reports_mismatch_and_copies_nothing(pcbnew, tmp_path):
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, swap_cap=True)
    res = TransplantCommands(Iface(tgt)).transplant_layout(
        {
            "sourceBoard": src_path,
            "sourceAnchorRef": "U1",
            "targetAnchorRef": "U1",
            "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
        }
    )
    assert res["success"] is False
    kinds = {m["type"] for m in res["verification"]["mismatches"]}
    assert "netMismatch" in kinds
    bad = {m["targetPad"] for m in res["verification"]["mismatches"] if m["type"] == "netMismatch"}
    assert bad == {"C1.1", "C1.2"}
    assert not tracks_of(pcbnew, tgt) and not vias_of(pcbnew, tgt)


def test_transplant_layout_param_errors(pcbnew, tmp_path):
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path)
    tc = TransplantCommands(Iface(tgt))
    assert (
        tc.transplant_layout(
            {
                "sourceBoard": str(tmp_path / "nope.kicad_pcb"),
                "sourceAnchorRef": "U1",
                "targetAnchorRef": "U1",
            }
        )["success"]
        is False
    )
    r = tc.transplant_layout(
        {"sourceBoard": src_path, "sourceAnchorRef": "U9", "targetAnchorRef": "U1"}
    )
    assert r["success"] is False and "U9" in r["message"]
    r = tc.transplant_layout(
        {
            "sourceBoard": src_path,
            "sourceAnchorRef": "U1",
            "targetAnchorRef": "U1",
            "include": ["wires"],
        }
    )
    assert r["success"] is False


# --------------------------------------------------------------------------- #
# Through the interface: auto-save after a committed change only
# --------------------------------------------------------------------------- #


def test_interface_routes_and_autosaves(pcbnew, tmp_path, pour_board):
    from kicad_interface import KiCADInterface

    board, _ = pour_board
    path = tmp_path / "stitch.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        iface = KiCADInterface()
    opened = iface.handle_command("open_board", {"boardPath": str(path)})
    assert opened.get("success"), opened
    mtime = path.stat().st_mtime_ns
    dry = iface.handle_command("stitch_ground", {"grid": 4.0, "edgePitch": 0, "dryRun": True})
    assert dry["success"] and path.stat().st_mtime_ns == mtime  # dry run: no write
    res = iface.handle_command("stitch_ground", {"grid": 4.0, "edgePitch": 0})
    assert res["success"] and res["saved"] is True, res
    reloaded = pcbnew.LoadBoard(str(path))
    assert len(vias_of(pcbnew, reloaded)) == res["placed"]["total"]
    # the next mutation is not refused as an "external change"
    again = iface.handle_command("cleanup_dangling", {"dryRun": True})
    assert again["success"] is True
