"""Copper obstacle model shared by ``fanout_to_planes`` and ``route_connection``.

Everything is in board millimetres (y down). The model is a flat list of
:class:`Shape` items -- pads, tracks (arcs as polylines), vias, rule-area
keepouts -- each with the copper layers it occupies and its net. Distances are
computed exactly per shape type (circle, axis-aligned rectangle, capsule,
polygon), both for single points (``Shape.distance``) and vectorised over
numpy grids (``Shape.distance_grid``).

Per-net rules come from the project's ``.kicad_pro`` net classes (the headless
board has only the Default class, #302), falling back to the board's own
Default net class. Custom ``.kicad_dru`` rules are not evaluated; run DRC
after writing copper.
"""

from __future__ import annotations

import fnmatch
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # numpy is optional for the scalar paths used by fanout
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

NM = 1_000_000.0


def mm(v: Any) -> float:
    return float(v) / NM


# ---------------------------------------------------------------------------
# Scalar distance helpers
# ---------------------------------------------------------------------------


def seg_point_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / l2))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def seg_seg_dist(a: Tuple[float, float], b: Tuple[float, float], c, d) -> float:
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(
        seg_point_dist(a[0], a[1], c[0], c[1], d[0], d[1]),
        seg_point_dist(b[0], b[1], c[0], c[1], d[0], d[1]),
        seg_point_dist(c[0], c[1], a[0], a[1], b[0], b[1]),
        seg_point_dist(d[0], d[1], a[0], a[1], b[0], b[1]),
    )


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(a, b, c, d) -> bool:
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    return d1 * d2 < 0 and d3 * d4 < 0


def point_in_polygon(x: float, y: float, pts: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def poly_point_dist(x: float, y: float, pts: Sequence[Tuple[float, float]]) -> float:
    """Distance to a polygon's area (0 inside)."""
    if point_in_polygon(x, y, pts):
        return 0.0
    n = len(pts)
    return min(
        seg_point_dist(x, y, pts[i][0], pts[i][1], pts[(i + 1) % n][0], pts[(i + 1) % n][1])
        for i in range(n)
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


class Shape:
    """One copper (or keepout) item.

    ``kind``: circle (cx, cy, r) | rect (x0, y0, x1, y1) | capsule (ax, ay, bx, by, r)
    | poly (points). ``layers`` is a set of copper layer names; ``net`` the net
    name ("" for no net). ``role`` is pad / track / via / keepout / hole.
    """

    __slots__ = ("kind", "geom", "layers", "net", "role", "ref", "bbox", "extra_clearance", "meta")

    def __init__(
        self,
        kind: str,
        geom: Tuple[Any, ...],
        layers: Iterable[str],
        net: str = "",
        role: str = "pad",
        ref: str = "",
        extra_clearance: float = 0.0,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.kind = kind
        self.geom = geom
        self.layers = set(layers)
        self.net = net
        self.role = role
        self.ref = ref
        self.extra_clearance = extra_clearance
        self.meta = meta or {}
        self.bbox = self._bbox()

    def _bbox(self) -> Tuple[float, float, float, float]:
        g = self.geom
        if self.kind == "circle":
            return (g[0] - g[2], g[1] - g[2], g[0] + g[2], g[1] + g[2])
        if self.kind == "rect":
            return g  # type: ignore[return-value]
        if self.kind == "capsule":
            r = g[4]
            return (
                min(g[0], g[2]) - r,
                min(g[1], g[3]) - r,
                max(g[0], g[2]) + r,
                max(g[1], g[3]) + r,
            )
        xs = [p[0] for p in g[0]]
        ys = [p[1] for p in g[0]]
        return (min(xs), min(ys), max(xs), max(ys))

    def distance(self, x: float, y: float) -> float:
        """Distance from a point to this shape's area (0 inside)."""
        g = self.geom
        if self.kind == "circle":
            return max(0.0, math.hypot(x - g[0], y - g[1]) - g[2])
        if self.kind == "rect":
            dx = max(g[0] - x, 0.0, x - g[2])
            dy = max(g[1] - y, 0.0, y - g[3])
            return math.hypot(dx, dy)
        if self.kind == "capsule":
            return max(0.0, seg_point_dist(x, y, g[0], g[1], g[2], g[3]) - g[4])
        return poly_point_dist(x, y, g[0])

    def segment_distance(self, a: Tuple[float, float], b: Tuple[float, float]) -> float:
        """Distance from the segment a-b (zero width) to this shape's area."""
        g = self.geom
        if self.kind == "circle":
            return max(0.0, seg_point_dist(g[0], g[1], a[0], a[1], b[0], b[1]) - g[2])
        if self.kind == "capsule":
            return max(0.0, seg_seg_dist(a, b, (g[0], g[1]), (g[2], g[3])) - g[4])
        if self.kind == "rect":
            pts = [(g[0], g[1]), (g[2], g[1]), (g[2], g[3]), (g[0], g[3])]
        else:
            pts = list(g[0])
        if point_in_polygon(a[0], a[1], pts) or point_in_polygon(b[0], b[1], pts):
            return 0.0
        n = len(pts)
        return min(seg_seg_dist(a, b, pts[i], pts[(i + 1) % n]) for i in range(n))

    def distance_grid(self, gx: "np.ndarray", gy: "np.ndarray") -> "np.ndarray":
        g = self.geom
        if self.kind == "circle":
            return np.maximum(np.hypot(gx - g[0], gy - g[1]) - g[2], 0.0)
        if self.kind == "rect":
            dx = np.maximum(np.maximum(g[0] - gx, 0.0), gx - g[2])
            dy = np.maximum(np.maximum(g[1] - gy, 0.0), gy - g[3])
            return np.hypot(dx, dy)
        if self.kind == "capsule":
            return np.maximum(_seg_dist_grid(gx, gy, g[0], g[1], g[2], g[3]) - g[4], 0.0)
        pts = g[0]
        n = len(pts)
        d = np.full(gx.shape, np.inf)
        inside = np.zeros(gx.shape, dtype=bool)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            d = np.minimum(d, _seg_dist_grid(gx, gy, x1, y1, x2, y2))
            if y1 != y2:
                cross = ((y1 > gy) != (y2 > gy)) & (gx < x1 + (gy - y1) * (x2 - x1) / (y2 - y1))
                inside ^= cross
        d[inside] = 0.0
        return d


def _seg_dist_grid(gx, gy, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    l2 = vx * vx + vy * vy
    if l2 == 0:
        return np.hypot(gx - ax, gy - ay)
    t = np.clip(((gx - ax) * vx + (gy - ay) * vy) / l2, 0.0, 1.0)
    return np.hypot(gx - (ax + t * vx), gy - (ay + t * vy))


class SpatialIndex:
    """Bucket grid over shapes' bboxes for fast neighbourhood queries."""

    def __init__(self, shapes: Sequence[Shape], cell: float = 2.0) -> None:
        self.cell = cell
        self.buckets: Dict[Tuple[int, int], List[int]] = {}
        self.shapes = list(shapes)
        for idx, s in enumerate(self.shapes):
            self._insert(idx, s)

    def _insert(self, idx: int, s: Shape) -> None:
        x0, y0, x1, y1 = s.bbox
        c = self.cell
        for i in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for j in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                self.buckets.setdefault((i, j), []).append(idx)

    def add(self, s: Shape) -> None:
        self.shapes.append(s)
        self._insert(len(self.shapes) - 1, s)

    def query(self, x0: float, y0: float, x1: float, y1: float) -> List[Shape]:
        c = self.cell
        seen = set()
        out = []
        for i in range(int(math.floor(x0 / c)), int(math.floor(x1 / c)) + 1):
            for j in range(int(math.floor(y0 / c)), int(math.floor(y1 / c)) + 1):
                for idx in self.buckets.get((i, j), ()):
                    if idx not in seen:
                        seen.add(idx)
                        out.append(self.shapes[idx])
        return out


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class Rules:
    """Per-net clearance / width / via sizes (mm) and board-wide constraints."""

    def __init__(self, board: Any, board_path: Optional[str] = None) -> None:
        ds = board.GetDesignSettings()
        default_cls = None
        try:
            default_cls = ds.m_NetSettings.GetDefaultNetclass()
        except Exception:
            pass

        def _nc(getter: str, fallback: float) -> float:
            try:
                return mm(getattr(default_cls, getter)())
            except Exception:
                return fallback

        self.default = {
            "clearance": _nc("GetClearance", 0.2),
            "track_width": _nc("GetTrackWidth", 0.25),
            "via_diameter": _nc("GetViaDiameter", 0.6),
            "via_drill": _nc("GetViaDrill", 0.3),
        }
        self.edge_clearance = _safe_mm(lambda: ds.m_CopperEdgeClearance, 0.3)
        self.hole_clearance = _safe_mm(lambda: ds.m_HoleClearance, 0.25)
        self.hole_to_hole = _safe_mm(lambda: ds.m_HoleToHoleMin, 0.25)
        self.min_clearance = _safe_mm(lambda: ds.m_MinClearance, 0.0)
        self.classes: Dict[str, Dict[str, float]] = {}
        self.patterns: List[Tuple[str, str]] = []
        self.assignments: Dict[str, List[str]] = {}
        self.source = "board default net class"
        path = board_path
        if not path:
            try:
                path = board.GetFileName()
            except Exception:
                path = None
        if path:
            pro = os.path.splitext(path)[0] + ".kicad_pro"
            try:
                from utils.project_netclasses import load_project_net_classes

                settings = load_project_net_classes(pro)
            except Exception:
                settings = None
            if settings:
                self.source = os.path.basename(pro)
                for cls in settings.get("classes", []):
                    entry = dict(self.default)
                    for k in entry:
                        if isinstance(cls.get(k), (int, float)) and cls.get(k) > 0:
                            entry[k] = float(cls[k])
                    self.classes[cls.get("name", "")] = entry
                if "Default" in self.classes:
                    self.default = self.classes["Default"]
                self.patterns = list(settings.get("patterns", []))
                self.assignments = dict(settings.get("assignments", {}))
        self._cache: Dict[str, Dict[str, float]] = {}

    def for_net(self, net: str) -> Dict[str, float]:
        if net in self._cache:
            return self._cache[net]
        rules = self.default
        names = self.assignments.get(net) or []
        if not names:
            for pattern, cls in self.patterns:
                if fnmatch.fnmatchcase(net, pattern):
                    names = [cls]
                    break
        for name in names:
            if name in self.classes:
                rules = self.classes[name]
                break
        self._cache[net] = rules
        return rules

    def clearance(self, net_a: str, net_b: str) -> float:
        return max(self.for_net(net_a)["clearance"], self.for_net(net_b)["clearance"])


def _safe_mm(fn, fallback: float) -> float:
    try:
        return mm(fn())
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Board extraction
# ---------------------------------------------------------------------------


def copper_layer_names(board: Any) -> List[str]:
    return [board.GetLayerName(lid) for lid in board.GetEnabledLayers().CuStack()]


def _poly_points(chain: Any) -> List[Tuple[float, float]]:
    pts = []
    for i in range(chain.PointCount()):
        p = chain.CPoint(i)
        pts.append((mm(p.x), mm(p.y)))
    return pts


def pad_shapes(board: Any, pad: Any, pcbnew: Any, cu_names: Dict[int, str]) -> List[Shape]:
    """One Shape per copper layer group of a pad (exact for 0/90-degree rect/oval/circle)."""
    out: List[Shape] = []
    net = pad.GetNetname() or ""
    ref = f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"
    attrib = pad.GetAttribute()
    npth = attrib == getattr(pcbnew, "PAD_ATTRIB_NPTH", -1)
    layers = [name for lid, name in cu_names.items() if pad.IsOnLayer(lid)]
    if npth:
        hole = pad.GetDrillSize()
        r = max(mm(hole.x), mm(hole.y), mm(pad.GetSize(pcbnew.F_Cu).x)) / 2
        pos = pad.GetPosition()
        return [Shape("circle", (mm(pos.x), mm(pos.y), r), cu_names.values(), "", "hole", ref, 0.0)]
    if not layers:
        return out
    by_layer: Dict[Tuple[Any, ...], List[str]] = {}
    for lid, name in cu_names.items():
        if name not in layers:
            continue
        shape = _pad_layer_shape(pad, lid, pcbnew)
        by_layer.setdefault(shape, []).append(name)
    for (kind, geom), names in by_layer.items():
        meta = {"pth": attrib == getattr(pcbnew, "PAD_ATTRIB_PTH", -2)}
        if meta["pth"]:
            d = pad.GetDrillSize()
            meta["drill"] = max(mm(d.x), mm(d.y))
        out.append(Shape(kind, geom, names, net, "pad", ref, meta=meta))
    return out


def _pad_layer_shape(pad: Any, lid: int, pcbnew: Any) -> Tuple[str, Tuple[Any, ...]]:
    try:
        shape = pad.GetShape(lid)
    except TypeError:
        shape = pad.GetShape()
    try:
        size = pad.GetSize(lid)
    except TypeError:
        size = pad.GetSize()
    pos = pad.GetPosition()
    cx, cy = mm(pos.x), mm(pos.y)
    try:
        angle = pad.GetOrientationDegrees() % 180.0
    except Exception:
        angle = 0.0
    right = abs(angle) < 1e-6 or abs(angle - 90.0) < 1e-6
    sx, sy = mm(size.x), mm(size.y)
    if abs(angle - 90.0) < 1e-6:
        sx, sy = sy, sx
    if shape == getattr(pcbnew, "PAD_SHAPE_CIRCLE", -1):
        return ("circle", (cx, cy, sx / 2))
    if right and shape == getattr(pcbnew, "PAD_SHAPE_OVAL", -1):
        if sx >= sy:
            h = (sx - sy) / 2
            return ("capsule", (cx - h, cy, cx + h, cy, sy / 2))
        h = (sy - sx) / 2
        return ("capsule", (cx, cy - h, cx, cy + h, sx / 2))
    rect_shapes = {
        getattr(pcbnew, n, -99)
        for n in ("PAD_SHAPE_RECTANGLE", "PAD_SHAPE_RECT", "PAD_SHAPE_ROUNDRECT")
    } | {getattr(pcbnew, "PAD_SHAPE_CHAMFERED_RECT", -98)}
    if right and shape in rect_shapes:
        return ("rect", (cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2))
    # rotated or custom/trapezoid: exact polygon from KiCad
    try:
        err = getattr(pcbnew, "ERROR_INSIDE", 0)
        poly = pad.GetEffectivePolygon(lid, err)
        pts = tuple(_poly_points(poly.COutline(0)))
        if len(pts) >= 3:
            return ("poly", (pts,))
    except Exception:
        pass
    bb = pad.GetBoundingBox()
    return ("rect", (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())))


def track_shapes(board: Any, t: Any, pcbnew: Any, cu_names: Dict[int, str]) -> List[Shape]:
    net = t.GetNetname() or ""
    cls = t.GetClass()
    if cls == "PCB_VIA":
        pos = t.GetPosition()
        layers = [name for lid, name in cu_names.items() if t.IsOnLayer(lid)] or list(
            cu_names.values()
        )
        try:
            w = mm(t.GetWidth(pcbnew.F_Cu))
        except TypeError:
            w = mm(t.GetWidth())
        return [
            Shape(
                "circle",
                (mm(pos.x), mm(pos.y), w / 2),
                layers,
                net,
                "via",
                meta={"drill": mm(t.GetDrillValue())},
            )
        ]
    layer = cu_names.get(t.GetLayer())
    if layer is None:
        return []
    r = mm(t.GetWidth()) / 2
    if cls == "PCB_ARC":
        try:
            from commands.routing import _arc_polyline_points

            pts = [(mm(x), mm(y)) for x, y in _arc_polyline_points(t)]
        except Exception:
            pts = [(mm(t.GetStart().x), mm(t.GetStart().y)), (mm(t.GetEnd().x), mm(t.GetEnd().y))]
    else:
        pts = [(mm(t.GetStart().x), mm(t.GetStart().y)), (mm(t.GetEnd().x), mm(t.GetEnd().y))]
    return [
        Shape("capsule", (a[0], a[1], b[0], b[1], r), [layer], net, "track")
        for a, b in zip(pts, pts[1:])
    ]


def keepout_shapes(board: Any, pcbnew: Any, cu_names: Dict[int, str]) -> List[Shape]:
    """Rule areas that forbid tracks and/or vias (pour-only ones are ignored)."""
    out = []
    for z in board.Zones():
        try:
            if not z.GetIsRuleArea():
                continue
            no_tracks, no_vias = bool(z.GetDoNotAllowTracks()), bool(z.GetDoNotAllowVias())
            if not (no_tracks or no_vias):
                continue
            layers = [name for lid, name in cu_names.items() if z.GetLayerSet().Contains(lid)]
            outline = z.Outline()
            for i in range(outline.OutlineCount()):
                pts = tuple(_poly_points(outline.COutline(i)))
                if len(pts) >= 3:
                    out.append(
                        Shape(
                            "poly",
                            (pts,),
                            layers,
                            "",
                            "keepout",
                            meta={"noTracks": no_tracks, "noVias": no_vias},
                        )
                    )
        except Exception:
            continue
    return out


def board_outlines(board: Any, pcbnew: Any) -> List[List[Tuple[float, float]]]:
    """Board outline polygons (outer outlines first, then holes) in mm."""
    ps = pcbnew.SHAPE_POLY_SET()
    try:
        board.GetBoardPolygonOutlines(ps, True)
    except TypeError:
        board.GetBoardPolygonOutlines(ps)
    polys = []
    for i in range(ps.OutlineCount()):
        polys.append(_poly_points(ps.COutline(i)))
        for j in range(ps.HoleCount(i)):
            polys.append(_poly_points(ps.CHole(i, j)))
    return polys


def outline_edges(polys: Sequence[Sequence[Tuple[float, float]]]) -> List[Shape]:
    """Board edge segments as zero-width capsules (for edge clearance)."""
    out = []
    for pts in polys:
        n = len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            out.append(Shape("capsule", (a[0], a[1], b[0], b[1], 0.0), [], "", "edge"))
    return out


def inside_board(x: float, y: float, polys: Sequence[Sequence[Tuple[float, float]]]) -> bool:
    """Inside an outer outline and outside its holes (even-odd over all rings)."""
    count = sum(1 for pts in polys if point_in_polygon(x, y, pts))
    return count % 2 == 1


def collect_copper(board: Any, pcbnew: Any) -> Tuple[List[Shape], Dict[int, str]]:
    cu_names = {lid: board.GetLayerName(lid) for lid in board.GetEnabledLayers().CuStack()}
    shapes: List[Shape] = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            shapes.extend(pad_shapes(board, pad, pcbnew, cu_names))
    for t in board.GetTracks():
        shapes.extend(track_shapes(board, t, pcbnew, cu_names))
    return shapes, cu_names


def parse_point(spec: Any, board: Any) -> Dict[str, Any]:
    """``{ref, pad}`` or ``{x, y, layer?}`` -> {"kind": "pad", "pad": PAD} | {"kind": "point", ...}."""
    if not isinstance(spec, dict):
        raise ValueError(f"endpoint must be an object, got {spec!r}")
    ref = spec.get("ref") or spec.get("reference") or spec.get("componentRef")
    if ref is not None:
        pad_name = str(spec.get("pad", ""))
        fp = board.FindFootprintByReference(str(ref))
        if fp is None:
            raise ValueError(f"footprint {ref} not found")
        for pad in fp.Pads():
            if pad.GetNumber() == pad_name:
                return {"kind": "pad", "pad": pad, "label": f"{ref}.{pad_name}"}
        raise ValueError(f"pad {pad_name} not found on {ref}")
    if "x" in spec and "y" in spec:
        unit = spec.get("unit", "mm")
        scale = {"mm": 1.0, "mil": 0.0254, "inch": 25.4}.get(unit)
        if scale is None:
            raise ValueError(f"unknown unit {unit}")
        return {
            "kind": "point",
            "x": float(spec["x"]) * scale,
            "y": float(spec["y"]) * scale,
            "layer": spec.get("layer"),
            "label": f"({spec['x']}, {spec['y']})",
        }
    raise ValueError("endpoint needs {ref, pad} or {x, y[, layer]}")
