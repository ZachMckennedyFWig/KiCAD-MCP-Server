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


def _make_pair(
    pcbnew,
    tmp_path,
    angle=90.0,
    back=False,
    swap_cap=False,
    src_anchor="SOIC-8_3.9x4.9mm_P1.27mm",
    tgt_anchor="SOIC-8_3.9x4.9mm_P1.27mm",
    cap_shift=0.0,
    sig_pour=False,
):
    """Source board saved to disk + target board in memory with the same parts moved.

    ``cap_shift`` displaces the target's C1 along the anchor's local x (mm);
    ``sig_pour`` adds a SIG stub ending in a SIG pour that straddles the
    region's left edge (x = 10) in the source.
    """

    def populate(board, ox, oy, ang, flip, swap, fp_name, shift=0.0):
        n = nets(pcbnew, board, ["VCC", "GND", "SIG"])
        u = footprint(
            pcbnew,
            board,
            "Package_SO",
            fp_name,
            "U1",
            ox,
            oy,
            angle=ang,
            pad_nets={"1": n["SIG"], "4": n["GND"], "8": n["VCC"], "9": n["GND"]},
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

        cx, cy = local(7.0 + shift, 0.0)
        want1 = local(7.0 + shift, 0.775)  # C1 pad 1 sits at local +y in the source
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
    n, u = populate(src, 20, 20, 0.0, False, False, src_anchor)
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
    if sig_pour:
        sx, sy = xy(("U1", "1"))
        track(pcbnew, src, n["SIG"], (sx, sy), (14.0, sy))
        zone(pcbnew, src, n["SIG"], pcbnew.F_Cu, [(8, 17.8), (15, 17.8), (15, 18.4), (8, 18.4)])
    path = tmp_path / "source.kicad_pcb"
    pcbnew.SaveBoard(str(path), src)

    tgt = pcbnew.BOARD()
    outline(pcbnew, tgt, 80, 80)
    populate(tgt, 50, 45, angle, back, swap_cap, tgt_anchor, cap_shift)
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
    # the anchor's pins fix the net map, so the swapped cap is named as
    # reversed rather than reported as two nets folded together
    assert [r["targetRef"] for r in res["verification"]["reversedParts"]] == ["C1"]
    assert "netMerge" not in kinds
    assert res["verification"]["blockingCount"] == 2
    assert not tracks_of(pcbnew, tgt) and not vias_of(pcbnew, tgt)


def test_transplant_layout_nets_filter_makes_other_mismatches_non_blocking(pcbnew, tmp_path):
    """Only the selected nets are copied; mismatches on the others are reported, not fatal."""
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=0.0, swap_cap=True)
    base = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
    }
    res = TransplantCommands(Iface(tgt)).transplant_layout(dict(base, nets=["SIG"]))
    assert res["success"] is True, res
    v = res["verification"]
    assert v["mismatchCount"] >= 2 and v["blockingCount"] == 0
    assert all(m["blocking"] is False for m in v["mismatches"])
    assert res["netMap"]["copied"] == ["SIG"]
    assert res["copied"]["tracks"] == 0 and res["copied"]["vias"] == 0
    assert res["copied"]["zones"] == 0 and res["copied"]["ruleAreas"] == 1
    assert res["skipped"]["notSelected"] == 7 + 2 + 1  # VCC/GND tracks, vias, GND zone
    # a name that is on neither board is a caller error, not a silent no-op
    bad = TransplantCommands(Iface(tgt)).transplant_layout(dict(base, nets=["NOPE"]))
    assert bad["success"] is False and "NOPE" in bad["errorDetails"]


def test_transplant_layout_split_exposed_pad_becomes_board_vias(pcbnew, tmp_path):
    """The RP2350 case: the reference anchor's exposed pad carries plated via pads, the
    target's library footprint has a plain exposed pad. The via pads match by
    containment, become board vias grown to the board's minimum drill, and a
    second run adds nothing."""
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(
        pcbnew,
        tmp_path,
        angle=90.0,
        src_anchor="SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm_ThermalVias",
        tgt_anchor="SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm",
    )
    ds = tgt.GetDesignSettings()
    ds.m_MinThroughDrill = pcbnew.FromMM(0.35)
    src = pcbnew.LoadBoard(src_path)
    holes = [
        p
        for p in src.FindFootprintByReference("U1").Pads()
        if str(p.GetNumber()) == "9" and p.GetDrillSize().x > 0
    ]
    assert holes, "fixture footprint should have thermal via pads"
    params = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
    }
    tc = TransplantCommands(Iface(tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is True, res
    v = res["verification"]
    assert v["blockingCount"] == 0, v["mismatches"]
    assert not [m for m in v["mismatches"] if m["type"] == "anchorPadMissing"]
    assert v["offsetPadCount"] >= len(holes) - 1  # the centre one may match exactly
    assert res["copied"]["padVias"] == len(holes)
    assert res["copied"]["vias"] == 2 and res["copied"]["tracks"] == 7
    drills = {round(pcbnew.ToMM(x.GetDrillValue()), 3) for x in vias_of(pcbnew, tgt)}
    assert drills == {0.35}
    assert res["report"]["viasResized"]
    # every new pad via sits on the target's exposed pad
    ep = [p for p in tgt.FindFootprintByReference("U1").Pads() if str(p.GetNumber()) == "9"][0]
    on_ep = [x for x in vias_of(pcbnew, tgt) if ep.HitTest(x.GetPosition())]
    assert len(on_ep) == len(holes)
    assert all(x.GetNetname() == "GND" for x in on_ep)

    # idempotent: everything is already there
    n_tracks, n_vias, n_zones = (
        len(tracks_of(pcbnew, tgt)),
        len(vias_of(pcbnew, tgt)),
        len(list(tgt.Zones())),
    )
    again = tc.transplant_layout(params)
    assert again["success"] is True, again
    assert sum(again["copied"].values()) == 0
    assert again["skipped"]["alreadyPresent"] == sum(res["copied"].values())
    assert (len(tracks_of(pcbnew, tgt)), len(vias_of(pcbnew, tgt)), len(list(tgt.Zones()))) == (
        n_tracks,
        n_vias,
        n_zones,
    )

    # replaceExisting swaps the copied zones/rule areas instead of stacking them,
    # and keeps the vias in the exposed pad
    rep = tc.transplant_layout(dict(params, replaceExisting=True))
    assert rep["success"] is True, rep
    assert rep["removedExisting"] > 0
    assert len(list(tgt.Zones())) == n_zones
    assert len(vias_of(pcbnew, tgt)) == n_vias
    assert rep["copied"]["zones"] == 1 and rep["copied"]["ruleAreas"] == 1


def test_transplant_layout_displaced_part_is_a_pad_offset(pcbnew, tmp_path):
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=0.0, cap_shift=0.2)
    params = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
        "dryRun": True,
    }
    tc = TransplantCommands(Iface(tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is False
    offs = [m for m in res["verification"]["mismatches"] if m["type"] == "padOffset"]
    assert {m["targetPad"] for m in offs} == {"C1.1", "C1.2"}
    assert all(m["blocking"] and abs(m["offset"] - 0.2) < 0.01 for m in offs)
    ok = tc.transplant_layout(dict(params, tolerance=0.25))
    assert ok["success"] is True and ok["verification"]["mismatchCount"] == 0, ok


def test_transplant_layout_clearance_and_skip_conflicting(pcbnew, tmp_path):
    """Existing target copper near/over the copied path: a clearance hit is reported,
    a short rolls back, and skipConflicting drops just the offending items."""
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=0.0)
    u1 = tgt.FindFootprintByReference("U1")
    sig = [p for p in u1.Pads() if str(p.GetNumber()) == "1"][0].GetNet()
    assert sig.GetNetname() == "SIG"
    # translation is (+30, +25): the copied VCC run is (52.475,43.095)->(55.5,43.095)
    # ->(55.5,45.775)->(57,45.775), 0.25 mm wide
    track(pcbnew, tgt, sig, (54.0, 42.82), (55.0, 42.82), width=0.2)  # 0.05 mm gap
    params = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
        "dryRun": True,
    }
    tc = TransplantCommands(Iface(tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is True, res
    rep = res["report"]
    assert rep["shortCount"] == 0 and rep["clearanceCount"] == 1
    assert rep["clearance"][0]["otherNet"] == "SIG" and rep["clearance"][0]["net"] == "VCC"

    track(pcbnew, tgt, sig, (55.0, 44.5), (56.0, 44.5), width=0.2)  # crosses x = 55.5
    before = len(tracks_of(pcbnew, tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is False and res["rolledBack"] is True
    assert len(tracks_of(pcbnew, tgt)) == before

    res = tc.transplant_layout(dict(params, skipConflicting=True))
    assert res["success"] is True, res
    dropped = res["report"]["droppedConflicting"]
    assert len(dropped) == 2 and {d["net"] for d in dropped} == {"VCC"}
    assert res["report"]["shortCount"] == 0 and res["report"]["clearanceCount"] == 0
    assert res["copied"]["tracks"] == 5
    assert len(tracks_of(pcbnew, tgt)) == before  # dry run


def test_transplant_layout_short_to_single_pad_is_not_renetted_away(pcbnew, tmp_path):
    """A copied stub landing on one pad of another net: BuildConnectivity() would
    give the stub that pad's net, hiding the short. It must still be reported."""
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=0.0, sig_pour=True)
    u1 = tgt.FindFootprintByReference("U1")
    pads = {str(p.GetNumber()): p for p in u1.Pads()}
    pads["1"].SetNet(pads["8"].GetNet())  # target pin 1 is VCC, the source's is SIG
    params = {
        "sourceBoard": src_path,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
        "netMap": {"SIG": "SIG"},
        "nets": ["SIG"],
        "dryRun": True,
    }
    tc = TransplantCommands(Iface(tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is False and res["verification"]["blockingCount"] == 1
    forced = tc.transplant_layout(dict(params, force=True))
    assert forced["success"] is True and forced["copied"]["tracks"] == 1, forced
    shorts = forced["report"]["shorts"]
    assert [(s["net"], s["otherNet"]) for s in shorts] == [("SIG", "VCC")]
    res = tc.transplant_layout(dict(params, verify=False))
    assert res["success"] is False and res.get("rolledBack") is True
    assert not tracks_of(pcbnew, tgt)


def test_transplant_layout_explains_track_ending_in_uncopied_pour(pcbnew, tmp_path):
    from commands.transplant import TransplantCommands

    src_path, tgt = _make_pair(pcbnew, tmp_path, angle=90.0, sig_pour=True)
    res = TransplantCommands(Iface(tgt)).transplant_layout(
        {
            "sourceBoard": src_path,
            "sourceAnchorRef": "U1",
            "targetAnchorRef": "U1",
            "region": {"x1": 10, "y1": 12, "x2": 32, "y2": 28},
            "dryRun": True,
        }
    )
    assert res["success"] is True, res
    partial = res["skipped"]["partialZones"]
    assert [z["net"] for z in partial] == ["SIG"]
    sig = [o for o in res["report"]["orphaned"] if o["net"] == "SIG"]
    assert len(sig) == 1 and "SIG pour" in sig[0]["reason"]


def test_transplant_layout_rp2350_reference(pcbnew, tmp_path):
    """Opt-in: Raspberry Pi's RP2350B minimal design (MIT) onto a copy of itself with
    the core rotated 90 deg and U1 swapped for KiCad's stock QFN-80 footprint, as
    on a real board. Set KICAD_RP2350_REFERENCE to the RPI-RP2350B-MINIMAL
    .kicad_pcb (from https://pip.raspberrypi.com/documents/RP-010329-CA)."""
    ref = os.environ.get("KICAD_RP2350_REFERENCE")
    if not ref or not os.path.isfile(ref):
        pytest.skip("set KICAD_RP2350_REFERENCE to the RP2350B minimal .kicad_pcb")
    from commands.transplant import TransplantCommands
    from utils.board_items import delete_board_item
    from utils.footprint_swap import load_library_footprint

    board = pcbnew.LoadBoard(ref)
    for t in list(board.GetTracks()):
        delete_board_item(board, t)
    for z in list(board.Zones()):
        delete_board_item(board, z)
    old = board.FindFootprintByReference("U1")
    assert old is not None and pcbnew.ToMM(old.GetPosition().x) == pytest.approx(100.0)
    pad_nets = {str(p.GetNumber()): p.GetNet() for p in old.Pads() if str(p.GetNumber())}
    stock = load_library_footprint(
        str(_lib("Package_DFN_QFN")), "QFN-80-1EP_10x10mm_P0.4mm_EP3.4x3.4mm"
    )
    stock.SetReference("U1")
    stock.SetPosition(pt(pcbnew, 100, 100))
    delete_board_item(board, old)
    board.Add(stock)
    for p in stock.Pads():
        if str(p.GetNumber()) in pad_nets:
            p.SetNet(pad_nets[str(p.GetNumber())])
    centre = pt(pcbnew, 100, 100)
    for fp in board.GetFootprints():
        q = fp.GetPosition()
        if abs(q.x - centre.x) <= mm(pcbnew, 12) and abs(q.y - centre.y) <= mm(pcbnew, 12):
            fp.Rotate(centre, pcbnew.EDA_ANGLE(90.0, pcbnew.DEGREES_T))
            fp.Move(pt(pcbnew, -40, -30))
    target = tmp_path / "rp2350_target.kicad_pcb"
    pcbnew.SaveBoard(str(target), board)
    tgt = pcbnew.LoadBoard(str(target))

    power = ["+3V3", "+1V1", "GND", "/VREG_AVDD", "/VREG_LX"]
    params = {
        "sourceBoard": ref,
        "sourceAnchorRef": "U1",
        "targetAnchorRef": "U1",
        "region": {"x1": 89.4, "y1": 89.4, "x2": 110.6, "y2": 110.6},
        "nets": power,
        "tolerance": 0.06,  # RPi's QFN pins sit 0.05 mm inside KiCad's
        "limit": 500,
    }
    tc = TransplantCommands(Iface(tgt))
    res = tc.transplant_layout(params)
    assert res["success"] is True, {k: res.get(k) for k in ("message", "verification")}
    assert res["transform"]["rotationDeg"] == 90.0
    v = res["verification"]
    assert v["blockingCount"] == 0 and v["padsMatched"] == v["padsChecked"]
    assert v["offsetPadCount"] >= 8  # the reference's via pads inside the stock EP
    assert res["copied"]["padVias"] == 9
    assert res["copied"]["tracks"] > 50 and res["copied"]["vias"] > 10
    assert res["copied"]["zones"] >= 3 and res["copied"]["ruleAreas"] >= 1
    assert res["report"]["shortCount"] == 0
    conn = res["report"]["connectivity"]
    assert conn["unconnectedAfter"] < conn["unconnectedBefore"]
    # +3V3 stubs that ended in the reference's large +3V3 pour are explained
    stubs = [o for o in res["report"]["orphaned"] if o["net"] == "+3V3" and "reason" in o]
    assert stubs and all("pour" in o["reason"] for o in stubs)
    again = tc.transplant_layout(params)
    assert sum(again["copied"].values()) == 0


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
