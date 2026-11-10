"""Pure-geometry tests for the parallel route_bus planner (no pcbnew)."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.bus_router import (  # noqa: E402
    OffsetError,
    clean_polyline,
    grid_tail_fix,
    lateral_offsets,
    normal,
    offset_polyline,
    order_members,
    parallel_fraction,
    polyline_length,
    segments_to_polylines,
)

S2 = math.sqrt(2.0)


def _seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / l2))
    return math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy))


def _poly_dist(p, poly):
    return min(_seg_dist(p, a, b) for a, b in zip(poly, poly[1:]))


def _angles(poly):
    out = []
    for a, b in zip(poly, poly[1:]):
        out.append(round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360, 6))
    return out


def _assert_parallel_at(guide, off_poly, offset):
    """Every offset segment is parallel to its guide segment at |offset|."""
    assert _angles(off_poly) == _angles(guide)
    for (a, b), (c, d) in zip(zip(guide, guide[1:]), zip(off_poly, off_poly[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy)
        n = (-dy / L, dx / L)
        for p in (c, d):
            signed = (p[0] - a[0]) * n[0] + (p[1] - a[1]) * n[1]
            assert signed == pytest.approx(offset, abs=1e-9)


# 45-degree bends, both turn directions (y down: +y is "south")
GUIDE_45_RIGHT = [(0.0, 0.0), (10.0, 0.0), (15.0, 5.0), (25.0, 5.0)]  # down then level: S-bend
GUIDE_45_ONE = [(0.0, 0.0), (10.0, 0.0), (20.0, 10.0)]  # one 45 turn towards +y
GUIDE_45_OTHER = [(0.0, 0.0), (10.0, 0.0), (20.0, -10.0)]  # one 45 turn towards -y
GUIDE_90_ONE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
GUIDE_90_OTHER = [(0.0, 0.0), (10.0, 0.0), (10.0, -10.0)]


@pytest.mark.parametrize(
    "guide", [GUIDE_45_RIGHT, GUIDE_45_ONE, GUIDE_45_OTHER, GUIDE_90_ONE, GUIDE_90_OTHER]
)
@pytest.mark.parametrize("offset", [-1.0, -0.5, 0.5, 1.0])
def test_offset_keeps_directions_and_perpendicular_spacing(guide, offset):
    poly, lays = offset_polyline(guide, offset)
    assert len(poly) == len(guide)
    _assert_parallel_at(guide, poly, offset)
    assert lays == [None] * (len(guide) - 1)


@pytest.mark.parametrize(
    "guide", [GUIDE_45_RIGHT, GUIDE_45_ONE, GUIDE_45_OTHER, GUIDE_90_ONE, GUIDE_90_OTHER]
)
def test_neighbour_spacing_equals_pitch_everywhere(guide):
    pitch = 0.5
    offs = lateral_offsets(4, pitch)
    polys = [offset_polyline(guide, o)[0] for o in offs]
    for a, b in zip(polys, polys[1:]):
        # each pair of corresponding segments lies on lines exactly pitch apart
        for (p, q), (r, s) in zip(zip(a, a[1:]), zip(b, b[1:])):
            dx, dy = q[0] - p[0], q[1] - p[1]
            L = math.hypot(dx, dy)
            n = (-dy / L, dx / L)
            for v in (r, s):
                assert (v[0] - p[0]) * n[0] + (v[1] - p[1]) * n[1] == pytest.approx(pitch)
        # never closer than pitch anywhere, and at pitch nearly everywhere
        # (only the outer side of a miter corner is farther away)
        for u, v in ((a, b), (b, a)):
            n_samples = 400
            pts = []
            for p, q in zip(u, u[1:]):
                for k in range(n_samples // len(u)):
                    t = k / (n_samples // len(u))
                    pts.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
            assert min(_poly_dist(x, v) for x in pts) >= pitch - 1e-9
            assert parallel_fraction(u, v, pitch, tol=1e-6, step=0.05) > 0.9


def test_miter_vertex_positions_45_and_90():
    # 90 degree left/right: miter vertex at (10 -/+ o, o) etc.
    poly, _ = offset_polyline(GUIDE_90_ONE, 1.0)  # normal of +x is +y; turn towards +y
    assert poly[1] == pytest.approx((9.0, 1.0))  # inside corner
    poly, _ = offset_polyline(GUIDE_90_ONE, -1.0)
    assert poly[1] == pytest.approx((11.0, -1.0))  # outside corner (sqrt2 from vertex)
    # 45 degree: the vertex moves by o * tan(22.5) along the segment
    t = math.tan(math.pi / 8)
    poly, _ = offset_polyline(GUIDE_45_ONE, 1.0)
    assert poly[1] == pytest.approx((10.0 - t, 1.0))
    poly, _ = offset_polyline(GUIDE_45_ONE, -1.0)
    assert poly[1] == pytest.approx((10.0 + t, -1.0))


def test_outer_member_is_longer_on_a_bend():
    for guide in (GUIDE_45_ONE, GUIDE_90_ONE, GUIDE_45_OTHER, GUIDE_90_OTHER):
        offs = lateral_offsets(3, 0.5)
        lengths = [polyline_length(offset_polyline(guide, o)[0]) for o in offs]
        assert lengths[1] == pytest.approx(polyline_length(guide))
        # one side is inner (shorter), the other outer (longer), symmetric
        assert lengths[0] + lengths[2] == pytest.approx(2 * lengths[1])
        assert abs(lengths[0] - lengths[2]) > 0.1
    # turning towards +y (the normal side): positive offsets are inside
    l_in = polyline_length(offset_polyline(GUIDE_90_ONE, 1.0)[0])
    l_out = polyline_length(offset_polyline(GUIDE_90_ONE, -1.0)[0])
    assert l_in == pytest.approx(20.0 - 2.0)
    assert l_out == pytest.approx(20.0 + 2.0)
    # S-bend: the two 45-degree turns cancel out
    s = [polyline_length(offset_polyline(GUIDE_45_RIGHT, o)[0]) for o in (-1, 0, 1)]
    assert s[0] == pytest.approx(s[1]) and s[2] == pytest.approx(s[1])


def test_short_inner_segment_collapses_into_a_single_miter():
    # two 45 turns 0.2 mm apart = a chamfered 90 corner; at offset 1 the inner
    # side of the 0.28 mm diagonal would reverse -> joined directly
    guide = [(0.0, 0.0), (10.0, 0.0), (10.2, 0.2), (10.2, 10.0)]
    poly, _ = offset_polyline(guide, 1.0)
    assert len(poly) == 3
    assert poly[1] == pytest.approx((9.2, 1.0))
    # spacing to the guide is never below the offset
    for k in range(101):
        t = k / 100
        p = (poly[0][0] + t * (poly[1][0] - poly[0][0]), poly[0][1] + t * (poly[1][1] - poly[0][1]))
        assert _poly_dist(p, guide) >= 1.0 - 1e-9
    # outer side keeps all three segments
    outer, _ = offset_polyline(guide, -1.0)
    assert len(outer) == 4
    _assert_parallel_at(guide, outer, -1.0)


def test_end_segment_too_short_raises_with_where():
    guide = [(0.0, 0.0), (0.2, 0.0), (0.2, 10.0)]
    with pytest.raises(OffsetError) as exc:
        offset_polyline(guide, 1.0)
    assert exc.value.where == "start"
    guide = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.2)]
    with pytest.raises(OffsetError) as exc:
        offset_polyline(guide, 1.0)
    assert exc.value.where == "end"


def test_u_turn_cannot_be_offset():
    guide = [(0.0, 0.0), (10.0, 0.0), (10.0, 0.5), (0.0, 0.5)]
    with pytest.raises(OffsetError):
        offset_polyline(guide, 1.0)


def test_layers_are_carried_and_collinear_points_merged():
    guide = [(0, 0), (5, 0), (10, 0), (15, 5)]
    pts, lays = clean_polyline(guide, ["F.Cu", "F.Cu", "F.Cu"])
    assert pts == [(0.0, 0.0), (10.0, 0.0), (15.0, 5.0)]
    assert lays == ["F.Cu", "F.Cu"]
    # a layer change at a collinear point is kept (it is a via site)
    pts, lays = clean_polyline(guide, ["F.Cu", "B.Cu", "B.Cu"])
    assert pts == [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0), (15.0, 5.0)]
    assert lays == ["F.Cu", "B.Cu", "B.Cu"]
    poly, lays = offset_polyline(guide, 0.5, ["F.Cu", "B.Cu", "B.Cu"])
    assert lays == ["F.Cu", "B.Cu", "B.Cu"]
    assert poly[1] == pytest.approx((5.0, 0.5))


def test_zero_offset_is_the_guide():
    poly, _ = offset_polyline(GUIDE_45_ONE, 0.0)
    assert poly == [tuple(map(float, p)) for p in GUIDE_45_ONE]


def test_lateral_offsets_are_centred():
    assert lateral_offsets(1, 0.5) == [0.0]
    assert lateral_offsets(2, 0.5) == [-0.25, 0.25]
    assert lateral_offsets(4, 0.4) == pytest.approx([-0.6, -0.2, 0.2, 0.6])


def test_normal_points_right_of_travel_in_y_down_coords():
    assert normal((1.0, 0.0)) == (-0.0, 1.0)  # east -> south (screen right)
    assert normal((0.0, 1.0)) == (-1.0, 0.0)  # south -> west


def test_order_members_by_start_projection():
    # bundle heading east; starts stacked vertically (y down)
    starts = [(0.0, 3.0), (0.0, 1.0), (0.0, 2.0)]
    ends = [(20.0, 3.0), (20.0, 1.0), (20.0, 2.0)]
    order = order_members(starts, (1.0, 0.0), ends, (1.0, 0.0))
    assert order == [1, 2, 0]  # north (left) to south (right)
    # heading west: left is south
    assert order_members(starts, (-1.0, 0.0)) == [0, 2, 1]
    # diagonal heading south-east
    starts = [(0.0, 0.0), (1.0, -1.0), (-1.0, 1.0)]
    assert order_members(starts, (1.0, 1.0)) == [1, 0, 2]


def test_order_ties_break_on_end_side():
    # start pads in line with the bundle direction: the end side decides
    starts = [(0.0, 0.0), (1.0, 0.0)]
    ends = [(20.0, 1.0), (20.0, 0.0)]
    assert order_members(starts, (1.0, 0.0), ends, (1.0, 0.0)) == [1, 0]


def test_parallel_fraction_partial():
    a = [(0.0, 0.0), (10.0, 0.0)]
    b = [(0.0, 0.5), (5.0, 0.5), (10.0, 3.0)]
    f = parallel_fraction(a, b, 0.5, tol=0.01, step=0.05)
    assert 0.45 < f < 0.55


def test_segments_to_polylines_chains_loose_segments():
    segs = [((5, 0), (10, 0)), ((0, 0), (5, 0)), ((10, 0), (12, 2)), ((50, 50), (51, 50))]
    polys = segments_to_polylines(segs)
    lens = sorted(round(polyline_length(p), 3) for p in polys)
    assert lens == [1.0, round(10 + 2 * S2, 3)]


def test_grid_tail_fix_keeps_runs_octilinear():
    pts = [(0.0, 0.0, "F.Cu"), (5.0, 0.0, "F.Cu"), (10.03, 0.04, "F.Cu")]
    out = grid_tail_fix(pts, 0.1)
    assert out[-2] == (10.0, 0.0, "F.Cu")
    assert out[-1] == (10.03, 0.04, "F.Cu")
    # on-grid anchors are left alone
    assert grid_tail_fix(pts[:2], 0.1) == pts[:2]
