"""Pure-geometry unit tests for tune_length (commands/length_tuning.py).

No pcbnew needed: the meander generator, corner treatment, amplitude solver
and the request parsing are plain Python. The board-backed parts (clearance,
writing, rollback, DRC on a real board) are in test_length_tuning_real.py.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from commands import length_tuning as lt  # noqa: E402

from utils import copper_geometry as cg  # noqa: E402

pytestmark = pytest.mark.unit

TROMBONE = 1.2  # slot step for spacing 0.6 (top + baseline gap)
ACCORDION = 0.6


def _slots(step, n, cap=1.0, x0=2.0, sides=None):
    sides = sides or [1 if step == TROMBONE else (1 if k % 2 == 0 else -1) for k in range(n)]
    return [(x0 + k * step, 0.6, sides[k], cap) for k in range(n)]


def _flat(pts):
    return [v for p in pts for v in p]


def _assert_continuous(prims, length):
    assert prims[0][1] == (0.0, 0.0)
    end = prims[-1][2] if prims[-1][0] == "line" else prims[-1][3]
    assert end == pytest.approx((length, 0.0))
    for p, q in zip(prims, prims[1:]):
        p_end = p[2] if p[0] == "line" else p[3]
        assert q[1] == pytest.approx(p_end, abs=1e-12)


# ------------------------------------------------------------------ polyline


def test_square_trombone_adds_two_h_per_top():
    pts = lt.meander_polyline(10.0, [(2.0, 0.6, 1, 0.5), (3.2, 0.6, 1, 0.5)])
    assert _flat(pts) == pytest.approx(
        _flat(
            [
                (0.0, 0.0),
                (2.0, 0.0),
                (2.0, 0.5),
                (2.6, 0.5),
                (2.6, 0.0),
                (3.2, 0.0),
                (3.2, 0.5),
                (3.8, 0.5),
                (3.8, 0.0),
                (10.0, 0.0),
            ]
        )
    )
    prims, added = lt.meander(10.0, [(2.0, 0.6, 1, 0.5), (3.2, 0.6, 1, 0.5)], 0.2, "square")
    assert added == pytest.approx(4 * 0.5)
    _assert_continuous(prims, 10.0)


def test_accordion_joins_adjacent_tops_with_one_leg():
    pts = lt.meander_polyline(10.0, [(2.0, 0.6, 1, 0.5), (2.6, 0.6, -1, 0.4)])
    # up, across, straight down through the baseline to the other side, across, back
    assert _flat(pts) == pytest.approx(
        _flat(
            [
                (0.0, 0.0),
                (2.0, 0.0),
                (2.0, 0.5),
                (2.6, 0.5),
                (2.6, -0.4),
                (3.2, -0.4),
                (3.2, 0.0),
                (10.0, 0.0),
            ]
        )
    )
    _, added = lt.meander(10.0, [(2.0, 0.6, 1, 0.5), (2.6, 0.6, -1, 0.4)], 0.2, "square")
    assert added == pytest.approx(0.5 + 0.9 + 0.4)


def test_no_tops_is_the_straight_segment():
    prims, added = lt.meander(5.0, [], 0.2, "chamfer")
    assert prims == [("line", (0.0, 0.0), (5.0, 0.0))]
    assert added == 0.0


# ------------------------------------------------------------------- corners


def test_chamfer_corners_are_45_degrees_and_cost_the_exact_saving():
    tops = [(2.0, 0.6, 1, 0.8)]
    prims, added = lt.meander(10.0, tops, 0.2, "chamfer")
    _assert_continuous(prims, 10.0)
    diagonals = 0
    for kind, a, b in prims:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx > 1e-9 and dy > 1e-9:
            assert dx == pytest.approx(dy)  # 45 degrees
            assert dx == pytest.approx(0.2)
            diagonals += 1
    assert diagonals == 4
    # each 90-degree corner cut by c saves c * (2 - sqrt 2)
    assert added == pytest.approx(2 * 0.8 - 4 * 0.2 * (2 - math.sqrt(2)))


def test_chamfer_is_limited_to_half_of_each_adjacent_segment():
    # a 0.2 mm high top: legs of 0.2 allow at most 0.1 per corner
    prims, _ = lt.meander(10.0, [(2.0, 0.6, 1, 0.2)], 0.5, "chamfer")
    for _, a, b in prims:
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx > 1e-9 and dy > 1e-9:
            assert dx == pytest.approx(0.1)


def test_round_corners_are_tangent_quarter_arcs():
    prims, added = lt.meander(10.0, [(2.0, 0.6, -1, 1.0)], 0.3, "round")
    _assert_continuous(prims, 10.0)
    arcs = [p for p in prims if p[0] == "arc"]
    assert len(arcs) == 4
    for _, a, mid, b, centre, r in arcs:
        assert r == pytest.approx(0.3)
        for p in (a, mid, b):
            assert math.hypot(p[0] - centre[0], p[1] - centre[1]) == pytest.approx(r)
        assert lt.prim_length(("arc", a, mid, b, centre, r)) == pytest.approx(r * math.pi / 2)
    assert added == pytest.approx(2.0 - 4 * 0.3 * (2 - math.pi / 2))


def test_arc_chords_follow_the_arc():
    _, a, mid, b, centre, r = [
        p for p in lt.meander(10.0, [(2.0, 0.6, 1, 1.0)], 0.3, "round")[0] if p[0] == "arc"
    ][0]
    chords = lt.prim_chords(("arc", a, mid, b, centre, r), pieces=4)
    assert chords[0][0] == a and chords[-1][1] == b
    for p, q in chords:
        for pt in (p, q):
            assert math.hypot(pt[0] - centre[0], pt[1] - centre[1]) == pytest.approx(r)


# ------------------------------------------------------------ amplitude solve


@pytest.mark.parametrize("corner", lt.CORNERS)
@pytest.mark.parametrize("step", [TROMBONE, ACCORDION])
@pytest.mark.parametrize("target", [0.35, 1.7, 3.3, 5.0])
def test_solver_hits_the_target_exactly(corner, step, target):
    size = 0.3 if corner == "round" else 0.2
    slots = _slots(step, 4)
    h, tops, added = lt.solve_amplitude(10.0, slots, target, size, corner)
    assert added == pytest.approx(target, abs=1e-9)
    assert 0 < h <= 1.0
    _, again = lt.meander(10.0, tops, size, corner)
    assert again == pytest.approx(target, abs=1e-9)


def test_solver_respects_each_slots_cap():
    slots = [(2.0, 0.6, 1, 1.0), (3.2, 0.6, 1, 0.3), (4.4, 0.6, 1, 1.0)]
    h, tops, added = lt.solve_amplitude(10.0, slots, 3.0, 0.2, "square")
    assert [t[3] for t in tops] == pytest.approx([h, 0.3, h])
    assert added == pytest.approx(3.0)
    assert h == pytest.approx((3.0 - 0.6) / 4)
    assert all(t[3] <= c[3] + 1e-12 for t, c in zip(tops, slots))


def test_solver_returns_full_amplitude_when_the_target_is_out_of_reach():
    slots = _slots(TROMBONE, 2, cap=0.5)
    h, tops, added = lt.solve_amplitude(10.0, slots, 50.0, 0.2, "square")
    assert h == 0.5
    assert added == pytest.approx(2.0)
    assert all(t[3] == 0.5 for t in tops)


def test_to_world_rotates_and_keeps_length():
    tops = [(2.0, 0.6, 1, 0.7), (3.2, 0.6, 1, 0.7)]
    local, added = lt.meander(6.0, tops, 0.2, "round")
    a, b = (10.0, 20.0), (10.0 + 6.0 / math.sqrt(2), 20.0 + 6.0 / math.sqrt(2))
    u = ((b[0] - a[0]) / 6.0, (b[1] - a[1]) / 6.0)
    world = lt.to_world(local, a, u, (-u[1], u[0]))
    assert lt.path_length(world) == pytest.approx(6.0 + added)
    assert world[0][1] == pytest.approx(a)
    assert world[-1][2] == pytest.approx(b)


# ---------------------------------------------------------------- region test


def test_region_distance():
    square = [(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)]
    inside = cg.Shape("circle", (1.0, 0.5, 0.1), ["F.Cu"], "X", "via")
    near = cg.Shape("circle", (1.0, 1.5, 0.2), ["F.Cu"], "X", "via")
    far_track = cg.Shape("capsule", (-1.0, 3.0, 3.0, 3.0, 0.1), ["F.Cu"], "X", "track")
    pad = cg.Shape("rect", (2.5, 0.0, 3.0, 1.0), ["F.Cu"], "X", "pad")
    assert lt.region_distance(square, inside) == 0.0
    assert lt.region_distance(square, near) == pytest.approx(0.3)
    assert lt.region_distance(square, far_track) == pytest.approx(1.9)
    assert lt.region_distance(square, pad) == pytest.approx(0.5)


# ------------------------------------------------------------ request parsing


class _Net:
    pass


class _Track:
    def __init__(self, net, length, cls="PCB_TRACK"):
        self._net, self._len, self._cls = net, length, cls

    def GetNetname(self):
        return self._net

    def GetClass(self):
        return self._cls

    def GetLength(self):
        return int(self._len * cg.NM)


class _Board:
    def __init__(self, lengths):
        self.tracks = [_Track(n, v) for n, v in lengths.items()] + [_Track("A", 99, "PCB_VIA")]

    def FindNet(self, name):
        return _Net() if name in ("A", "B", "C", "P", "N") else None

    def GetTracks(self):
        return self.tracks


def _jobs(params, lengths=None):
    board = _Board(lengths or {"A": 10.0, "B": 14.0, "C": 12.0, "P": 20.0, "N": 21.5})
    return lt.LengthTuner(board)._jobs(params)


def test_jobs_target_sources():
    jobs, lengths = _jobs({"net": "A", "targetLength": 15})
    assert lengths["A"] == pytest.approx(10.0)  # vias are not length
    assert [(j["net"], j["target"]) for j in jobs] == [("A", 15.0)]
    jobs, _ = _jobs({"net": "A", "matchTo": "C"})
    assert jobs[0]["target"] == pytest.approx(12.0)
    jobs, _ = _jobs({"net": "A", "matchTo": ["B", "C"]})
    assert jobs[0]["target"] == pytest.approx(14.0) and "B" in jobs[0]["source"]
    jobs, _ = _jobs({"nets": ["A", "B", "C"]})
    assert {j["net"]: j["target"] for j in jobs} == pytest.approx({"A": 14, "B": 14, "C": 14})


def test_jobs_pairs_tune_the_shorter_member():
    jobs, _ = _jobs({"pairs": [["P", "N"]], "pairTolerance": 0.05})
    assert len(jobs) == 1
    job = jobs[0]
    assert job["net"] == "P" and job["target"] == pytest.approx(21.5)
    assert job["pair"] == ["P", "N"] and job["tolerance"] == 0.05


@pytest.mark.parametrize(
    "params, text",
    [
        ({}, "give net"),
        ({"net": "A"}, "targetLength or matchTo"),
        ({"net": "A", "targetLength": 3, "matchTo": "B"}, "not both"),
        ({"net": "ZZ", "targetLength": 3}, "nets not found"),
        ({"pairs": [["P"]]}, "each pair"),
        ({"net": "P", "targetLength": 30, "pairs": [["P", "N"]]}, "both a net to tune"),
    ],
)
def test_jobs_reject_bad_requests(params, text):
    with pytest.raises(ValueError, match=text):
        _jobs(params)


class _Rules:
    def clearance(self, a, b):
        return 0.2

    def for_net(self, net):
        return {"clearance": 0.2}


def test_shape_options_defaults_and_validation():
    tuner = lt.LengthTuner(None)
    ctx = {"rules": _Rules()}
    so = tuner._shape_opts(ctx, "A", 0.2, {}, False)
    assert so["pitch"] == pytest.approx(0.6)  # 3 x width beats width + clearance
    assert so["amplitude"] == 1.0 and so["style"] == "trombone" and so["corner"] == "chamfer"
    pair = tuner._shape_opts(ctx, "A", 0.2, {}, True)
    assert pair["pitch"] == pytest.approx(0.4) and pair["amplitude"] == pytest.approx(0.4)
    wide = tuner._shape_opts(ctx, "A", 0.1, {"clearance": 0.5}, False)
    assert wide["pitch"] == pytest.approx(0.6)  # width + clearance beats 3 x width
    rounded = tuner._shape_opts(ctx, "A", 0.2, {"style": "rounded"}, False)
    assert rounded["style"] == "trombone" and rounded["corner"] == "round"
    with pytest.raises(ValueError, match="style"):
        tuner._shape_opts(ctx, "A", 0.2, {"style": "zigzag"}, False)
    with pytest.raises(ValueError, match="corner"):
        tuner._shape_opts(ctx, "A", 0.2, {"corner": "bevel"}, False)
    with pytest.raises(ValueError, match="spacing"):
        tuner._shape_opts(ctx, "A", 0.2, {"spacing": 0.3}, False)
    with pytest.raises(ValueError, match="amplitude"):
        tuner._shape_opts(ctx, "A", 0.2, {"amplitude": 0}, False)


def test_tune_length_without_a_board():
    assert lt.LengthTuner(None).tune_length({"net": "A"})["message"] == "No board is loaded"
