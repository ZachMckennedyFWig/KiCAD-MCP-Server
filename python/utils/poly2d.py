"""Pure-Python 2D geometry in mm for board-editing and standards checks.

Region tests (rule areas, standards regions, plane splits) run on plain
``[(x, y), ...]`` lists instead of ``SHAPE_POLY_SET``: KiCad 10's SWIG layer
hands back bare ``SwigPyObject``s from some polygon accessors and frees
polygons passed to ``ZONE.SetOutline`` (MCP_ISSUES #5r), so the fewer SWIG
polygon objects the checks touch, the fewer ways they can break. The board is
read once into lists (``swig_poly_points``), and everything after that is
ordinary arithmetic.
"""

import math
from typing import Any, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Polygon = List[Point]
BBox = Tuple[float, float, float, float]

NM_PER_MM = 1_000_000.0
EPS = 1e-9


def parse_polygon(value: Any, min_points: int = 3) -> Polygon:
    """``[[x, y], ...]`` or ``[{x, y}, ...]`` (mm) -> list of tuples.

    A bounding box ``{x1, y1, x2, y2}`` or ``[x1, y1, x2, y2]`` is accepted
    as a rectangle. Raises ValueError with a readable message.
    """
    if isinstance(value, dict) and all(k in value for k in ("x1", "y1", "x2", "y2")):
        x1, y1, x2, y2 = (float(value[k]) for k in ("x1", "y1", "x2", "y2"))
        return rect_polygon(x1, y1, x2, y2)
    if (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) for v in value)
    ):
        return rect_polygon(*(float(v) for v in value))
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"polygon must be a list of [x, y] points, got {type(value).__name__}")
    pts: Polygon = []
    for p in value:
        try:
            if isinstance(p, dict):
                pts.append((float(p["x"]), float(p["y"])))
            else:
                pts.append((float(p[0]), float(p[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError(f"cannot read a polygon vertex from {p!r}")
    if len(pts) > 3 and pts[0] == pts[-1]:
        pts.pop()
    if len(pts) < min_points:
        raise ValueError(f"polygon needs at least {min_points} vertices, got {len(pts)}")
    return pts


def parse_point(value: Any) -> Point:
    """``[x, y]`` or ``{x, y}`` (mm)."""
    try:
        if isinstance(value, dict):
            return float(value["x"]), float(value["y"])
        return float(value[0]), float(value[1])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError(f"cannot read a point from {value!r}; use [x, y] or {{x, y}} in mm")


def rect_polygon(x1: float, y1: float, x2: float, y2: float) -> Polygon:
    lx, hx = min(x1, x2), max(x1, x2)
    ly, hy = min(y1, y2), max(y1, y2)
    return [(lx, ly), (hx, ly), (hx, hy), (lx, hy)]


def bbox(points: Iterable[Point]) -> BBox:
    xs, ys = [], []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def bbox_overlap(a: BBox, b: BBox, margin: float = 0.0) -> bool:
    return (
        a[0] - margin <= b[2]
        and b[0] - margin <= a[2]
        and a[1] - margin <= b[3]
        and b[1] - margin <= a[3]
    )


def area(poly: Sequence[Point]) -> float:
    s = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def point_in_polygon(pt: Point, poly: Sequence[Point]) -> bool:
    """Even-odd test; points on an edge count as inside."""
    x, y = pt
    n = len(poly)
    inside = False
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if point_segment_distance(pt, (x1, y1), (x2, y2)) <= 1e-7:
            return True
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def point_strictly_inside(pt: Point, poly: Sequence[Point], tol: float = 1e-4) -> bool:
    """Inside and farther than ``tol`` from every edge."""
    if not point_in_polygon(pt, poly):
        return False
    n = len(poly)
    return all(point_segment_distance(pt, poly[i], poly[(i + 1) % n]) > tol for i in range(n))


def point_in_shape(
    pt: Point, outline: Sequence[Point], holes: Sequence[Sequence[Point]] = ()
) -> bool:
    return point_in_polygon(pt, outline) and not any(point_strictly_inside(pt, h) for h in holes)


def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= EPS:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Proper crossing (the interiors intersect at a single point)."""
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    return ((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS)) and (
        (d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS)
    )


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if segments_cross(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def segment_touches_polygon(a: Point, b: Point, poly: Sequence[Point], margin: float = 0.0) -> bool:
    """True when the segment (inflated by ``margin``) and the polygon intersect."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    n = len(poly)
    for i in range(n):
        if segment_distance(a, b, poly[i], poly[(i + 1) % n]) <= margin:
            return True
    return False


def segment_enters_polygon(a: Point, b: Point, poly: Sequence[Point], tol: float = 1e-4) -> bool:
    """True when part of the segment lies strictly inside the polygon.

    A segment that only runs along (or ends on) the boundary does not count,
    so copper sitting on a region's border is not reported as an intrusion.
    """
    pts = [a, b]
    n = len(poly)
    for i in range(n):
        c, d = poly[i], poly[(i + 1) % n]
        if segments_cross(a, b, c, d):
            # Sample just either side of the crossing.
            ix, iy = _intersection(a, b, c, d)
            pts.append((ix, iy))
    # Check midpoints between consecutive sorted parameter points.
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    if length2 <= EPS:
        return point_strictly_inside(a, poly, tol)
    ts = sorted({max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2)) for p in pts})
    samples = [ts[0], ts[-1]] + [(ts[i] + ts[i + 1]) / 2 for i in range(len(ts) - 1)]
    return any(point_strictly_inside((ax + t * dx, ay + t * dy), poly, tol) for t in samples)


def _intersection(a: Point, b: Point, c: Point, d: Point) -> Point:
    x1, y1 = a
    x2, y2 = b
    x3, y3 = c
    x4, y4 = d
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) <= EPS:
        return a
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


def polygons_intersect(p: Sequence[Point], q: Sequence[Point], margin: float = 0.0) -> bool:
    """True when two simple polygons overlap or come within ``margin``."""
    if not bbox_overlap(bbox(p), bbox(q), margin):
        return False
    if point_in_polygon(p[0], q) or point_in_polygon(q[0], p):
        return True
    n, m = len(p), len(q)
    for i in range(n):
        a, b = p[i], p[(i + 1) % n]
        for j in range(m):
            if segment_distance(a, b, q[j], q[(j + 1) % m]) <= margin:
                return True
    return False


def polygons_overlap_strictly(p: Sequence[Point], q: Sequence[Point], tol: float = 1e-4) -> bool:
    """True when the interiors overlap (shared edges or touching corners don't count)."""
    if not bbox_overlap(bbox(p), bbox(q), -tol):
        return False
    if any(point_strictly_inside(v, q, tol) for v in p) or any(
        point_strictly_inside(v, p, tol) for v in q
    ):
        return True
    n = len(p)
    for i in range(n):
        if segment_enters_polygon(p[i], p[(i + 1) % n], q, tol):
            return True
    # Identical or edge-aligned shapes: no vertex or edge is strictly inside
    # the other, but an interior point of one is.
    for a, b in ((p, q), (q, p)):
        c = (sum(x for x, _ in a) / len(a), sum(y for _, y in a) / len(a))
        if point_strictly_inside(c, a, tol) and point_strictly_inside(c, b, tol):
            return True
    return False


def polygon_inside(inner: Sequence[Point], outer: Sequence[Point], tol: float = 1e-6) -> bool:
    """True when every vertex of ``inner`` is inside ``outer`` and no edges cross."""
    if not all(point_in_polygon(v, outer) for v in inner):
        return False
    n, m = len(inner), len(outer)
    for i in range(n):
        for j in range(m):
            if segments_cross(inner[i], inner[(i + 1) % n], outer[j], outer[(j + 1) % m]):
                return False
    return True


def sample_segment(a: Point, b: Point, step: float) -> List[Point]:
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(math.ceil(length / max(step, 1e-3))))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def rotate(pt: Point, degrees: float, origin: Point = (0.0, 0.0)) -> Point:
    """Rotate in KiCad's screen frame (Y down): positive angles turn counter-clockwise on screen."""
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    x, y = pt[0] - origin[0], pt[1] - origin[1]
    return origin[0] + x * c + y * s, origin[1] - x * s + y * c


# ---------------------------------------------------------------------------
# SWIG readers (the only functions here that touch pcbnew objects)
# ---------------------------------------------------------------------------


def _chain_points(chain: Any) -> Polygon:
    count = chain.PointCount()
    if not isinstance(count, int):
        raise TypeError(f"SHAPE_LINE_CHAIN.PointCount returned {type(count).__name__}")
    out: Polygon = []
    for i in range(count):
        p = chain.CPoint(i)
        out.append((p.x / NM_PER_MM, p.y / NM_PER_MM))
    return out


def swig_poly_points(poly: Any) -> List[Tuple[Polygon, List[Polygon]]]:
    """``SHAPE_POLY_SET`` -> ``[(outline, [holes...]), ...]`` in mm.

    Raises TypeError when the object is not a usable polygon set (the bare
    ``SwigPyObject`` of MCP_ISSUES #5r), so callers can say so clearly.
    """
    for method in ("OutlineCount", "COutline", "HoleCount", "CHole"):
        if not hasattr(poly, method):
            raise TypeError(
                f"expected a SHAPE_POLY_SET, got {type(poly).__name__} (broken SWIG proxy)"
            )
    out: List[Tuple[Polygon, List[Polygon]]] = []
    for i in range(poly.OutlineCount()):
        outline = _chain_points(poly.COutline(i))
        holes = [_chain_points(poly.CHole(i, h)) for h in range(poly.HoleCount(i))]
        if len(outline) >= 3:
            out.append((outline, holes))
    return out


def zone_polygons(zone: Any) -> List[Tuple[Polygon, List[Polygon]]]:
    """A zone's outline(s) in mm, with a corner-list fallback for broken proxies."""
    try:
        return swig_poly_points(zone.Outline())
    except TypeError:
        pass
    pts: Polygon = []
    for i in range(zone.GetNumCorners()):
        p = zone.GetCornerPosition(i)
        pts.append((p.x / NM_PER_MM, p.y / NM_PER_MM))
    return [(pts, [])] if len(pts) >= 3 else []


def optional_polygon(value: Any) -> Optional[Polygon]:
    return None if value is None else parse_polygon(value)
