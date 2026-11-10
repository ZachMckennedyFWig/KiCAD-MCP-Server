"""Board query and inspection tools (MCP_ISSUES Part 2, section 2.4).

Four read-only questions a layout session asks over and over, each answered in
one call instead of a whole-board dump or a render:

* ``query_region``    -- what copper and text is inside a window (Q1).
* ``net_report``      -- per net: length per layer, vias, widths, islands,
                         plane-split crossings, regions entered (Q3).
* ``connected_items`` -- the copper island an item or a point belongs to (Q4).
* ``why_unconnected`` -- the islands of a net and the nearest gap between
                         them, with coordinates (Q4).

Units are millimetres. Coordinates are KiCad board coordinates: the absolute
positions stored in the .kicad_pcb (page origin top-left, +y down), not
relative to a user-set grid or drill/aux origin.

Every tool answers from the board loaded in memory, or from ``boardPath``
(loaded fresh from disk) when that is given. Outputs are compact by default:
counts plus the first ``limit`` rows (design principle 3, "summaries first").

Connectivity comes from KiCad's own engine (``BOARD.GetConnectivity()``). Its
``GetRatsnestForNet`` returns an unwrapped SwigPyObject on KiCad 10, so islands
are built from ``GetConnectedItems`` clusters and gaps are measured here.
"""

import heapq
import logging
import math
import os
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pcbnew

logger = logging.getLogger("kicad_interface")

NM_PER_MM = 1_000_000.0
DETAILS = ("summary", "items", "full")
KIND_ORDER = ("pad", "via", "track", "arc", "zone", "rule_area", "text", "footprint", "graphic")
DEFAULT_KINDS = ("pad", "via", "track", "arc", "zone", "rule_area", "text")
COLUMNS = ["kind", "id", "net", "layer", "geom", "info", "locked"]
_KIND_ALIASES = {
    "pads": "pad",
    "tracks": "track",
    "arcs": "arc",
    "vias": "via",
    "zones": "zone",
    "rule_areas": "rule_area",
    "keepout": "rule_area",
    "keepouts": "rule_area",
    "texts": "text",
    "footprints": "footprint",
    "graphics": "graphic",
}
COORDINATE_NOTE = "mm, KiCad board coordinates (as stored in the .kicad_pcb; +y is down)"

# Old KiCad layer names that callers still type.
_LEGACY_LAYER_NAMES = {
    "f.silks": "F.Silkscreen",
    "b.silks": "B.Silkscreen",
    "f.crtyd": "F.Courtyard",
    "b.crtyd": "B.Courtyard",
    "f.adhes": "F.Adhesive",
    "b.adhes": "B.Adhesive",
    "dwgs.user": "User.Drawings",
    "cmts.user": "User.Comments",
    "eco1.user": "User.Eco1",
    "eco2.user": "User.Eco2",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _err(message: str, details: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out = {"success": False, "message": message, "errorDetails": details or message}
    out.update(extra)
    return out


def _mm(nm: float) -> float:
    return round(nm / NM_PER_MM, 4)


def _r(v: float, nd: int = 3) -> float:
    return round(float(v), nd)


def _xy(vec: Any) -> Tuple[float, float]:
    return vec.x / NM_PER_MM, vec.y / NM_PER_MM


def _box_mm(box: Any) -> Tuple[float, float, float, float]:
    return (
        box.GetLeft() / NM_PER_MM,
        box.GetTop() / NM_PER_MM,
        box.GetRight() / NM_PER_MM,
        box.GetBottom() / NM_PER_MM,
    )


def _boxes_overlap(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _box_gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _uuid(item: Any) -> str:
    try:
        return item.m_Uuid.AsString()
    except Exception:
        return ""


def _short(uuid: str) -> str:
    return uuid[:8] if uuid else ""


def _detail(params: Dict[str, Any], default: str) -> Tuple[Optional[str], Optional[Dict]]:
    detail = params.get("detail") or default
    if detail not in DETAILS:
        return None, _err(
            f"Unknown detail '{detail}'", "detail must be one of: summary, items, full"
        )
    return detail, None


def _limit(params: Dict[str, Any], default: int) -> Tuple[Optional[int], Optional[Dict]]:
    raw = params.get("limit", default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, _err("limit must be an integer", f"Got: {raw!r}")
    if value < 0:
        return None, _err("limit must be >= 0", f"Got: {value}")
    return value, None


def _call_layer(fn: Any, layer: int) -> Any:
    """Call a KiCad 10 per-layer getter, falling back to the KiCad 8/9 signature."""
    try:
        return fn(layer)
    except TypeError:
        return fn()


def _enum_names(prefix: str) -> Dict[int, str]:
    names: Dict[int, str] = {}
    for attr in dir(pcbnew):
        if attr.startswith(prefix):
            value = getattr(pcbnew, attr)
            if isinstance(value, int) and not isinstance(value, bool):
                names.setdefault(value, attr[len(prefix) :].lower())
    return names


def resolve_board(
    default_board: Any, params: Dict[str, Any], key: str = "boardPath"
) -> Tuple[Any, Optional[str], Optional[Dict[str, Any]]]:
    """Return ``(board, source, error)``.

    A ``boardPath`` is always loaded fresh from disk, so the answer reflects the
    file even when a different (or stale) board is held in memory.
    """
    path = params.get(key)
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            return None, None, _err(f"Board file not found: {path}", path)
        try:
            board = pcbnew.LoadBoard(path)
        except Exception as exc:
            return None, None, _err(f"Failed to load board {path}", str(exc))
        if board is None or not hasattr(board, "GetFootprints"):
            return (
                None,
                None,
                _err(
                    "pcbnew.LoadBoard returned an unusable object",
                    "KiCad's SWIG binding handed back a bare SwigPyObject instead of a BOARD. "
                    "Restart the MCP server and try again.",
                ),
            )
        return board, "disk", None
    if default_board is None:
        return (
            None,
            None,
            _err(
                "No board is loaded", "Open a board first or pass boardPath to read one from disk"
            ),
        )
    if not hasattr(default_board, "GetFootprints"):
        return (
            None,
            None,
            _err(
                "The in-memory board is unusable (bare SwigPyObject)",
                "Reload it with open_board/reload_board, or pass boardPath",
            ),
        )
    return default_board, "memory", None


def _parse_window(params: Dict[str, Any]) -> Tuple[Optional[Tuple[float, ...]], Optional[Dict]]:
    try:
        x0, y0, x1, y1 = (float(params[k]) for k in ("x0", "y0", "x1", "y1"))
    except KeyError as exc:
        return None, _err(f"Missing {exc.args[0]}", "x0, y0, x1, y1 (mm) are required")
    except (TypeError, ValueError):
        return None, _err("x0, y0, x1, y1 must be numbers (mm)")
    rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
        return None, _err("The window has zero area", f"Got {rect}")
    return rect, None


class NetMatcher:
    """Match board net names against requested names.

    A requested name matches exactly, or, when it has no ``/``, matches the last
    path component of a hierarchical net (``SDA`` matches ``/MCU/SDA``).
    """

    def __init__(self, wanted: Optional[Iterable[str]]):
        self.wanted = [str(n) for n in wanted] if wanted else []
        self.exact = set(self.wanted)
        self.tails = {n for n in self.wanted if "/" not in n}

    def __bool__(self) -> bool:
        return bool(self.wanted)

    def match(self, name: Optional[str]) -> bool:
        if not self.wanted:
            return True
        if not name:
            return False
        if name in self.exact:
            return True
        return name.rsplit("/", 1)[-1] in self.tails


def resolve_net_names(board: Any, wanted: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Map requested names to board net names. Returns ``(names, problems)``."""
    import difflib

    all_names = [n for n in _net_names(board) if n]
    found: List[str] = []
    problems: List[str] = []
    for name in wanted:
        if name in all_names:
            found.append(name)
            continue
        tail_hits = [n for n in all_names if "/" not in name and n.rsplit("/", 1)[-1] == name]
        if len(tail_hits) == 1:
            found.append(tail_hits[0])
        elif len(tail_hits) > 1:
            problems.append(f"'{name}' is ambiguous: {', '.join(sorted(tail_hits)[:6])}")
        else:
            close = difflib.get_close_matches(name, all_names, n=3)
            hint = f" (did you mean {', '.join(close)}?)" if close else ""
            problems.append(f"net '{name}' not found{hint}")
    return found, problems


def _net_names(board: Any) -> List[str]:
    names: List[str] = []
    try:
        for name in board.GetNetsByName().keys():
            names.append(str(name))
        return names
    except Exception:
        pass
    try:
        for i in range(board.GetNetCount()):
            item = board.FindNet(i)
            if item is not None:
                names.append(item.GetNetname())
    except Exception:
        pass
    return names


class LayerNames:
    """Canonical layer names and name lookup (canonical, board name or legacy)."""

    def __init__(self, board: Any):
        self.board = board
        self._names: Dict[int, str] = {}
        self._by_name: Dict[str, int] = {}
        count = getattr(pcbnew, "PCB_LAYER_ID_COUNT", 128)
        count = count if isinstance(count, int) else 128
        for lid in range(count):
            try:
                std = board.GetStandardLayerName(lid)
            except Exception:
                continue
            if not isinstance(std, str) or not std:
                continue
            self._names[lid] = std
            self._by_name.setdefault(std.lower(), lid)
        try:
            enabled = board.GetEnabledLayers()
            self.copper = [int(lid) for lid in enabled.CuStack()]
            self.enabled = {int(lid) for lid in enabled.Seq()}
        except Exception:
            self.copper = [0, 2]
            self.enabled = set(self._names)
        for lid in self.enabled:
            try:
                user = board.GetLayerName(lid)
            except Exception:
                continue
            if isinstance(user, str) and user:
                self._by_name.setdefault(user.lower(), lid)
        for legacy, std in _LEGACY_LAYER_NAMES.items():
            if std.lower() in self._by_name:
                self._by_name.setdefault(legacy, self._by_name[std.lower()])
        self.copper_set = set(self.copper)
        self.stack_index = {lid: i for i, lid in enumerate(self.copper)}

    def name(self, lid: int) -> str:
        return self._names.get(lid, str(lid))

    def lookup(self, name: str) -> Optional[int]:
        return self._by_name.get(str(name).strip().lower())

    def resolve(self, names: Optional[Iterable[str]]) -> Tuple[Optional[Set[int]], Optional[Dict]]:
        if not names:
            return None, None
        ids: Set[int] = set()
        unknown = []
        for name in names:
            lid = self.lookup(name)
            if lid is None:
                unknown.append(str(name))
            else:
                ids.add(lid)
        if unknown:
            valid = ", ".join(self.name(l) for l in self.copper)
            return None, _err(
                f"Unknown layer(s): {', '.join(unknown)}",
                f"Use KiCad layer names, e.g. {valid}, F.Silkscreen, Edge.Cuts",
            )
        return ids, None

    def copper_of(self, layer_set: Any) -> List[int]:
        try:
            ids = [int(lid) for lid in layer_set.CuStack()]
        except Exception:
            return []
        return [lid for lid in ids if lid in self.copper_set]

    def span_label(self, ids: Sequence[int]) -> str:
        """``F.Cu``; ``*.Cu`` for every copper layer; else a comma list."""
        if not ids:
            return ""
        if len(ids) == len(self.copper) and len(ids) > 1:
            return "*.Cu"
        ordered = sorted(ids, key=lambda lid: self.stack_index.get(lid, 99 + lid))
        return ",".join(self.name(lid) for lid in ordered)


_PAD_SHAPES: Dict[int, str] = {}
_PAD_ATTRS: Dict[int, str] = {}
_VIA_TYPES: Dict[int, str] = {}


def _pad_shape_name(pad: Any, layer: int) -> str:
    if not _PAD_SHAPES:
        _PAD_SHAPES.update(_enum_names("PAD_SHAPE_"))
        _PAD_SHAPES[1] = "rect"
    try:
        return _PAD_SHAPES.get(int(_call_layer(pad.GetShape, layer)), "?")
    except Exception:
        return "?"


def _pad_attr_name(pad: Any) -> str:
    if not _PAD_ATTRS:
        _PAD_ATTRS.update(_enum_names("PAD_ATTRIB_"))
    try:
        return _PAD_ATTRS.get(int(pad.GetAttribute()), "?")
    except Exception:
        return "?"


def _via_type_name(via: Any) -> str:
    if not _VIA_TYPES:
        _VIA_TYPES.update(_enum_names("VIATYPE_"))
    try:
        return _VIA_TYPES.get(int(via.GetViaType()), "?")
    except Exception:
        return "?"


# --------------------------------------------------------------------------- #
# Pure-Python 2D geometry (millimetres)
# --------------------------------------------------------------------------- #


def point_in_poly(x: float, y: float, pts: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            xc = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xc:
                inside = not inside
        j = i
    return inside


class BandedPolygon:
    """A polygon whose edges are bucketed into horizontal bands.

    Ray casting then only visits the edges of one band, which matters for
    zone fills with thousands of vertices.
    """

    def __init__(self, pts: Sequence[Tuple[float, float]], band: float = 0.5):
        self.pts = pts
        self.band = band
        self.box = _pts_box(pts)
        self.bands: Dict[int, List[Tuple[float, float, float, float]]] = defaultdict(list)
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if y1 == y2:
                continue
            for b in range(math.floor(min(y1, y2) / band), math.floor(max(y1, y2) / band) + 1):
                self.bands[b].append((x1, y1, x2, y2))

    def contains(self, x: float, y: float) -> bool:
        box = self.box
        if not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
            return False
        inside = False
        for x1, y1, x2, y2 in self.bands.get(math.floor(y / self.band), ()):
            if (y1 > y) != (y2 > y):
                if x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                    inside = not inside
        return inside


def seg_hits_rect(
    x1: float, y1: float, x2: float, y2: float, rect: Sequence[float], pad: float = 0.0
) -> bool:
    """Liang-Barsky: does the segment touch the rectangle grown by ``pad``?"""
    rx0, ry0, rx1, ry1 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - rx0), (dx, rx1 - x1), (-dy, y1 - ry0), (dy, ry1 - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def poly_hits_rect(pts: Sequence[Tuple[float, float]], rect: Sequence[float]) -> bool:
    if not pts:
        return False
    for x, y in pts:
        if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
            return True
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    if point_in_poly(cx, cy, pts):
        return True
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if seg_hits_rect(x1, y1, x2, y2, rect):
            return True
    return False


def _pt_seg(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> Tuple[float, float, float]:
    """Distance from P to segment AB, and the closest point on AB."""
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll <= 0:
        return math.hypot(px - ax, py - ay), ax, ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / ll))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy), qx, qy


def _seg_intersect_t(
    ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float
) -> Optional[Tuple[float, float]]:
    """Parameters (t on AB, u on CD) where the two segments cross, or None."""
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if den == 0:
        return None
    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return t, u
    return None


def seg_seg(
    a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]
) -> Tuple[float, Tuple[float, float], Tuple[float, float]]:
    """Distance between two segments and the closest points (on a, on b)."""
    ax, ay, bx, by = a
    cx, cy, dx, dy = b
    hit = _seg_intersect_t(ax, ay, bx, by, cx, cy, dx, dy)
    if hit is not None:
        t = hit[0]
        p = (ax + t * (bx - ax), ay + t * (by - ay))
        return 0.0, p, p
    best = None
    for px, py, (sx0, sy0, sx1, sy1), flip in (
        (ax, ay, b, False),
        (bx, by, b, False),
        (cx, cy, a, True),
        (dx, dy, a, True),
    ):
        d, qx, qy = _pt_seg(px, py, sx0, sy0, sx1, sy1)
        if best is None or d < best[0]:
            best = (d, (qx, qy), (px, py)) if flip else (d, (px, py), (qx, qy))
    return best  # type: ignore[return-value]


def arc_points(
    s: Tuple[float, float], m: Tuple[float, float], e: Tuple[float, float], n: int = 12
) -> List[Tuple[float, float]]:
    """Polyline through the circle that passes start, mid and end."""
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [s, e]
    ux = (
        (x1 * x1 + y1 * y1) * (y2 - y3)
        + (x2 * x2 + y2 * y2) * (y3 - y1)
        + (x3 * x3 + y3 * y3) * (y1 - y2)
    ) / d
    uy = (
        (x1 * x1 + y1 * y1) * (x3 - x2)
        + (x2 * x2 + y2 * y2) * (x1 - x3)
        + (x3 * x3 + y3 * y3) * (x2 - x1)
    ) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)

    def ccw_span(a: float, b: float) -> float:
        return (b - a) % (2 * math.pi)

    span = ccw_span(a1, a3)
    if ccw_span(a1, a2) > span:  # the mid point is on the other way round
        span = span - 2 * math.pi
    return [
        (ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n))
        for i in range(n + 1)
    ]


def chain_points(chain: Any) -> List[Tuple[float, float]]:
    pts = []
    for i in range(chain.PointCount()):
        p = chain.CPoint(i)
        pts.append((p.x / NM_PER_MM, p.y / NM_PER_MM))
    return pts


def poly_set_outlines(ps: Any, fracture: bool = True) -> List[List[Tuple[float, float]]]:
    """Outlines of a SHAPE_POLY_SET in mm. Fractured, so holes are bridged in."""
    if ps is None:
        return []
    if fracture:
        try:
            if any(ps.HoleCount(i) for i in range(ps.OutlineCount())):
                ps = pcbnew.SHAPE_POLY_SET(ps)
                ps.Fracture()
        except Exception:
            pass
    out = []
    for i in range(ps.OutlineCount()):
        pts = chain_points(ps.COutline(i) if hasattr(ps, "COutline") else ps.Outline(i))
        if len(pts) >= 3:
            out.append(pts)
    return out


def item_polygons(
    item: Any, layer: int, max_error_mm: float = 0.01
) -> List[List[Tuple[float, float]]]:
    """KiCad's own copper/graphic shape of ``item`` on ``layer`` as mm polygons."""
    ps = pcbnew.SHAPE_POLY_SET()
    try:
        item.TransformShapeToPolygon(
            ps, layer, 0, int(max_error_mm * NM_PER_MM), pcbnew.ERROR_INSIDE
        )
    except Exception:
        return []
    return poly_set_outlines(ps)


def _pts_box(pts: Sequence[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# --------------------------------------------------------------------------- #
# Board index: every copper item, grouped by net
# --------------------------------------------------------------------------- #


class BoardIndex:
    """One pass over the board, grouping pads, tracks, vias and zones by net code."""

    def __init__(self, board: Any):
        self.board = board
        self.pads: Dict[int, List[Tuple[Any, Any]]] = defaultdict(list)  # (pad, footprint)
        self.tracks: Dict[int, List[Any]] = defaultdict(list)  # tracks and arcs
        self.vias: Dict[int, List[Any]] = defaultdict(list)
        self.zones: Dict[int, List[Any]] = defaultdict(list)  # copper zones, no rule areas
        self.all_zones: List[Any] = []
        self.footprints = list(board.GetFootprints())
        for fp in self.footprints:
            for pad in fp.Pads():
                self.pads[pad.GetNetCode()].append((pad, fp))
        for t in board.GetTracks():
            cls = t.GetClass()
            if cls == "PCB_VIA":
                self.vias[t.GetNetCode()].append(t)
            else:
                self.tracks[t.GetNetCode()].append(t)
        for z in board.Zones():
            self.all_zones.append(z)
            try:
                if z.GetIsRuleArea():
                    continue
            except Exception:
                pass
            self.zones[z.GetNetCode()].append(z)
        self.net_names: Dict[int, str] = {}
        try:
            for name, net in board.GetNetsByName().items():
                self.net_names[net.GetNetCode()] = str(name)
        except Exception:
            pass

    def netcode(self, name: str) -> Optional[int]:
        try:
            net = self.board.FindNet(name)
        except Exception:
            return None
        if net is None:
            return None
        return net.GetNetCode()

    def name_of(self, code: int) -> str:
        return self.net_names.get(code, "")


# --------------------------------------------------------------------------- #
# Row building (shared by query_region and connected_items)
# --------------------------------------------------------------------------- #


def _fp_locked(fp: Any) -> bool:
    try:
        return bool(fp.IsLocked())
    except Exception:
        return False


def _pad_row(pad: Any, fp: Any, ln: LayerNames, full: bool) -> Tuple[list, Dict[str, Any]]:
    x, y = _xy(pad.GetPosition())
    cu = ln.copper_of(pad.GetLayerSet())
    first = cu[0] if cu else pcbnew.F_Cu
    size = _call_layer(pad.GetSize, first)
    shape = _pad_shape_name(pad, first)
    sx, sy = _mm(size.x), _mm(size.y)
    info = f"{shape} {sx:g}x{sy:g}" if sx != sy or shape not in ("circle",) else f"{shape} {sx:g}"
    drill = pad.GetDrillSize()
    if drill.x > 0:
        info += f" d{_mm(drill.x):g}" + (f"x{_mm(drill.y):g}" if drill.y != drill.x else "")
    attr = _pad_attr_name(pad)
    layer = ln.span_label(cu) if cu else attr.upper()
    locked = _fp_locked(fp) or bool(getattr(pad, "IsLocked", lambda: False)())
    row = ["pad", f"{fp.GetReference()}.{pad.GetNumber()}", pad.GetNetname() or None, layer]
    row += [[_r(x), _r(y)], info, locked]
    extra: Dict[str, Any] = {}
    if full:
        extra = {
            "ref": fp.GetReference(),
            "number": pad.GetNumber(),
            "shape": shape,
            "type": attr,
            "size": [sx, sy],
            "drill": [_mm(drill.x), _mm(drill.y)] if drill.x > 0 else None,
            "orientation": _r(pad.GetOrientationDegrees(), 2),
            "bbox": [_r(v) for v in _box_mm(pad.GetBoundingBox())],
        }
    return row, extra


def _track_row(t: Any, ln: LayerNames, full: bool) -> Tuple[list, Dict[str, Any]]:
    sx, sy = _xy(t.GetStart())
    ex, ey = _xy(t.GetEnd())
    is_arc = t.GetClass() == "PCB_ARC"
    if is_arc:
        mx, my = _xy(t.GetMid())
        geom = [_r(sx), _r(sy), _r(mx), _r(my), _r(ex), _r(ey)]
    else:
        geom = [_r(sx), _r(sy), _r(ex), _r(ey)]
    uuid = _uuid(t)
    row = ["arc" if is_arc else "track", _short(uuid), t.GetNetname() or None]
    row += [ln.name(t.GetLayer()), geom, f"w{_mm(t.GetWidth()):g}", bool(t.IsLocked())]
    extra = {"uuid": uuid, "lengthMm": _r(t.GetLength() / NM_PER_MM)} if full else {}
    return row, extra


def _via_row(v: Any, ln: LayerNames, full: bool) -> Tuple[list, Dict[str, Any]]:
    x, y = _xy(v.GetPosition())
    top, bot = v.TopLayer(), v.BottomLayer()
    dia = _mm(_call_layer(v.GetWidth, top))
    drill = _mm(v.GetDrillValue())
    vtype = _via_type_name(v)
    uuid = _uuid(v)
    row = ["via", _short(uuid), v.GetNetname() or None, f"{ln.name(top)}-{ln.name(bot)}"]
    row += [[_r(x), _r(y)], f"{dia:g}/{drill:g}" + ("" if vtype == "through" else f" {vtype}")]
    row.append(bool(v.IsLocked()))
    extra = {"uuid": uuid, "diameter": dia, "drill": drill, "type": vtype} if full else {}
    return row, extra


def _zone_layers(z: Any, ln: LayerNames, copper_only: bool = True) -> List[int]:
    try:
        ls = z.GetLayerSet()
        ids = ln.copper_of(ls) if copper_only else [int(l) for l in ls.Seq()]
        if ids:
            return ids
    except Exception:
        pass
    try:
        return [int(z.GetLayer())]
    except Exception:
        return []


def _rule_flags(z: Any) -> str:
    flags = []
    for label, method in (
        ("tracks", "GetDoNotAllowTracks"),
        ("vias", "GetDoNotAllowVias"),
        ("pads", "GetDoNotAllowPads"),
        ("pour", "GetDoNotAllowZoneFills"),
        ("fp", "GetDoNotAllowFootprints"),
    ):
        fn = getattr(z, method, None)
        try:
            if callable(fn) and fn():
                flags.append(label)
        except Exception:
            pass
    return "no " + ",".join(flags) if flags else "rule area"


def _zone_row(
    z: Any, ln: LayerNames, full: bool, window: Optional[Sequence[float]] = None
) -> Tuple[list, Dict[str, Any]]:
    rule = bool(z.GetIsRuleArea())
    ids = _zone_layers(z, ln, copper_only=not rule)
    box = _box_mm(z.GetBoundingBox())
    name = z.GetZoneName() or None
    uuid = _uuid(z)
    if rule:
        info = _rule_flags(z)
    else:
        filled = bool(z.IsFilled())
        info = "filled" if filled else "unfilled"
        if filled and window is not None:
            hit = [ln.name(l) for l in ids if _fill_hits_rect(z, l, window)]
            info = f"fill here: {','.join(hit)}" if hit else "no fill in window"
        try:
            pri = z.GetAssignedPriority()
            if pri:
                info += f" pri{pri}"
        except Exception:
            pass
    row = ["rule_area" if rule else "zone", name or _short(uuid), None if rule else z.GetNetname()]
    row += [ln.span_label(ids) if not rule else ",".join(ln.name(l) for l in ids)]
    row += [[_r(v) for v in box], info, bool(z.IsLocked())]
    extra: Dict[str, Any] = {}
    if full:
        extra = {"uuid": uuid, "layers": [ln.name(l) for l in ids]}
        if not rule:
            try:
                extra["filledAreaMm2"] = _r(z.GetFilledArea() / (NM_PER_MM * NM_PER_MM), 2)
            except Exception:
                pass
    return row, extra


def _fill_hits_rect(z: Any, layer: int, rect: Sequence[float]) -> bool:
    try:
        if not z.HasFilledPolysForLayer(layer):
            return False
        ps = z.GetFilledPolysList(layer)
    except Exception:
        return False
    for i in range(ps.OutlineCount()):
        chain = ps.COutline(i) if hasattr(ps, "COutline") else ps.Outline(i)
        try:
            bb = _box_mm(chain.BBox())
            if not _boxes_overlap(bb, rect):
                continue
        except Exception:
            pass
        if poly_hits_rect(chain_points(chain), rect):
            return True
    return False


def _text_row(
    t: Any, ln: LayerNames, owner: Optional[str], label: str, full: bool
) -> Tuple[list, Dict[str, Any]]:
    x, y = _xy(t.GetPosition())
    text = t.GetShownText(False) if hasattr(t, "GetShownText") else t.GetText()
    try:
        text = str(text)
    except Exception:
        text = ""
    shown = text if len(text) <= 40 else text[:37] + "..."
    try:
        h = _mm(t.GetTextHeight())
    except Exception:
        h = 0
    ident = f"{owner}.{label}" if owner else _short(_uuid(t))
    row = ["text", ident, None, ln.name(t.GetLayer()), [_r(x), _r(y)], f"'{shown}' h{h:g}"]
    row.append(bool(getattr(t, "IsLocked", lambda: False)()))
    extra = {"uuid": _uuid(t), "text": text, "owner": owner} if full else {}
    return row, extra


def _fp_row(fp: Any, ln: LayerNames, full: bool) -> Tuple[list, Dict[str, Any]]:
    x, y = _xy(fp.GetPosition())
    value = fp.GetValue()
    fpid = ""
    try:
        fpid = str(fp.GetFPIDAsString())
    except Exception:
        pass
    info = value if len(value) <= 24 else value[:21] + "..."
    if fpid:
        info += " | " + fpid.split(":")[-1][:32]
    row = ["footprint", fp.GetReference(), None, ln.name(fp.GetLayer())]
    row += [[_r(x), _r(y), _r(fp.GetOrientationDegrees(), 1)], info, _fp_locked(fp)]
    extra: Dict[str, Any] = {}
    if full:
        extra = {
            "uuid": _uuid(fp),
            "value": value,
            "footprint": fpid,
            "bbox": [_r(v) for v in _box_mm(_fp_box(fp))],
            "padCount": len(list(fp.Pads())),
        }
    return row, extra


def _fp_box(fp: Any) -> Any:
    try:
        return fp.GetBoundingBox(False, False)
    except TypeError:
        return fp.GetBoundingBox()


_SHAPE_NAMES: Dict[int, str] = {}


def _graphic_row(
    g: Any, ln: LayerNames, owner: Optional[str], full: bool
) -> Tuple[list, Dict[str, Any]]:
    if not _SHAPE_NAMES:
        _SHAPE_NAMES.update(_enum_names("SHAPE_T_"))
        _SHAPE_NAMES[1] = "rect"
    try:
        shape = _SHAPE_NAMES.get(int(g.GetShape()), "?")
    except Exception:
        shape = "?"
    if shape == "segment":
        sx, sy = _xy(g.GetStart())
        ex, ey = _xy(g.GetEnd())
        geom = [_r(sx), _r(sy), _r(ex), _r(ey)]
    else:
        geom = [_r(v) for v in _box_mm(g.GetBoundingBox())]
    try:
        width = _mm(g.GetWidth())
    except Exception:
        width = 0
    uuid = _uuid(g)
    ident = f"{owner}.gfx" if owner else _short(uuid)
    row = ["graphic", ident, None, ln.name(g.GetLayer()), geom, f"{shape} w{width:g}"]
    row.append(bool(getattr(g, "IsLocked", lambda: False)()))
    extra = {"uuid": uuid, "shape": shape, "owner": owner} if full else {}
    return row, extra


def _rows_payload(
    rows: List[Tuple[list, Dict[str, Any]]], detail: str, limit: int
) -> Dict[str, Any]:
    """Compact table: ``columns`` + ``rows`` (``full`` adds an ``extra`` column)."""
    order = {k: i for i, k in enumerate(KIND_ORDER)}

    def key(entry: Tuple[list, Dict[str, Any]]) -> Tuple:
        row = entry[0]
        geom = row[4] if isinstance(row[4], list) else [0, 0]
        return (order.get(row[0], 99), str(row[2] or ""), geom[1], geom[0], str(row[1]))

    rows = sorted(rows, key=key)
    shown = rows[:limit]
    payload: Dict[str, Any] = {
        "columns": COLUMNS + (["extra"] if detail == "full" else []),
        "rows": [r + [e] if detail == "full" else r for r, e in shown],
    }
    if len(rows) > len(shown):
        payload["truncated"] = True
        payload["omitted"] = len(rows) - len(shown)
    return payload


# --------------------------------------------------------------------------- #
# Islands (connectivity clusters) and gaps
# --------------------------------------------------------------------------- #

# A primitive is a capsule: segment (x1, y1, x2, y2) grown by radius r. Points
# are zero-length segments. ``owner`` describes the copper it came from.
_Prim = Tuple[float, float, float, float, float, int]  # x1, y1, x2, y2, r, owner index

GRID_CELL_MM = 1.0


class Island:
    def __init__(self, index: int):
        self.index = index
        self.items: List[Any] = []  # pads, tracks, arcs, vias
        self.zone_uuids: Set[str] = set()
        self.fragments: List[Tuple[Any, int, List[Tuple[float, float]]]] = []  # zone, layer, pts

    def counts(self) -> Dict[str, int]:
        c = Counter(_kind_of(it) for it in self.items)
        return {
            "pads": c.get("pad", 0),
            "tracks": c.get("track", 0) + c.get("arc", 0),
            "vias": c.get("via", 0),
            "zoneFills": len(self.fragments),
        }


def _kind_of(item: Any) -> str:
    cls = item.GetClass()
    return {"PAD": "pad", "PCB_TRACK": "track", "PCB_ARC": "arc", "PCB_VIA": "via"}.get(
        cls, "zone" if cls == "ZONE" else cls.lower()
    )


def connectivity(board: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Fresh connectivity for ``board``, or a clear error for a broken SWIG object."""
    try:
        board.BuildConnectivity()
    except Exception as exc:
        logger.debug(f"BuildConnectivity failed: {exc}")
    try:
        conn = board.GetConnectivity()
    except Exception as exc:
        return None, _err("Could not read board connectivity", str(exc))
    if conn is None or not hasattr(conn, "GetConnectedItems"):
        return None, _err(
            "Board connectivity is unavailable",
            "BOARD.GetConnectivity() returned an unwrapped SWIG object "
            f"({type(conn).__name__}); this KiCad build's Python binding cannot expose it",
        )
    return conn, None


def find_islands(
    conn: Any, index: BoardIndex, ln: LayerNames, code: int, with_fragments: bool = True
) -> Tuple[List[Island], Dict[str, Any]]:
    """Group a net's copper into KiCad connectivity clusters.

    Clusters are seeded from pads, tracks and vias. A zone can belong to more
    than one cluster when its fill is split; its fill outlines are then given to
    the cluster whose copper touches them. Returns ``(islands, info)``.
    """
    # Pad outlines are cached by uuid for this call only: a later call may see
    # the same pad moved or reshaped.
    _PRIM_CACHE.clear()
    seeds: List[Any] = [p for p, _ in index.pads.get(code, [])]
    seeds += index.tracks.get(code, []) + index.vias.get(code, [])
    zones = index.zones.get(code, [])
    by_uuid = {_uuid(it): it for it in seeds}
    assigned: Dict[str, int] = {}
    islands: List[Island] = []
    unresolved = 0
    for seed in seeds:
        su = _uuid(seed)
        if su in assigned:
            continue
        island = Island(len(islands))
        island.items.append(seed)
        assigned[su] = island.index
        try:
            members = conn.GetConnectedItems(seed)
        except Exception as exc:
            logger.debug(f"GetConnectedItems failed: {exc}")
            members = []
        for m in members:
            if not hasattr(m, "GetClass"):
                unresolved += 1
                continue
            mu = _uuid(m)
            if m.GetClass() == "ZONE":
                island.zone_uuids.add(mu)
                continue
            if mu in assigned or mu not in by_uuid:
                continue
            assigned[mu] = island.index
            island.items.append(by_uuid[mu])
        islands.append(island)

    info: Dict[str, Any] = {"unfilledZones": 0, "isolatedFills": 0, "unresolvedItems": unresolved}
    zone_islands: Dict[str, List[Island]] = defaultdict(list)
    for isl in islands:
        for zu in isl.zone_uuids:
            zone_islands[zu].append(isl)
    pending: List[Tuple[Any, int, List[Tuple[float, float]], List[Island]]] = []
    for z in zones:
        if not z.IsFilled():
            info["unfilledZones"] += 1
            continue
        if not with_fragments:
            continue
        owners = zone_islands.get(_uuid(z), [])
        for layer in _zone_layers(z, ln):
            try:
                if not z.HasFilledPolysForLayer(layer):
                    continue
                outlines = poly_set_outlines(z.GetFilledPolysList(layer), fracture=False)
            except Exception:
                continue
            for pts in outlines:
                target = owners[0] if len(owners) == 1 else _fragment_owner(pts, layer, owners, ln)
                if target is None:
                    pending.append((z, layer, pts, owners))
                else:
                    target.fragments.append((z, layer, pts))
    # A fill that touches no pad, track or via may still overlap another zone's
    # fill on the same layer (zone-to-zone connection): it joins that island.
    changed = True
    while pending and changed:
        changed = False
        for entry in list(pending):
            z, layer, pts, owners = entry
            target = _fill_neighbour(pts, layer, owners or islands)
            if target is not None:
                target.fragments.append((z, layer, pts))
                pending.remove(entry)
                changed = True
    for z, layer, pts, _owners in pending:
        info["isolatedFills"] += 1
        target = Island(len(islands))
        target.fragments.append((z, layer, pts))
        islands.append(target)
    # Biggest island first, renumbered.
    islands.sort(key=lambda isl: (-isl.counts()["pads"], -len(isl.items), -len(isl.fragments)))
    for i, isl in enumerate(islands):
        isl.index = i
    return islands, info


def _fill_neighbour(
    pts: List[Tuple[float, float]], layer: int, candidates: List[Island]
) -> Optional[Island]:
    """The island holding a fill on ``layer`` that overlaps or touches ``pts``."""
    box = _pts_box(pts)
    for isl in candidates:
        for _z, flayer, other in isl.fragments:
            if flayer == layer and _boxes_overlap(box, _pts_box(other)):
                if _polys_touch(pts, other):
                    return isl
    return None


def _polys_touch(
    a: List[Tuple[float, float]], b: List[Tuple[float, float]], tol: float = 0.005
) -> bool:
    """True when two polygons overlap, one contains the other, or edges meet."""
    ba, bb = _pts_box(a), _pts_box(b)
    common = (
        max(ba[0], bb[0]) - tol,
        max(ba[1], bb[1]) - tol,
        min(ba[2], bb[2]) + tol,
        min(ba[3], bb[3]) + tol,
    )
    if common[0] > common[2] or common[1] > common[3]:
        return False
    if point_in_poly(a[0][0], a[0][1], b) or point_in_poly(b[0][0], b[0][1], a):
        return True

    def edges(pts: List[Tuple[float, float]]) -> List[Tuple[float, float, float, float]]:
        out = []
        n = len(pts)
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            if _boxes_overlap((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)), common):
                out.append((x1, y1, x2, y2))
        return out

    ea, eb = edges(a), edges(b)
    grid: Dict[Tuple[int, int], List[Tuple[float, float, float, float]]] = defaultdict(list)
    for e in eb:
        for i in range(math.floor(min(e[0], e[2])), math.floor(max(e[0], e[2])) + 1):
            for j in range(math.floor(min(e[1], e[3])), math.floor(max(e[1], e[3])) + 1):
                grid[(i, j)].append(e)
    for e in ea:
        for i in range(math.floor(min(e[0], e[2]) - tol), math.floor(max(e[0], e[2]) + tol) + 1):
            for j in range(
                math.floor(min(e[1], e[3]) - tol), math.floor(max(e[1], e[3]) + tol) + 1
            ):
                for f in grid.get((i, j), ()):
                    if seg_seg(e, f)[0] <= tol:
                        return True
    return False


def _fragment_owner(
    pts: List[Tuple[float, float]], layer: int, owners: List[Island], ln: LayerNames
) -> Optional[Island]:
    """Which island's copper touches this fill outline on ``layer``."""
    box = _pts_box(pts)
    edges = [(pts[i][0], pts[i][1], *pts[(i + 1) % len(pts)]) for i in range(len(pts))]
    for isl in owners:
        for item in isl.items:
            for prim in item_prims(item, layer, ln):
                x1, y1, x2, y2, r = prim
                pb = (min(x1, x2) - r, min(y1, y2) - r, max(x1, x2) + r, max(y1, y2) + r)
                if not _boxes_overlap(pb, box):
                    continue
                if point_in_poly(x1, y1, pts):
                    return isl
                for e in edges:
                    eb = (min(e[0], e[2]), min(e[1], e[3]), max(e[0], e[2]), max(e[1], e[3]))
                    if not _boxes_overlap(pb, eb):
                        continue
                    if seg_seg((x1, y1, x2, y2), e)[0] <= r + 0.001:
                        return isl
    return None


_PRIM_CACHE: Dict[Tuple[str, int], List[Tuple[float, float, float, float, float]]] = {}


def item_prims(
    item: Any, layer: Optional[int], ln: LayerNames
) -> List[Tuple[float, float, float, float, float]]:
    """Capsules for one item on ``layer`` (``None``: on any copper layer it has)."""
    kind = _kind_of(item)
    if kind == "track":
        if layer is not None and item.GetLayer() != layer:
            return []
        sx, sy = _xy(item.GetStart())
        ex, ey = _xy(item.GetEnd())
        return [(sx, sy, ex, ey, item.GetWidth() / NM_PER_MM / 2)]
    if kind == "arc":
        if layer is not None and item.GetLayer() != layer:
            return []
        pts = arc_points(_xy(item.GetStart()), _xy(item.GetMid()), _xy(item.GetEnd()), 8)
        r = item.GetWidth() / NM_PER_MM / 2
        return [
            (pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], r) for i in range(len(pts) - 1)
        ]
    if kind == "via":
        span = ln.copper_of(item.GetLayerSet())
        if layer is not None and layer not in span:
            return []
        x, y = _xy(item.GetPosition())
        return [(x, y, x, y, _call_layer(item.GetWidth, item.TopLayer()) / NM_PER_MM / 2)]
    if kind == "pad":
        cu = ln.copper_of(item.GetLayerSet())
        if not cu or (layer is not None and layer not in cu):
            return []
        use = layer if layer is not None else cu[0]
        key = (_uuid(item), use)
        cached = _PRIM_CACHE.get(key)
        if cached is None:
            cached = []
            for poly in item_polygons(item, use, 0.02):
                n = len(poly)
                cached += [(poly[i][0], poly[i][1], *poly[(i + 1) % n], 0.0) for i in range(n)]
            if not cached:
                x, y = _xy(item.GetPosition())
                cached = [(x, y, x, y, 0.0)]
            if len(_PRIM_CACHE) > 20000:
                _PRIM_CACHE.clear()
            _PRIM_CACHE[key] = cached
        return cached
    return []


def _describe(item: Any, ln: LayerNames) -> Dict[str, Any]:
    kind = _kind_of(item)
    if kind == "pad":
        fp = item.GetParentFootprint()
        ref = fp.GetReference() if fp is not None else "?"
        return {"kind": "pad", "id": f"{ref}.{item.GetNumber()}"}
    if kind == "via":
        return {"kind": "via", "id": _short(_uuid(item))}
    return {"kind": kind, "id": _short(_uuid(item)), "layer": ln.name(item.GetLayer())}


class _IslandGeom:
    """Capsules of one island, split into short pieces and bucketed on a grid.

    Each capsule carries a copper-layer bit mask, so a gap is measured between
    copper that shares a layer: a B.Cu track crossing over an F.Cu track is not
    a zero gap.
    """

    def __init__(self, island: Island, ln: LayerNames, cell: float = GRID_CELL_MM):
        self.cell = cell
        # x1, y1, x2, y2, radius, owner index, layer mask
        self.prims: List[Tuple[float, float, float, float, float, int, int]] = []
        self.owners: List[Tuple[Dict[str, Any], List[str]]] = []
        for item in island.items:
            ids = self._item_layer_ids(item, ln)
            owner = len(self.owners)
            self.owners.append((_describe(item, ln), [ln.name(l) for l in ids]))
            mask = _layer_mask(ids, ln)
            for p in item_prims(item, None, ln):
                self._add(p, owner, mask)
        for z, layer, pts in island.fragments:
            owner = len(self.owners)
            desc = {"kind": "zone", "id": z.GetZoneName() or _short(_uuid(z))}
            self.owners.append((desc, [ln.name(layer)]))
            mask = _layer_mask([layer], ln)
            n = len(pts)
            for i in range(n):
                x2, y2 = pts[(i + 1) % n]
                self._add((pts[i][0], pts[i][1], x2, y2, 0.0), owner, mask)
        if self.prims:
            self.box = (
                min(min(p[0], p[2]) - p[4] for p in self.prims),
                min(min(p[1], p[3]) - p[4] for p in self.prims),
                max(max(p[0], p[2]) + p[4] for p in self.prims),
                max(max(p[1], p[3]) + p[4] for p in self.prims),
            )
        else:
            self.box = (0.0, 0.0, 0.0, 0.0)
        self.mask = 0
        for p in self.prims:
            self.mask |= p[6]
        self.grid: Optional[Dict[Tuple[int, int], List[int]]] = None
        self.bounds = (0, 0, 0, 0)

    @staticmethod
    def _item_layer_ids(item: Any, ln: LayerNames) -> List[int]:
        if _kind_of(item) in ("track", "arc"):
            return [item.GetLayer()]
        return ln.copper_of(item.GetLayerSet())

    def _add(self, p: Tuple[float, float, float, float, float], owner: int, mask: int) -> None:
        x1, y1, x2, y2, r = p
        pieces = max(1, int(math.hypot(x2 - x1, y2 - y1) / self.cell))
        for i in range(pieces):
            a, b = i / pieces, (i + 1) / pieces
            self.prims.append(
                (
                    x1 + (x2 - x1) * a,
                    y1 + (y2 - y1) * a,
                    x1 + (x2 - x1) * b,
                    y1 + (y2 - y1) * b,
                    r,
                    owner,
                    mask,
                )
            )

    def _cells(self, p: Tuple) -> Tuple[int, int, int, int]:
        c = self.cell
        return (
            math.floor((min(p[0], p[2]) - p[4]) / c),
            math.floor((min(p[1], p[3]) - p[4]) / c),
            math.floor((max(p[0], p[2]) + p[4]) / c),
            math.floor((max(p[1], p[3]) + p[4]) / c),
        )

    def build_grid(self) -> Dict[Tuple[int, int], List[int]]:
        if self.grid is None:
            grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
            for idx, p in enumerate(self.prims):
                i0, j0, i1, j1 = self._cells(p)
                for i in range(i0, i1 + 1):
                    for j in range(j0, j1 + 1):
                        grid[(i, j)].append(idx)
            self.grid = grid
            if grid:
                keys = list(grid.keys())
                self.bounds = (
                    min(k[0] for k in keys),
                    min(k[1] for k in keys),
                    max(k[0] for k in keys),
                    max(k[1] for k in keys),
                )
        return self.grid

    def nearest(
        self, q: Tuple, best: float, same_layer: bool
    ) -> Optional[Tuple[float, Tuple, Tuple, int]]:
        """Closest capsule to ``q`` closer than ``best``: (gap, pt_q, pt_this, index)."""
        grid = self.build_grid()
        if not grid:
            return None
        qi0, qj0, qi1, qj1 = self._cells(q)
        bi0, bj0, bi1, bj1 = self.bounds
        start = max(0, max(bi0 - qi1, qi0 - bi1, bj0 - qj1, qj0 - bj1, 0) - 1)
        stop = max(bi1 - qi0, qi1 - bi0, bj1 - qj0, qj1 - bj0, 0) + 1
        state: List[Any] = [None, best]
        seen: Set[int] = set()
        mask = q[6] if same_layer else -1
        k = start
        while k <= stop:
            if (k - 1) * self.cell > state[1]:
                break
            i0, j0, i1, j1 = qi0 - k, qj0 - k, qi1 + k, qj1 + k
            for i in range(max(i0, bi0), min(i1, bi1) + 1):
                for j in (j0, j1) if k else range(j0, j1 + 1):
                    self._check(grid.get((i, j)), q, mask, state, seen)
            if k:
                for j in range(max(j0 + 1, bj0), min(j1 - 1, bj1) + 1):
                    for i in (i0, i1):
                        self._check(grid.get((i, j)), q, mask, state, seen)
            k += 1
        return state[0]

    def _check(self, bucket, q, mask, state, seen) -> None:
        if not bucket:
            return
        for idx in bucket:
            if idx in seen:
                continue
            seen.add(idx)
            p = self.prims[idx]
            if not (p[6] & mask):
                continue
            d, pq, pp = seg_seg((q[0], q[1], q[2], q[3]), (p[0], p[1], p[2], p[3]))
            gap = d - q[4] - p[4]
            if gap < state[1]:
                state[1] = gap
                state[0] = (gap, _surface(pq, pp, q[4]), _surface(pp, pq, p[4]), idx)


def _layer_mask(ids: Iterable[int], ln: LayerNames) -> int:
    mask = 0
    for lid in ids:
        mask |= 1 << ln.stack_index.get(lid, 63)
    return mask


def _surface(p: Tuple[float, float], toward: Tuple[float, float], r: float) -> Tuple[float, float]:
    """Move ``p`` by ``r`` toward ``toward``: from a centreline to the copper edge."""
    dx, dy = toward[0] - p[0], toward[1] - p[1]
    d = math.hypot(dx, dy)
    if d <= 0 or r <= 0:
        return p
    r = min(r, d)
    return p[0] + dx / d * r, p[1] + dy / d * r


def _prim_box(p: Tuple) -> Tuple[float, float, float, float]:
    return min(p[0], p[2]), min(p[1], p[3]), max(p[0], p[2]), max(p[1], p[3])


def _island_gap(a: _IslandGeom, b: _IslandGeom) -> Optional[Dict[str, Any]]:
    """Nearest copper-to-copper gap between two islands.

    Measured between copper on a shared layer first. Only when the islands
    share no layer is the plain 2D distance used, flagged ``needsVia``.
    """
    swapped = len(a.prims) > len(b.prims)
    if swapped:
        a, b = b, a
    if not a.prims or not b.prims:
        return None
    same_layer = bool(a.mask & b.mask)
    best = math.inf
    hit = None
    order = sorted(range(len(a.prims)), key=lambda i: _box_gap(_prim_box(a.prims[i]), b.box))
    for i in order:
        q = a.prims[i]
        if same_layer and not (q[6] & b.mask):
            continue
        if _box_gap(_prim_box(q), b.box) - q[4] > best:
            break
        found = b.nearest(q, best, same_layer)
        if found is not None and found[0] < best:
            best = found[0]
            hit = (found, i)
    if hit is None:
        return None
    (gap, pa, pb, j), i = hit
    oa = a.owners[a.prims[i][5]]
    ob = b.owners[b.prims[j][5]]
    ends = [
        {"at": [_r(pa[0]), _r(pa[1])], **oa[0], "layers": oa[1]},
        {"at": [_r(pb[0]), _r(pb[1])], **ob[0], "layers": ob[1]},
    ]
    if swapped:
        ends.reverse()
    out: Dict[str, Any] = {"gapMm": _r(max(0.0, gap)), "from": ends[0], "to": ends[1]}
    shared = sorted(set(ends[0]["layers"]) & set(ends[1]["layers"]))
    if same_layer and shared:
        out["layer"] = shared[0] if len(shared) == 1 else shared
    else:
        out["needsVia"] = True
    return out


def island_gaps(islands: List[Island], ln: LayerNames) -> List[Dict[str, Any]]:
    """Gaps that would join every island: a minimum spanning tree (lazy Kruskal).

    A gap that needs a via (the islands share no copper layer) sorts after
    every same-layer gap.
    """
    if len(islands) < 2:
        return []
    geoms = [_IslandGeom(isl, ln) for isl in islands]
    parent = list(range(len(islands)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    heap: List[Tuple[float, int, int, int, Optional[Dict]]] = []
    for i in range(len(geoms)):
        for j in range(i + 1, len(geoms)):
            if geoms[i].prims and geoms[j].prims:
                heapq.heappush(heap, (_box_gap(geoms[i].box, geoms[j].box), 0, i, j, None))
    edges: List[Dict[str, Any]] = []
    while heap and len(edges) < len(islands) - 1:
        _key, exact, i, j, data = heapq.heappop(heap)
        if find(i) == find(j):
            continue
        if not exact:
            gap = _island_gap(geoms[i], geoms[j])
            if gap is not None:
                key = gap["gapMm"] + (1e6 if gap.get("needsVia") else 0.0)
                heapq.heappush(heap, (key, 1, i, j, gap))
            continue
        parent[find(i)] = find(j)
        edges.append({"fromIsland": i + 1, "toIsland": j + 1, **(data or {})})
    return edges


def island_summary(isl: Island, ln: LayerNames, pad_limit: int = 12) -> Dict[str, Any]:
    pads = []
    layers: Set[int] = set()
    xs: List[float] = []
    ys: List[float] = []
    for item in isl.items:
        kind = _kind_of(item)
        if kind == "pad":
            fp = item.GetParentFootprint()
            pads.append(f"{fp.GetReference() if fp else '?'}.{item.GetNumber()}")
            layers.update(ln.copper_of(item.GetLayerSet()))
            x, y = _xy(item.GetPosition())
            xs.append(x)
            ys.append(y)
        elif kind == "via":
            layers.update(ln.copper_of(item.GetLayerSet()))
            x, y = _xy(item.GetPosition())
            xs.append(x)
            ys.append(y)
        else:
            layers.add(item.GetLayer())
            for pt in (item.GetStart(), item.GetEnd()):
                x, y = _xy(pt)
                xs.append(x)
                ys.append(y)
    zone_layers: Set[str] = set()
    for z, layer, pts in isl.fragments:
        zone_layers.add(ln.name(layer))
        layers.add(layer)
        bx = _pts_box(pts)
        xs += [bx[0], bx[2]]
        ys += [bx[1], bx[3]]
    pads.sort()
    out: Dict[str, Any] = {"island": isl.index + 1, **isl.counts()}
    out["layers"] = [ln.name(l) for l in sorted(layers, key=lambda l: ln.stack_index.get(l, 99))]
    if zone_layers:
        out["zoneLayers"] = sorted(zone_layers)
    if xs:
        out["bbox"] = [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))]
    out["padList"] = pads[:pad_limit]
    if len(pads) > pad_limit:
        out["padListOmitted"] = len(pads) - pad_limit
    return out


# --------------------------------------------------------------------------- #
# Plane-split crossings
# --------------------------------------------------------------------------- #


class PlaneMap:
    """Filled copper on one reference layer, by net, with an edge grid."""

    def __init__(self, layer: int, zones: List[Any], cell: float = 2.0):
        self.layer = layer
        self.cell = cell
        self.polys: List[Tuple[str, List[Tuple[float, float]], Tuple[float, ...]]] = []
        self.banded: List[Tuple[str, BandedPolygon]] = []
        self.unfilled = 0
        for z in zones:
            try:
                if z.GetIsRuleArea() or not z.IsOnLayer(layer):
                    continue
            except Exception:
                continue
            outlines: List[List[Tuple[float, float]]] = []
            try:
                if z.IsFilled() and z.HasFilledPolysForLayer(layer):
                    outlines = poly_set_outlines(z.GetFilledPolysList(layer))
                else:
                    self.unfilled += 1
                    outlines = poly_set_outlines(z.Outline())
            except Exception:
                continue
            for pts in outlines:
                net = z.GetNetname() or "<no net>"
                self.polys.append((net, pts, _pts_box(pts)))
                self.banded.append((net, BandedPolygon(pts)))
        self.edges: Dict[Tuple[int, int], List[Tuple[float, float, float, float]]] = defaultdict(
            list
        )
        for _, pts, _box in self.polys:
            n = len(pts)
            for i in range(n):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % n]
                for key in self._cells(x1, y1, x2, y2):
                    self.edges[key].append((x1, y1, x2, y2))

    def __bool__(self) -> bool:
        return bool(self.polys)

    def _cells(self, x1: float, y1: float, x2: float, y2: float) -> Iterable[Tuple[int, int]]:
        c = self.cell
        for i in range(math.floor(min(x1, x2) / c), math.floor(max(x1, x2) / c) + 1):
            for j in range(math.floor(min(y1, y2) / c), math.floor(max(y1, y2) / c) + 1):
                yield (i, j)

    def net_at(self, x: float, y: float) -> Optional[str]:
        for net, poly in self.banded:
            if poly.contains(x, y):
                return net
        return None

    def runs(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> List[Tuple[Optional[str], float, float]]:
        """Split a segment at fill edges: ``[(net or None, t_start, t_end), ...]``."""
        ts = {0.0, 1.0}
        seen: Set[Tuple[float, float, float, float]] = set()
        for key in self._cells(x1, y1, x2, y2):
            for e in self.edges.get(key, ()):
                if e in seen:
                    continue
                seen.add(e)
                hit = _seg_intersect_t(x1, y1, x2, y2, *e)
                if hit is not None:
                    ts.add(hit[0])
        cuts = sorted(ts)
        out: List[Tuple[Optional[str], float, float]] = []
        for a, b in zip(cuts, cuts[1:]):
            if b - a < 1e-9:
                continue
            m = (a + b) / 2
            net = self.net_at(x1 + (x2 - x1) * m, y1 + (y2 - y1) * m)
            if out and out[-1][0] == net:
                out[-1] = (net, out[-1][1], b)
            else:
                out.append((net, a, b))
        return out


def reference_layers_for(
    layer: int, ln: LayerNames, planes: Dict[int, PlaneMap], allowed: Optional[Set[int]]
) -> List[int]:
    """Nearest plane layers above and below ``layer`` in the copper stack."""
    if layer not in ln.stack_index:
        return []
    pos = ln.stack_index[layer]
    cands = [l for l in ln.copper if l != layer and (allowed is None or l in allowed)]
    cands = [l for l in cands if planes.get(l)]
    above = [l for l in cands if ln.stack_index[l] < pos]
    below = [l for l in cands if ln.stack_index[l] > pos]
    out = []
    if above:
        out.append(max(above, key=lambda l: ln.stack_index[l]))
    if below:
        out.append(min(below, key=lambda l: ln.stack_index[l]))
    return out


def track_crossings(
    tracks: List[Any],
    ln: LayerNames,
    planes: Dict[int, PlaneMap],
    allowed: Optional[Set[int]],
    min_void: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Reference changes under a net's tracks: (split crossings, long voids)."""
    crossings: List[Dict[str, Any]] = []
    voids: List[Dict[str, Any]] = []
    for t in tracks:
        layer = t.GetLayer()
        refs = reference_layers_for(layer, ln, planes, allowed)
        if not refs:
            continue
        if t.GetClass() == "PCB_ARC":
            pts = arc_points(_xy(t.GetStart()), _xy(t.GetMid()), _xy(t.GetEnd()), 8)
        else:
            pts = [_xy(t.GetStart()), _xy(t.GetEnd())]
        for ref in refs:
            plane = planes[ref]
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                seg_len = math.hypot(x2 - x1, y2 - y1)
                if seg_len <= 0:
                    continue
                runs = plane.runs(x1, y1, x2, y2)
                last_net: Optional[str] = None
                for net, a, b in runs:
                    length = (b - a) * seg_len
                    if net is None:
                        if length >= min_void:
                            m = (a + b) / 2
                            voids.append(
                                {
                                    "at": [_r(x1 + (x2 - x1) * m), _r(y1 + (y2 - y1) * m)],
                                    "layer": ln.name(layer),
                                    "refLayer": ln.name(ref),
                                    "lengthMm": _r(length),
                                }
                            )
                        continue
                    if last_net is not None and net != last_net:
                        crossings.append(
                            {
                                "at": [_r(x1 + (x2 - x1) * a), _r(y1 + (y2 - y1) * a)],
                                "layer": ln.name(layer),
                                "refLayer": ln.name(ref),
                                "from": last_net,
                                "to": net,
                            }
                        )
                    last_net = net
    return crossings, voids


def parse_regions(raw: Any) -> Tuple[Dict[str, List[Tuple[float, float]]], Optional[Dict]]:
    """``{name: [[x, y], ...]}`` or ``{name: {x0, y0, x1, y1}}`` -> polygons (mm)."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, _err("regions must be an object of name -> polygon or rectangle")
    out: Dict[str, List[Tuple[float, float]]] = {}
    for name, spec in raw.items():
        try:
            if isinstance(spec, dict):
                x0, y0, x1, y1 = (float(spec[k]) for k in ("x0", "y0", "x1", "y1"))
                out[str(name)] = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            else:
                pts = [(float(p[0]), float(p[1])) for p in spec]
                if len(pts) < 3:
                    raise ValueError("fewer than 3 points")
                out[str(name)] = pts
        except Exception as exc:
            return {}, _err(
                f"Bad region '{name}'",
                f"Use [[x, y], ...] (>= 3 points) or {{x0, y0, x1, y1}} in mm ({exc})",
            )
    return out, None


def _seg_in_poly(
    x1: float, y1: float, x2: float, y2: float, pts: Sequence[Tuple[float, float]]
) -> bool:
    if point_in_poly(x1, y1, pts) or point_in_poly(x2, y2, pts):
        return True
    n = len(pts)
    for i in range(n):
        if _seg_intersect_t(x1, y1, x2, y2, *pts[i], *pts[(i + 1) % n]) is not None:
            return True
    return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


class BoardQueryCommands:
    """Read-only board queries. ``board_source`` returns the in-memory board."""

    def __init__(self, board_source: Any):
        # A callable keeps the reference fresh across open/reload without the
        # interface having to push it into this object.
        self._board_source = board_source if callable(board_source) else (lambda: board_source)

    @property
    def board(self) -> Any:
        return self._board_source()

    # ------------------------------------------------------------------ Q1
    def query_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Items inside a window, as a compact table (see module docstring)."""
        rect, error = _parse_window(params)
        if error:
            return error
        detail, error = _detail(params, "items")
        if error:
            return error
        limit, error = _limit(params, 60)
        if error:
            return error
        kinds_raw = params.get("kinds")
        if kinds_raw:
            kinds = {
                _KIND_ALIASES.get(str(k).strip().lower(), str(k).strip().lower()) for k in kinds_raw
            }
            unknown = kinds - set(KIND_ORDER)
            if unknown:
                return _err(
                    f"Unknown kind(s): {', '.join(sorted(unknown))}",
                    f"kinds may be: {', '.join(KIND_ORDER)}",
                )
        else:
            kinds = set(DEFAULT_KINDS)
        board, source, error = resolve_board(self.board, params)
        if error:
            return error
        try:
            return self._query_region(board, source, rect, kinds, params, detail, limit)
        except Exception as exc:
            logger.exception("query_region failed")
            return _err("query_region failed", str(exc))

    def _query_region(self, board, source, rect, kinds, params, detail, limit) -> Dict[str, Any]:
        ln = LayerNames(board)
        layer_ids, error = ln.resolve(params.get("layers"))
        if error:
            return error
        nets = NetMatcher(params.get("nets"))
        full = detail == "full"
        rows: List[Tuple[list, Dict[str, Any]]] = []

        def on_layers(ids: Iterable[int]) -> bool:
            return layer_ids is None or bool(layer_ids.intersection(ids))

        footprints = list(board.GetFootprints())
        if "pad" in kinds:
            for fp in footprints:
                if not _boxes_overlap(_box_mm(_fp_box(fp)), rect):
                    continue
                for pad in fp.Pads():
                    if not nets.match(pad.GetNetname()):
                        continue
                    if not _boxes_overlap(_box_mm(pad.GetBoundingBox()), rect):
                        continue
                    if not on_layers([int(l) for l in pad.GetLayerSet().Seq()]):
                        continue
                    rows.append(_pad_row(pad, fp, ln, full))
        if kinds & {"track", "arc", "via"}:
            for t in board.GetTracks():
                cls = t.GetClass()
                kind = {"PCB_VIA": "via", "PCB_ARC": "arc"}.get(cls, "track")
                if kind not in kinds or not nets.match(t.GetNetname()):
                    continue
                if not _boxes_overlap(_box_mm(t.GetBoundingBox()), rect):
                    continue
                if kind == "via":
                    if not on_layers(ln.copper_of(t.GetLayerSet())):
                        continue
                    rows.append(_via_row(t, ln, full))
                    continue
                if not on_layers([t.GetLayer()]):
                    continue
                half = t.GetWidth() / NM_PER_MM / 2
                if kind == "arc":
                    pts = arc_points(_xy(t.GetStart()), _xy(t.GetMid()), _xy(t.GetEnd()), 12)
                else:
                    pts = [_xy(t.GetStart()), _xy(t.GetEnd())]
                if any(seg_hits_rect(*a, *b, rect, half) for a, b in zip(pts, pts[1:])):
                    rows.append(_track_row(t, ln, full))
        if kinds & {"zone", "rule_area"}:
            for z in board.Zones():
                rule = bool(z.GetIsRuleArea())
                if ("rule_area" if rule else "zone") not in kinds:
                    continue
                if nets and (rule or not nets.match(z.GetNetname())):
                    continue
                if not _boxes_overlap(_box_mm(z.GetBoundingBox()), rect):
                    continue
                if not on_layers(_zone_layers(z, ln, copper_only=not rule)):
                    continue
                outlines = poly_set_outlines(z.Outline())
                if not any(poly_hits_rect(pts, rect) for pts in outlines):
                    continue
                rows.append(_zone_row(z, ln, full, rect))
        if not nets:
            if "text" in kinds:
                rows += self._texts(board, footprints, ln, rect, on_layers, full)
            if "graphic" in kinds:
                rows += self._graphics(board, footprints, ln, rect, on_layers, full)
        if "footprint" in kinds:
            for fp in footprints:
                if not _boxes_overlap(_box_mm(_fp_box(fp)), rect):
                    continue
                if not on_layers([fp.GetLayer()]):
                    continue
                if nets and not any(nets.match(p.GetNetname()) for p in fp.Pads()):
                    continue
                rows.append(_fp_row(fp, ln, full))

        by_kind = Counter(r[0][0] for r in rows)
        result: Dict[str, Any] = {
            "success": True,
            "window": {"x0": rect[0], "y0": rect[1], "x1": rect[2], "y1": rect[3]},
            "units": COORDINATE_NOTE,
            "boardSource": source,
            "count": len(rows),
            "byKind": dict(sorted(by_kind.items(), key=lambda kv: KIND_ORDER.index(kv[0]))),
        }
        net_counts = Counter(r[0][2] for r in rows if r[0][2])
        if detail == "summary":
            layer_counts: Counter = Counter()
            for r, _ in rows:
                for name in str(r[3] or "").split(","):
                    if name:
                        layer_counts[name] += 1
            result["byLayer"] = dict(layer_counts.most_common())
            result["byNet"] = dict(net_counts.most_common(15))
            if len(net_counts) > 15:
                result["netsOmitted"] = len(net_counts) - 15
            return result
        result["nets"] = sorted(net_counts)[:40]
        result.update(_rows_payload(rows, detail, limit))
        return result

    @staticmethod
    def _texts(board, footprints, ln, rect, on_layers, full) -> List[Tuple[list, Dict]]:
        rows = []
        for d in board.GetDrawings():
            if d.GetClass() not in ("PCB_TEXT", "PCB_TEXTBOX"):
                continue
            if on_layers([d.GetLayer()]) and _boxes_overlap(_box_mm(d.GetBoundingBox()), rect):
                rows.append(_text_row(d, ln, None, "", full))
        for fp in footprints:
            if not _boxes_overlap(_box_mm(fp.GetBoundingBox()), rect):
                continue
            ref = fp.GetReference()
            texts: List[Tuple[Any, str]] = []
            try:
                for f in fp.GetFields():
                    texts.append((f, f.GetName()))
            except Exception:
                texts += [(fp.Reference(), "Reference"), (fp.Value(), "Value")]
            for g in fp.GraphicalItems():
                if g.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX"):
                    texts.append((g, "text"))
            for t, label in texts:
                try:
                    if not t.IsVisible():
                        continue
                except Exception:
                    pass
                if not on_layers([t.GetLayer()]):
                    continue
                if _boxes_overlap(_box_mm(t.GetBoundingBox()), rect):
                    rows.append(_text_row(t, ln, ref, label, full))
        return rows

    @staticmethod
    def _graphics(board, footprints, ln, rect, on_layers, full) -> List[Tuple[list, Dict]]:
        rows = []
        sources: List[Tuple[Any, Optional[str]]] = [(d, None) for d in board.GetDrawings()]
        for fp in footprints:
            if _boxes_overlap(_box_mm(_fp_box(fp)), rect):
                sources += [(g, fp.GetReference()) for g in fp.GraphicalItems()]
        for g, owner in sources:
            if g.GetClass() != "PCB_SHAPE":
                continue
            if not on_layers([g.GetLayer()]):
                continue
            if not _boxes_overlap(_box_mm(g.GetBoundingBox()), rect):
                continue
            rows.append(_graphic_row(g, ln, owner, full))
        return rows

    # ------------------------------------------------------------------ Q3
    def net_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Per-net routing report. See module docstring for the fields."""
        wanted = params.get("nets")
        if wanted is not None and (
            not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted)
        ):
            return _err("nets must be a list of net names")
        detail, error = _detail(params, "items" if wanted else "summary")
        if error:
            return error
        limit, error = _limit(params, 25)
        if error:
            return error
        regions, error = parse_regions(params.get("regions"))
        if error:
            return error
        try:
            min_void = float(params.get("minVoidMm", 1.0))
        except (TypeError, ValueError):
            return _err("minVoidMm must be a number (mm)")
        board, source, error = resolve_board(self.board, params)
        if error:
            return error
        try:
            return self._net_report(board, source, wanted, detail, limit, regions, min_void, params)
        except Exception as exc:
            logger.exception("net_report failed")
            return _err("net_report failed", str(exc))

    def _net_report(self, board, source, wanted, detail, limit, regions, min_void, params):
        ln = LayerNames(board)
        allowed, error = ln.resolve(params.get("referenceLayers"))
        if error:
            return error
        warnings: List[str] = []
        if wanted:
            names, problems = resolve_net_names(board, wanted)
            if problems and not names:
                return _err("No requested net was found", "; ".join(problems))
            warnings += problems
        else:
            names = sorted(n for n in _net_names(board) if n)
        conn, error = connectivity(board)
        if error:
            return error
        index = BoardIndex(board)
        do_planes = params.get("planeCrossings", True) is not False
        planes: Dict[int, PlaneMap] = {}
        if do_planes:
            for lid in ln.copper:
                pm = PlaneMap(lid, index.all_zones)
                if pm:
                    planes[lid] = pm
                    if pm.unfilled:
                        warnings.append(
                            f"{pm.unfilled} zone(s) on {ln.name(lid)} are unfilled; their outlines "
                            "were used for split checks (run refill_zones for exact fills)"
                        )
        reports = []
        for name in names:
            code = index.netcode(name)
            if code is None:
                continue
            reports.append(
                self._one_net(
                    name, code, index, conn, ln, planes, allowed, min_void, regions, detail
                )
            )
        if not wanted:
            reports.sort(key=lambda r: (r["fullyConnected"], -r.get("splitCrossings", 0), r["net"]))
        shown = reports[:limit]
        result: Dict[str, Any] = {
            "success": True,
            "units": COORDINATE_NOTE,
            "boardSource": source,
            "netCount": len(reports),
            "notFullyConnected": sum(1 for r in reports if not r["fullyConnected"]),
        }
        if do_planes:
            result["planeLayers"] = [ln.name(l) for l in ln.copper if l in planes]
            result["netsWithSplitCrossings"] = sum(1 for r in reports if r.get("splitCrossings"))
        result["nets"] = shown
        if len(reports) > len(shown):
            result["truncated"] = True
            result["omitted"] = len(reports) - len(shown)
        if warnings:
            result["warnings"] = warnings
        return result

    def _one_net(self, name, code, index, conn, ln, planes, allowed, min_void, regions, detail):
        tracks = index.tracks.get(code, [])
        vias = index.vias.get(code, [])
        pads = index.pads.get(code, [])
        zones = index.zones.get(code, [])
        by_layer: Dict[int, float] = defaultdict(float)
        widths: List[float] = []
        for t in tracks:
            by_layer[t.GetLayer()] += t.GetLength() / NM_PER_MM
            widths.append(t.GetWidth() / NM_PER_MM)
        total = sum(by_layer.values())
        islands, info = find_islands(conn, index, ln, code, with_fragments=False)
        n_islands = len(islands)
        rep: Dict[str, Any] = {
            "net": name,
            "pads": len(pads),
            "tracks": len(tracks),
            "vias": len(vias),
            "zones": len(zones),
            "lengthMm": _r(total),
            "islands": n_islands,
            "fullyConnected": n_islands <= 1,
            "unroutedConnections": max(0, n_islands - 1),
        }
        if info["unfilledZones"]:
            rep["unfilledZones"] = info["unfilledZones"]
        if detail != "summary":
            order = sorted(by_layer, key=lambda l: ln.stack_index.get(l, 99))
            rep["lengthByLayer"] = {ln.name(l): _r(by_layer[l]) for l in order}
            rep["layers"] = [ln.name(l) for l in order]
            zl: Set[int] = set()
            for z in zones:
                zl.update(_zone_layers(z, ln))
            if zl:
                rep["zoneLayers"] = [
                    ln.name(l) for l in sorted(zl, key=lambda l: ln.stack_index.get(l, 99))
                ]
            if widths:
                rep["widthMm"] = [_r(min(widths)), _r(max(widths))]
            if vias:
                sizes = Counter(
                    f"{_mm(_call_layer(v.GetWidth, v.TopLayer())):g}/{_mm(v.GetDrillValue()):g}"
                    for v in vias
                )
                rep["viaSizes"] = dict(sizes.most_common())
        if planes:
            crossings, voids = track_crossings(tracks, ln, planes, allowed, min_void)
            rep["splitCrossings"] = len(crossings)
            rep["referenceVoids"] = len(voids)
            if detail != "summary":
                cap = None if detail == "full" else 5
                if crossings:
                    rep["crossings"] = crossings[:cap]
                if voids:
                    rep["voids"] = sorted(voids, key=lambda v: -v["lengthMm"])[:cap]
        if regions:
            entered = []
            for rname, poly in regions.items():
                if self._net_enters(poly, tracks, vias, pads):
                    entered.append(rname)
            rep["regions"] = entered
        return rep

    @staticmethod
    def _net_enters(poly, tracks, vias, pads) -> bool:
        for pad, _fp in pads:
            if point_in_poly(*_xy(pad.GetPosition()), poly):
                return True
        for v in vias:
            if point_in_poly(*_xy(v.GetPosition()), poly):
                return True
        box = _pts_box(poly)
        for t in tracks:
            if not _boxes_overlap(_box_mm(t.GetBoundingBox()), box):
                continue
            if t.GetClass() == "PCB_ARC":
                pts = arc_points(_xy(t.GetStart()), _xy(t.GetMid()), _xy(t.GetEnd()), 8)
            else:
                pts = [_xy(t.GetStart()), _xy(t.GetEnd())]
            if any(_seg_in_poly(*a, *b, poly) for a, b in zip(pts, pts[1:])):
                return True
        return False

    # ------------------------------------------------------------------ Q4
    def why_unconnected(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The islands of one net and the gaps that would join them."""
        net = params.get("net")
        if not isinstance(net, str) or not net:
            return _err("net is required", "Pass the net name, e.g. GND or /Audio/LRCK")
        limit, error = _limit(params, 20)
        if error:
            return error
        board, source, error = resolve_board(self.board, params)
        if error:
            return error
        try:
            names, problems = resolve_net_names(board, [net])
            if not names:
                return _err(problems[0] if problems else f"net '{net}' not found")
            name = names[0]
            ln = LayerNames(board)
            conn, error = connectivity(board)
            if error:
                return error
            index = BoardIndex(board)
            code = index.netcode(name)
            islands, info = find_islands(conn, index, ln, code)
            gaps = island_gaps(islands, ln) if len(islands) > 1 else []
            result: Dict[str, Any] = {
                "success": True,
                "net": name,
                "units": COORDINATE_NOTE,
                "boardSource": source,
                "fullyConnected": len(islands) <= 1,
                "islandCount": len(islands),
                "islands": [island_summary(isl, ln) for isl in islands[:limit]],
            }
            if len(islands) > limit:
                result["islandsOmitted"] = len(islands) - limit
            if gaps:
                result["gaps"] = gaps[:limit]
                result["gapNote"] = (
                    "Each gap is the nearest copper-to-copper distance between two islands in "
                    "2D (layers listed per end; sharedLayers empty means a via is needed). "
                    "The gaps form a minimum spanning tree: closing all of them connects the net."
                )
            notes = []
            if info["unfilledZones"]:
                notes.append(
                    f"{info['unfilledZones']} zone(s) on this net are unfilled, so connections "
                    "through them are not counted. Run refill_zones first."
                )
            if info["isolatedFills"]:
                notes.append(
                    f"{info['isolatedFills']} zone fill fragment(s) touch no pad, track or via "
                    "of this net (isolated copper islands)."
                )
            if not islands:
                notes.append("This net has no copper items on the board.")
            if info["unresolvedItems"]:
                notes.append(
                    f"{info['unresolvedItems']} connected item(s) came back from KiCad's SWIG "
                    "binding as bare SwigPyObjects and were skipped."
                )
            if notes:
                result["notes"] = notes
            return result
        except Exception as exc:
            logger.exception("why_unconnected failed")
            return _err("why_unconnected failed", str(exc))

    def connected_items(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The island that an item (uuid, or ref+pad) or a point belongs to."""
        detail, error = _detail(params, "summary")
        if error:
            return error
        limit, error = _limit(params, 40)
        if error:
            return error
        has_point = params.get("x") is not None and params.get("y") is not None
        if not (params.get("uuid") or params.get("ref") or has_point):
            return _err(
                "Say what to start from",
                "Pass uuid (a track/via/pad/zone uuid or its first 8+ characters), "
                "ref + pad, or x + y (mm, optional layer)",
            )
        board, source, error = resolve_board(self.board, params)
        if error:
            return error
        try:
            return self._connected_items(board, source, params, detail, limit, has_point)
        except Exception as exc:
            logger.exception("connected_items failed")
            return _err("connected_items failed", str(exc))

    def _connected_items(self, board, source, params, detail, limit, has_point):
        ln = LayerNames(board)
        index = BoardIndex(board)
        hits: List[Dict[str, Any]] = []
        start, start_layer, error = self._resolve_start(board, index, ln, params, has_point, hits)
        if error:
            return error
        conn, error = connectivity(board)
        if error:
            return error
        code = start.GetNetCode()
        result: Dict[str, Any] = {
            "success": True,
            "units": COORDINATE_NOTE,
            "boardSource": source,
            "start": _describe_any(start, ln),
        }
        if len(hits) > 1:
            result["otherHits"] = hits[1:6]
        if code <= 0:
            result.update({"net": None, "note": "The item has no net, so it connects to nothing."})
            return result
        name = start.GetNetname()
        islands, info = find_islands(conn, index, ln, code)
        target = self._island_of(start, start_layer, has_point, params, islands, ln)
        result["net"] = name
        result["netIslandCount"] = len(islands)
        if target is None:
            result["note"] = "The start item is in no island (zone fill without connections)."
            return result
        result["island"] = island_summary(target, ln, pad_limit=limit)
        result["isWholeNet"] = len(islands) <= 1
        if len(islands) > 1:
            result["hint"] = "The net is split; call why_unconnected for the gaps between islands."
        if detail != "summary":
            rows = []
            full = detail == "full"
            for item in target.items:
                kind = _kind_of(item)
                if kind == "pad":
                    rows.append(_pad_row(item, item.GetParentFootprint(), ln, full))
                elif kind == "via":
                    rows.append(_via_row(item, ln, full))
                else:
                    rows.append(_track_row(item, ln, full))
            seen = set()
            for z, _layer, _pts in target.fragments:
                if _uuid(z) not in seen:
                    seen.add(_uuid(z))
                    rows.append(_zone_row(z, ln, full))
            result.update(_rows_payload(rows, detail, limit))
        return result

    @staticmethod
    def _resolve_start(board, index, ln, params, has_point, hits):
        start_layer = None
        if params.get("layer"):
            start_layer = ln.lookup(params["layer"])
            if start_layer is None:
                return None, None, _err(f"Unknown layer {params['layer']}")
        uuid = params.get("uuid")
        if uuid:
            uuid = str(uuid).lower()
            if len(uuid) < 8:
                return None, None, _err("uuid must be at least 8 characters")
            matches = []
            for fp in index.footprints:
                for pad in fp.Pads():
                    if _uuid(pad).startswith(uuid):
                        matches.append(pad)
            for t in board.GetTracks():
                if _uuid(t).startswith(uuid):
                    matches.append(t)
            for z in index.all_zones:
                if _uuid(z).startswith(uuid):
                    matches.append(z)
            if not matches:
                return None, None, _err(f"No pad, track, via or zone has uuid {uuid}")
            if len(matches) > 1:
                return None, None, _err(f"uuid prefix {uuid} is ambiguous ({len(matches)} items)")
            return matches[0], start_layer, None
        if params.get("ref"):
            ref = str(params["ref"])
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                return None, None, _err(f"Footprint {ref} not found")
            number = params.get("pad")
            if number is None:
                return None, None, _err("pad is required with ref (pad number, e.g. '5')")
            for pad in fp.Pads():
                if str(pad.GetNumber()) == str(number):
                    return pad, start_layer, None
            return None, None, _err(f"{ref} has no pad {number}")
        try:
            x, y = float(params["x"]), float(params["y"])
        except (TypeError, ValueError):
            return None, None, _err("x and y must be numbers (mm)")
        pt = pcbnew.VECTOR2I(int(round(x * NM_PER_MM)), int(round(y * NM_PER_MM)))
        found: List[Tuple[int, Any]] = []
        for fp in index.footprints:
            for pad in fp.Pads():
                try:
                    hit = (
                        pad.HitTest(pt, 0, start_layer)
                        if start_layer is not None
                        else pad.HitTest(pt)
                    )
                except TypeError:
                    hit = pad.HitTest(pt)
                if hit:
                    found.append((0, pad))
        for t in board.GetTracks():
            kind = _kind_of(t)
            if kind != "via" and start_layer is not None and t.GetLayer() != start_layer:
                continue
            if kind == "via" and start_layer is not None:
                if start_layer not in ln.copper_of(t.GetLayerSet()):
                    continue
            if t.HitTest(pt):
                found.append((1 if kind == "via" else 2, t))
        for z in index.all_zones:
            if z.GetIsRuleArea():
                continue
            for layer in _zone_layers(z, ln):
                if start_layer is not None and layer != start_layer:
                    continue
                try:
                    if z.HitTestFilledArea(layer, pt):
                        found.append((3, z))
                        if start_layer is None:
                            start_layer = layer
                        break
                except Exception:
                    continue
        if not found:
            where = f" on {ln.name(start_layer)}" if start_layer is not None else ""
            return None, None, _err(f"No copper at ({x:g}, {y:g}){where}")
        found.sort(key=lambda f: f[0])
        hits.extend(_describe_any(item, ln) for _, item in found)
        return found[0][1], start_layer, None

    @staticmethod
    def _island_of(start, layer, has_point, params, islands, ln) -> Optional[Island]:
        su = _uuid(start)
        if start.GetClass() != "ZONE":
            for isl in islands:
                if any(_uuid(it) == su for it in isl.items):
                    return isl
            return None
        # A zone: pick the fill fragment under the point (or the first one).
        px = py = None
        if has_point:
            px, py = float(params["x"]), float(params["y"])
        for isl in islands:
            for z, flayer, pts in isl.fragments:
                if _uuid(z) != su or (layer is not None and flayer != layer):
                    continue
                if px is None or point_in_poly(px, py, pts):
                    return isl
        return None


def _describe_any(item: Any, ln: LayerNames) -> Dict[str, Any]:
    if item.GetClass() == "ZONE":
        return {
            "kind": "zone",
            "id": item.GetZoneName() or _short(_uuid(item)),
            "net": item.GetNetname(),
            "layers": [ln.name(l) for l in _zone_layers(item, ln)],
        }
    out = _describe(item, ln)
    out["net"] = item.GetNetname() or None
    return out
