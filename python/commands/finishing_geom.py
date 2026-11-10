"""Geometry helpers shared by the finishing and layout-transplant tools.

Everything here works in KiCad internal units (nanometres) and leans on
KiCad's own shape engine (``GetEffectiveShape()`` + ``SHAPE.Collide``) rather
than bounding boxes. That matters for silkscreen: a connector's outline is a
rectangle of strokes, and a bounding-box test treats its empty interior as
solid, which hid every candidate position in the reference script
(MCP_ISSUES F2).

KiCad 10 SWIG notes that shaped this module (MCP_ISSUES #5r):

* ``BOARD.GetTracks()`` returns down-cast items, so ``isinstance(x,
  pcbnew.PCB_VIA)`` is reliable on a real board.
* ``GetOwnClearance()`` returns 0 on a board loaded outside the editor (the
  DRC engine is not initialised), so clearances come from the net classes
  via ``NET_SETTINGS.GetEffectiveNetClass(name)`` plus the board minimum.
* ``ZONE.GetLayerName()`` can lie on fresh zones; layer sets are read with
  ``GetLayerSet().CuStack()`` instead.
"""

import logging
import math
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")

NM_PER_MM = 1_000_000

BBox = Tuple[int, int, int, int]


class ParamError(Exception):
    """A caller mistake, reported as ``{"success": False, ...}`` without a traceback."""

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def response(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"success": False, "message": self.message}
        if self.details:
            out["errorDetails"] = self.details
        return out


def no_board() -> Dict[str, Any]:
    return {
        "success": False,
        "message": "No board is loaded",
        "errorDetails": "Load or create a board first (open_project / open_board)",
    }


def to_nm(value_mm: Any) -> int:
    return int(round(float(value_mm) * NM_PER_MM))


def to_mm(value_nm: Any, digits: int = 4) -> float:
    return round(float(value_nm) / NM_PER_MM, digits)


def vec(x_nm: float, y_nm: float) -> Any:
    return pcbnew.VECTOR2I(int(round(x_nm)), int(round(y_nm)))


def num(params: Dict[str, Any], key: str, default: Optional[float]) -> Optional[float]:
    """Read a numeric parameter, rejecting booleans and non-numbers with a clear error."""
    value = params.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool):
        raise ParamError(f"'{key}' must be a number", f"got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ParamError(f"'{key}' must be a number", f"got {value!r}")


def box_of(bbox: Any) -> BBox:
    """BOX2I -> (x0, y0, x1, y1) in nm."""
    return (
        int(bbox.GetLeft()),
        int(bbox.GetTop()),
        int(bbox.GetRight()),
        int(bbox.GetBottom()),
    )


def grow(box: BBox, amount: int) -> BBox:
    return (box[0] - amount, box[1] - amount, box[2] + amount, box[3] + amount)


def boxes_overlap(a: BBox, b: BBox) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def parse_point(value: Any, what: str) -> Tuple[float, float]:
    """Accept ``[x, y]`` or ``{"x": .., "y": ..}`` (mm)."""
    if isinstance(value, dict) and "x" in value and "y" in value:
        return float(value["x"]), float(value["y"])
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    raise ParamError(
        f"Invalid point in {what}", f"expected [x, y] or {{x, y}} in mm, got {value!r}"
    )


def parse_region(value: Any, what: str = "region") -> Optional[BBox]:
    """``{"x1","y1","x2","y2"}`` (mm, any corner order) -> nm box, or None."""
    if value is None:
        return None
    if not isinstance(value, dict) or not all(k in value for k in ("x1", "y1", "x2", "y2")):
        raise ParamError(f"Invalid {what}", "expected {x1, y1, x2, y2} in mm")
    x1, y1, x2, y2 = (to_nm(value[k]) for k in ("x1", "y1", "x2", "y2"))
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def in_box(x: int, y: int, box: BBox) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


class ShapeIndex:
    """Uniform-grid bucket index over bounding boxes, for proximity queries.

    Payloads are opaque. ``query`` yields each payload whose box overlaps the
    query box, once.
    """

    def __init__(self, cell_nm: int = 2 * NM_PER_MM) -> None:
        self.cell = max(int(cell_nm), 1)
        self.buckets: Dict[Tuple[int, int], List[int]] = {}
        self.items: List[Tuple[BBox, Any]] = []

    def _keys(self, box: BBox) -> Iterator[Tuple[int, int]]:
        c = self.cell
        for i in range(int(box[0] // c), int(box[2] // c) + 1):
            for j in range(int(box[1] // c), int(box[3] // c) + 1):
                yield (i, j)

    def add(self, box: BBox, payload: Any) -> None:
        idx = len(self.items)
        self.items.append((box, payload))
        for key in self._keys(box):
            self.buckets.setdefault(key, []).append(idx)

    def query(self, box: BBox) -> Iterator[Any]:
        seen = set()
        for key in self._keys(box):
            for idx in self.buckets.get(key, ()):
                if idx in seen:
                    continue
                seen.add(idx)
                item_box, payload = self.items[idx]
                if boxes_overlap(item_box, box):
                    yield payload

    def __len__(self) -> int:
        return len(self.items)


# --------------------------------------------------------------------------- #
# Board-level helpers
# --------------------------------------------------------------------------- #


def layer_name(board: Any, layer_id: int) -> str:
    """Canonical layer name (``In1.Cu``), not the user alias (``In1.Cu.GND``)."""
    getter = getattr(pcbnew.BOARD, "GetStandardLayerName", None)
    if getter is not None:
        try:
            return str(getter(layer_id))
        except Exception:
            pass
    return str(board.GetLayerName(layer_id))


def layer_id(board: Any, name: str) -> int:
    lid = board.GetLayerID(str(name))
    if lid is None or lid < 0:
        raise ParamError(f"Unknown layer '{name}'", "use names like F.Cu, In1.Cu, B.Cu")
    return int(lid)


def copper_layers(board: Any) -> List[int]:
    return [int(x) for x in board.GetEnabledLayers().CuStack()]


def zone_copper_layers(zone: Any) -> List[int]:
    return [int(x) for x in zone.GetLayerSet().CuStack()]


def board_outline(board: Any) -> Optional[Any]:
    """Board outline as a SHAPE_POLY_SET, or None if Edge.Cuts does not close."""
    poly = pcbnew.SHAPE_POLY_SET()
    try:
        ok = board.GetBoardPolygonOutlines(poly, True)
    except TypeError:  # KiCad 8/9 signature
        ok = board.GetBoardPolygonOutlines(poly)
    if not ok or poly.OutlineCount() == 0:
        return None
    return poly


def inset_poly(poly: Any, amount_nm: int) -> Any:
    """Copy of ``poly`` shrunk by ``amount_nm`` (grown when negative)."""
    out = pcbnew.SHAPE_POLY_SET(poly)
    if amount_nm:
        out.Inflate(
            -int(amount_nm),
            pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
            max(1000, abs(int(amount_nm)) // 50),
        )
    try:
        out.BuildBBoxCaches()
    except Exception:
        pass
    return out


def poly_contains(poly: Any, x: float, y: float) -> bool:
    try:
        return bool(poly.Contains(vec(x, y), -1, 0, True))
    except Exception:
        return bool(poly.Contains(vec(x, y)))


def chain_points(chain: Any) -> List[Tuple[int, int]]:
    pts = []
    for i in range(chain.PointCount()):
        p = chain.CPoint(i)
        pts.append((int(p.x), int(p.y)))
    return pts


def poly_rings(poly: Any) -> List[List[Tuple[int, int]]]:
    """Every outline and hole of a SHAPE_POLY_SET as closed point lists."""
    rings = []
    for i in range(poly.OutlineCount()):
        rings.append(chain_points(poly.Outline(i)))
        for h in range(poly.HoleCount(i)):
            rings.append(chain_points(poly.Hole(i, h)))
    return rings


def walk_polyline(
    pts: Sequence[Tuple[float, float]], pitch: float, closed: bool
) -> List[Tuple[float, float, float, float]]:
    """Points every ``pitch`` along a polyline, with the local unit normal.

    Returns ``(x, y, nx, ny)``. Each segment is divided evenly so corners are
    always included; duplicate joints are emitted once.
    """
    if len(pts) < 2 or pitch <= 0:
        return []
    seq = list(pts) + ([pts[0]] if closed and pts[0] != pts[-1] else [])
    out: List[Tuple[float, float, float, float]] = []
    for idx, ((ax, ay), (bx, by)) in enumerate(zip(seq, seq[1:])):
        length = math.hypot(bx - ax, by - ay)
        if length <= 0:
            continue
        nx, ny = -(by - ay) / length, (bx - ax) / length
        n = max(1, int(math.ceil(length / pitch - 1e-9)))
        start = 0 if idx == 0 else 1
        for i in range(start, n + 1):
            if closed and idx == len(seq) - 2 and i == n:
                continue  # the closing point repeats the first one
            out.append((ax + (bx - ax) * i / n, ay + (by - ay) * i / n, nx, ny))
    return out


def walk_ring_evenly(ring: Sequence[Tuple[int, int]], pitch: float) -> List[Tuple[float, float]]:
    """Points at a constant arc-length pitch around a closed ring (for curved outlines)."""
    if len(ring) < 2 or pitch <= 0:
        return []
    seq = list(ring) + [ring[0]]
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(seq, seq[1:]))
    if total <= 0:
        return []
    n = max(1, int(round(total / pitch)))
    step = total / n
    out = []
    target, run = 0.0, 0.0
    for a, b in zip(seq, seq[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        while seg > 0 and target <= run + seg + 1e-6 and len(out) < n:
            t = (target - run) / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            target += step
        run += seg
    return out


def netclass_clearance_fn(board: Any):
    """Return ``f(net_name) -> clearance_nm`` reading the effective net class."""
    ds = board.GetDesignSettings()
    floor = int(getattr(ds, "m_MinClearance", 0) or 0)
    cache: Dict[str, int] = {}
    try:
        settings = ds.m_NetSettings
    except Exception:
        settings = None

    def clearance(net_name: str) -> int:
        if net_name in cache:
            return cache[net_name]
        value = 0
        if settings is not None:
            try:
                value = int(settings.GetEffectiveNetClass(net_name).GetClearance())
            except Exception:
                try:
                    value = int(settings.GetDefaultNetclass().GetClearance())
                except Exception:
                    value = 0
        cache[net_name] = max(value, floor)
        return cache[net_name]

    return clearance


def resolve_net(board: Any, name: Optional[str], fallbacks: Iterable[str] = ()) -> Tuple[Any, str]:
    candidates = [name] if name else list(fallbacks)
    for cand in candidates:
        if not cand:
            continue
        net = board.FindNet(str(cand))
        if net is not None and hasattr(net, "GetNetCode") and net.GetNetCode() > 0:
            return net, str(cand)
    if name:
        raise ParamError(f"Net '{name}' not found on the board", "pass an exact board net name")
    raise ParamError(
        "No ground net detected", f"pass 'net' explicitly (tried {', '.join(candidates)})"
    )


def is_via(item: Any) -> bool:
    return isinstance(item, pcbnew.PCB_VIA)


def is_arc(item: Any) -> bool:
    return isinstance(item, pcbnew.PCB_ARC)


def is_track(item: Any) -> bool:
    """A straight segment or arc, not a via."""
    return isinstance(item, pcbnew.PCB_TRACK) and not isinstance(item, pcbnew.PCB_VIA)


def via_width(via: Any) -> int:
    getter = getattr(via, "GetFrontWidth", None)
    if getter is not None:
        return int(getter())
    return int(via.GetWidth())


def item_shape(item: Any, layer: int) -> Any:
    return item.GetEffectiveShape(layer)


def shape_box(shape: Any) -> BBox:
    return box_of(shape.BBox())


def pad_layers(pad: Any, coppers: Sequence[int]) -> List[int]:
    return [lid for lid in coppers if pad.IsOnLayer(lid)]


def footprint_ref(item: Any) -> str:
    try:
        fp = item.GetParentFootprint()
        return fp.GetReference() if fp is not None else ""
    except Exception:
        return ""


def project_t(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    """Parameter of the projection of P on segment AB (clamped to [0, 1])."""
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom <= 0:
        return 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    return max(0.0, min(1.0, t))


def point_seg_dist(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    t = project_t(px, py, ax, ay, bx, by)
    return math.hypot(px - (ax + t * (bx - ax)), py - (ay + t * (by - ay)))


def seg_intersection_t(
    a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float], d: Tuple[float, float]
) -> Optional[float]:
    """Parameter on AB where AB crosses CD, or None."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    denom = rx * sy - ry * sx
    if denom == 0:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / denom
    u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t
    return None


def snapshot_fills(board: Any) -> List[Tuple[Any, bool, Dict[int, Any]]]:
    """Copy every zone's filled polygons, so a rolled-back edit can put them back.

    Refilling after a rollback is not the same: it can differ from the fill
    the user saved (e.g. made by the GUI), which would make a failed mutation
    leave the file different.
    """
    snap = []
    for z in board.Zones():
        if z.GetIsRuleArea():
            continue
        layers = {}
        for lid in zone_copper_layers(z):
            try:
                layers[lid] = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(lid))
            except Exception:
                continue
        snap.append((z, bool(z.IsFilled()), layers))
    return snap


def restore_fills(snap: List[Tuple[Any, bool, Dict[int, Any]]]) -> None:
    for z, filled, layers in snap:
        try:
            for lid, poly in layers.items():
                z.SetFilledPolysList(lid, poly)
            z.SetIsFilled(filled)
        except Exception as e:  # a zone removed meanwhile: nothing to restore
            logger.debug(f"restore_fills skipped a zone: {e}")


# --------------------------------------------------------------------------- #
# Disk-vs-obstacle tests
#
# SWIG exposes SHAPE_CIRCLE.Collide / SHAPE_SEGMENT.Collide only with a SEG
# argument, so circles and segments are tested numerically here; anything
# else stays a generic SHAPE (whose Collide accepts any shape).
# --------------------------------------------------------------------------- #


def as_obstacle(shape: Any) -> Tuple[Any, ...]:
    name = type(shape).__name__
    try:
        if name == "SHAPE_CIRCLE":
            c = shape.GetCenter()
            return ("c", int(c.x), int(c.y), int(shape.GetRadius()))
        if name == "SHAPE_SEGMENT":
            seg = shape.GetSeg()
            return (
                "s",
                int(seg.A.x),
                int(seg.A.y),
                int(seg.B.x),
                int(seg.B.y),
                int(shape.GetWidth()) / 2,
            )
    except Exception:
        pass
    return ("g", shape)


def circle_obstacle(x: int, y: int, radius: int) -> Tuple[Any, ...]:
    return ("c", int(x), int(y), int(radius))


def segment_obstacle(a: Any, b: Any, half_width: float) -> Tuple[Any, ...]:
    return ("s", int(a.x), int(a.y), int(b.x), int(b.y), half_width)


def obstacle_box(ob: Tuple[Any, ...]) -> BBox:
    if ob[0] == "c":
        return (ob[1] - ob[3], ob[2] - ob[3], ob[1] + ob[3], ob[2] + ob[3])
    if ob[0] == "s":
        hw = int(math.ceil(ob[5]))
        return (
            min(ob[1], ob[3]) - hw,
            min(ob[2], ob[4]) - hw,
            max(ob[1], ob[3]) + hw,
            max(ob[2], ob[4]) + hw,
        )
    return shape_box(ob[1])


def disk_hits(ob: Tuple[Any, ...], x: float, y: float, radius: int, clearance: int) -> bool:
    """True when a disk at (x, y) comes closer than ``clearance`` to the obstacle."""
    if ob[0] == "c":
        return math.hypot(ob[1] - x, ob[2] - y) < ob[3] + radius + clearance
    if ob[0] == "s":
        return point_seg_dist(x, y, ob[1], ob[2], ob[3], ob[4]) < ob[5] + radius + clearance
    return bool(ob[1].Collide(pcbnew.SHAPE_CIRCLE(vec(x, y), int(radius)), int(clearance)))
