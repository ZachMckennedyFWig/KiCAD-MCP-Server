"""Unit tests for the board query helpers that need no KiCad.

The pcbnew-backed behaviour is covered by test_board_query_real.py.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.board_query import (  # noqa: E402
    BandedPolygon,
    BoardQueryCommands,
    NetMatcher,
    arc_points,
    parse_regions,
    point_in_poly,
    poly_hits_rect,
    seg_hits_rect,
    seg_seg,
)
from commands.board_render import wants_native_render  # noqa: E402

pytestmark = pytest.mark.unit

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]


def test_every_tool_reports_a_missing_board() -> None:
    q = BoardQueryCommands(None)
    for result in (
        q.query_region({"x0": 0, "y0": 0, "x1": 1, "y1": 1}),
        q.net_report({}),
        q.why_unconnected({"net": "GND"}),
        q.connected_items({"x": 1, "y": 1}),
    ):
        assert result["success"] is False
        assert "No board" in result["message"]


def test_parameter_validation_happens_before_board_access() -> None:
    q = BoardQueryCommands(None)
    assert "zero area" in q.query_region({"x0": 1, "y0": 1, "x1": 1, "y1": 5})["message"]
    assert (
        "detail"
        in q.query_region({"x0": 0, "y0": 0, "x1": 1, "y1": 1, "detail": "everything"})[
            "errorDetails"
        ]
    )
    assert "limit" in q.query_region({"x0": 0, "y0": 0, "x1": 1, "y1": 1, "limit": -1})["message"]
    assert q.net_report({"nets": "GND"})["message"] == "nets must be a list of net names"
    assert "net is required" in q.why_unconnected({})["message"]
    assert "start from" in q.connected_items({})["message"]


def test_board_source_is_read_on_every_call() -> None:
    holder = {"board": None}
    q = BoardQueryCommands(lambda: holder["board"])
    assert q.board is None
    holder["board"] = "loaded"
    assert q.board == "loaded"


def test_net_matcher_accepts_hierarchical_tails() -> None:
    m = NetMatcher(["SDA", "/Power/+5V"])
    assert m.match("SDA") and m.match("/MCU/SDA")
    assert m.match("/Power/+5V") and not m.match("/Other/+5V")
    assert not m.match(None)
    assert NetMatcher(None).match("anything")


def test_polygon_helpers() -> None:
    assert point_in_poly(5, 5, SQUARE) and not point_in_poly(11, 5, SQUARE)
    banded = BandedPolygon(SQUARE)
    for x, y in [(5, 5), (0.1, 9.9), (11, 5), (5, -1), (9.99, 0.01)]:
        assert banded.contains(x, y) == point_in_poly(x, y, SQUARE)
    assert seg_hits_rect(-5, 5, 15, 5, (0, 0, 10, 10))
    assert not seg_hits_rect(-5, 12, 15, 12, (0, 0, 10, 10))
    assert seg_hits_rect(-5, 10.4, 15, 10.4, (0, 0, 10, 10), pad=0.5)
    assert poly_hits_rect(SQUARE, (4, 4, 6, 6))  # window inside the polygon
    assert not poly_hits_rect(SQUARE, (20, 20, 30, 30))


def test_segment_distance_and_closest_points() -> None:
    d, p, q = seg_seg((0, 0, 10, 0), (12, 0, 20, 0))
    assert d == pytest.approx(2.0) and p == (10, 0) and q == (12, 0)
    d, p, q = seg_seg((0, 0, 10, 10), (0, 10, 10, 0))
    assert d == 0.0 and p == pytest.approx((5, 5))
    d, _p, q = seg_seg((5, 3, 5, 3), (0, 0, 10, 0))
    assert d == pytest.approx(3.0) and q == pytest.approx((5, 0))


def test_arc_points_follow_the_mid_point() -> None:
    upper = arc_points((-1, 0), (0, -1), (1, 0), 8)
    lower = arc_points((-1, 0), (0, 1), (1, 0), 8)
    assert upper[0] == pytest.approx((-1, 0)) and upper[-1] == pytest.approx((1, 0))
    assert all(math.hypot(x, y) == pytest.approx(1.0) for x, y in upper + lower)
    assert min(y for _, y in upper) == pytest.approx(-1.0)
    assert max(y for _, y in lower) == pytest.approx(1.0)


def test_parse_regions() -> None:
    regions, error = parse_regions({"A": {"x0": 0, "y0": 0, "x1": 1, "y1": 2}, "B": SQUARE})
    assert error is None
    assert regions["A"] == [(0, 0), (1, 0), (1, 2), (0, 2)]
    assert regions["B"] == [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    _, error = parse_regions({"bad": [[0, 0], [1, 1]]})
    assert error and "bad" in error["message"]
    _, error = parse_regions([1, 2])
    assert error is not None


def test_native_render_is_opt_in() -> None:
    assert not wants_native_render({"pcbPath": "x.kicad_pcb", "layers": ["F.Cu"]})
    assert not wants_native_render({"highlightNets": [], "showRatsnest": False})
    assert wants_native_render({"region": {"x0": 0, "y0": 0, "x1": 1, "y1": 1}})
    assert wants_native_render({"highlightNets": ["GND"]})
    assert wants_native_render({"scale": 20})
