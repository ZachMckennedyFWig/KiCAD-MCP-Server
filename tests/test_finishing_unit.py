"""Unit tests for the finishing / transplant helpers that need no real pcbnew.

The geometry these tools stand on (polyline walking, the anchor transform fit,
region tests) is pure Python, so it is pinned here where CI always runs it.
The pcbnew-backed behaviour is covered by tests/test_finishing_real.py.
"""

import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.finishing_geom import (  # noqa: E402
    ParamError,
    ShapeIndex,
    disk_hits,
    parse_point,
    parse_region,
    point_seg_dist,
    seg_intersection_t,
    walk_polyline,
    walk_ring_evenly,
)
from commands.transplant import Transform, fit_transform, point_in_poly  # noqa: E402

pytestmark = pytest.mark.unit

MM = 1_000_000


def test_walk_polyline_keeps_corners_and_pitch():
    pts = [(0, 0), (10 * MM, 0), (10 * MM, 4 * MM)]
    out = walk_polyline(pts, 1.5 * MM, closed=False)
    xy = [(round(x / MM, 6), round(y / MM, 6)) for x, y, _, _ in out]
    assert xy[0] == (0, 0) and (10, 0) in xy and xy[-1] == (10, 4)
    assert len(xy) == len(set(xy))  # the joint is emitted once
    gaps = [math.dist(a, b) for a, b in zip(xy, xy[1:])]
    assert max(gaps) <= 1.5 + 1e-9
    # normals are unit length and perpendicular to the first segment
    nx, ny = out[0][2], out[0][3]
    assert math.hypot(nx, ny) == pytest.approx(1.0)
    assert nx == pytest.approx(0.0)


def test_walk_polyline_closed_does_not_repeat_start():
    square = [(0, 0), (4 * MM, 0), (4 * MM, 4 * MM), (0, 4 * MM)]
    out = walk_polyline(square, 2 * MM, closed=True)
    xy = [(round(x), round(y)) for x, y, _, _ in out]
    assert len(xy) == 8 and len(set(xy)) == 8


def test_walk_ring_evenly_spacing():
    ring = [(0, 0), (10 * MM, 0), (10 * MM, 10 * MM), (0, 10 * MM)]
    out = walk_ring_evenly(ring, 2.5 * MM)
    assert len(out) == 16
    for a, b in zip(out, out[1:]):
        assert math.dist(a, b) == pytest.approx(2.5 * MM, rel=1e-6)


@pytest.mark.parametrize("deg", [0, 90, 180, 270, 33.5])
@pytest.mark.parametrize("mirror", [False, True])
def test_fit_transform_recovers_rigid_motion(deg, mirror):
    truth = Transform(math.radians(deg), mirror, (5 * MM, 7 * MM), (40 * MM, -3 * MM))
    src = [(5 * MM, 7 * MM), (9 * MM, 7 * MM), (5 * MM, 12 * MM), (11 * MM, 2 * MM)]
    dst = [truth.apply(*p) for p in src]
    fitted, resid = fit_transform(src, dst, mirror)
    assert resid < 1.0  # nm
    for p, q in zip(src, dst):
        x, y = fitted.apply(*p)
        assert math.hypot(x - q[0], y - q[1]) < 1.0
        assert fitted.inverse(x, y) == pytest.approx(p, abs=1e-3)


def test_fit_transform_flags_a_wrong_mirror():
    truth = Transform(0.0, True, (0, 0), (0, 0))
    src = [(0, 0), (4 * MM, 0), (0, 3 * MM)]
    dst = [truth.apply(*p) for p in src]
    _, resid = fit_transform(src, dst, mirror=False)
    assert resid > 0.5 * MM


def test_transform_kicad_degrees_sign():
    # +90 deg in raw y-down coordinates turns +x into +y (clockwise on screen),
    # which is -90 in KiCad's counter-clockwise convention.
    t = Transform(math.radians(90), False, (0, 0), (0, 0))
    assert t.apply(MM, 0) == pytest.approx((0, MM), abs=1e-6)
    assert t.kicad_degrees == pytest.approx(-90)


def test_point_in_poly_and_segment_helpers():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert point_in_poly(5, 5, sq) and not point_in_poly(11, 5, sq)
    assert point_seg_dist(5, 3, 0, 0, 10, 0) == pytest.approx(3)
    assert point_seg_dist(-3, 4, 0, 0, 10, 0) == pytest.approx(5)
    assert seg_intersection_t((0, 0), (10, 0), (4, -1), (4, 1)) == pytest.approx(0.4)
    assert seg_intersection_t((0, 0), (10, 0), (0, 1), (10, 1)) is None


def test_disk_hits_numeric_obstacles():
    circle = ("c", 0, 0, 300_000)
    assert disk_hits(circle, 700_000, 0, 300_000, 150_000)  # 0.7 < 0.3 + 0.3 + 0.15
    assert not disk_hits(circle, 700_000, 0, 300_000, 50_000)  # 0.7 > 0.65
    seg = ("s", 0, 0, 10 * MM, 0, 125_000)
    assert disk_hits(seg, 5 * MM, 500_000, 300_000, 100_000)
    assert not disk_hits(seg, 5 * MM, 600_000, 300_000, 100_000)


def test_shape_index_query_dedupes():
    idx = ShapeIndex(cell_nm=MM)
    idx.add((0, 0, 5 * MM, 5 * MM), "big")
    idx.add((8 * MM, 8 * MM, 9 * MM, 9 * MM), "small")
    assert list(idx.query((1 * MM, 1 * MM, 4 * MM, 4 * MM))) == ["big"]
    assert sorted(idx.query((0, 0, 10 * MM, 10 * MM))) == ["big", "small"]


def test_param_parsing_errors():
    assert parse_point([1, 2], "x") == (1.0, 2.0)
    assert parse_point({"x": 1, "y": 2}, "x") == (1.0, 2.0)
    with pytest.raises(ParamError):
        parse_point([1], "fence")
    assert parse_region({"x1": 5, "y1": 1, "x2": 2, "y2": 4}) == (2 * MM, 1 * MM, 5 * MM, 4 * MM)
    with pytest.raises(ParamError) as e:
        parse_region({"x1": 1})
    assert e.value.response()["success"] is False


def test_tools_are_routed_and_refuse_without_board():
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface()
    names = [
        "stitch_ground",
        "tidy_silkscreen",
        "add_pin_labels",
        "cleanup_dangling",
        "transplant_layout",
    ]
    for name in names:
        assert name in iface.command_routes
    iface.board = None
    for name in names:
        res = iface.command_routes[name]({})
        assert res["success"] is False and "No board" in res["message"]
