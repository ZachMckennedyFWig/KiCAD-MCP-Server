"""Parallel bundle routing for ``route_bus`` (mode "parallel").

A bus (e.g. I2S BCLK / LRCLK / DOUT / MCLK) is routed as one bundle:

1. The members' ends are resolved and oriented so every ``from`` end is on
   the same side; the bundle direction runs from the start centroid to the
   end centroid.
2. A guide (centre line) is routed with the grid A* router on one layer
   (vias only with ``allowVias``), with the obstacle inflation widened to
   the whole bundle: half-width = (n-1)/2 * pitch (times the 45-degree miter
   factor) + width/2, plus the usual clearance. Its ends are free grid
   points in a breakout disk around each end's pad centroid, so the corridor
   never has to squeeze between the pads themselves.
3. Members are ordered across the bundle by projecting their start pads on
   the guide's start normal (or by an explicit ``order``), and each member's
   trunk is the guide offset by its lateral index x pitch with miter joins
   (``offset_polyline``): octilinear in, octilinear out, spacing measured
   perpendicular = pitch on every segment; outer traces are longer on bends.
4. Short fan-in / fan-out legs connect each member's pads to its trunk ends
   with the A* router, treating the rest of the bundle as obstacles.
5. Every member is re-checked with the router's exact clearance check
   against the board and the other members before anything is written.

Any failure raises ``BundleFallback``; ``route_bus`` then routes the
connections sequentially and reports why.

The geometry helpers at the top are pure (no pcbnew) and unit-tested.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - KiCad builds without numpy
    np = None  # type: ignore[assignment]

from utils import copper_geometry as cg

Point = Tuple[float, float]

# 1 / cos(22.5 deg): how far a miter vertex of a 45-degree bend sits from the
# guide vertex, per unit of offset.
MITER45 = 1.0 / math.cos(math.pi / 8)
PSEUDO_NET = "\u0000route_bus_guide"
EPS = 1e-7


class OffsetError(ValueError):
    """A polyline cannot be offset; ``where`` is "start", "end" or "interior"."""

    def __init__(self, message: str, where: str = "interior") -> None:
        super().__init__(message)
        self.where = where


class BundleFallback(Exception):
    """Parallel routing is not possible; route the bus sequentially instead."""


# --------------------------------------------------------------- pure geometry
def _unit(dx: float, dy: float) -> Point:
    n = math.hypot(dx, dy)
    if n < EPS:
        raise OffsetError("zero-length segment")
    return dx / n, dy / n


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def normal(d: Point) -> Point:
    """Normal of direction ``d`` used for offsets: (-dy, dx).

    In KiCad's y-down coordinates this points to the RIGHT of the direction
    of travel, so a negative offset is on the left.
    """
    return -d[1], d[0]


def clean_polyline(
    points: Sequence[Point], layers: Optional[Sequence[str]] = None
) -> Tuple[List[Point], List[Optional[str]]]:
    """Drop zero-length segments and merge collinear same-layer runs."""
    pts = [(float(x), float(y)) for x, y in points]
    lays: List[Optional[str]] = list(layers) if layers is not None else [None] * (len(pts) - 1)
    if len(lays) != len(pts) - 1:
        raise ValueError("layers must have one entry per segment")
    out_p: List[Point] = [pts[0]]
    out_l: List[Optional[str]] = []
    for p, L in zip(pts[1:], lays):
        if math.hypot(p[0] - out_p[-1][0], p[1] - out_p[-1][1]) < EPS:
            continue
        if out_l and out_l[-1] == L and len(out_p) >= 2:
            a, b = out_p[-2], out_p[-1]
            d1 = (b[0] - a[0], b[1] - a[1])
            d2 = (p[0] - b[0], p[1] - b[1])
            if (
                abs(_cross(d1, d2)) < 1e-9 * max(1.0, math.hypot(*d1) * math.hypot(*d2))
                and (d1[0] * d2[0] + d1[1] * d2[1]) > 0
            ):
                out_p[-1] = p
                continue
        out_p.append(p)
        out_l.append(L)
    return out_p, out_l


def offset_polyline(
    points: Sequence[Point], offset: float, layers: Optional[Sequence[str]] = None
) -> Tuple[List[Point], List[Optional[str]]]:
    """Offset a polyline sideways by ``offset`` with miter joins.

    Every output segment is parallel to its guide segment at exactly
    ``|offset|`` (measured perpendicular), so two offsets of the same guide
    at o1 and o2 are ``|o1 - o2|`` apart on every segment. Positive offsets
    go to ``normal(direction)``. On the inside of a bend a segment that is
    shorter than the miter set-back would reverse; it is dropped and its
    neighbours are joined directly (OffsetError where="interior" if they are
    parallel). A reversed first/last segment raises OffsetError with
    where="start"/"end" so the caller can trim the guide.

    ``layers`` (one per segment) is carried through; returns
    (vertices, segment_layers).
    """
    pts, lays = clean_polyline(points, layers)
    if len(pts) < 2:
        raise OffsetError("polyline needs at least two distinct points")
    # lines: (offset start point, direction, layer, offset end point)
    lines = []
    for (a, b), L in zip(zip(pts, pts[1:]), lays):
        d = _unit(b[0] - a[0], b[1] - a[1])
        n = normal(d)
        lines.append(
            (
                (a[0] + offset * n[0], a[1] + offset * n[1]),
                d,
                L,
                (b[0] + offset * n[0], b[1] + offset * n[1]),
            )
        )
    if abs(offset) < EPS:
        return pts, lays

    def join(l1, l2) -> Point:
        (p1, d1, _, _), (p2, d2, _, _) = l1, l2
        c = _cross(d1, d2)
        if abs(c) < 1e-9:
            if d1[0] * d2[0] + d1[1] * d2[1] < 0:
                raise OffsetError("the guide reverses direction (180-degree turn)")
            # collinear continuation (or a parallel jog that cannot be joined)
            off = _cross(d1, (p2[0] - p1[0], p2[1] - p1[1]))
            if abs(off) > 1e-6:
                raise OffsetError("parallel offset lines cannot be joined")
            return p2
        t = _cross((p2[0] - p1[0], p2[1] - p1[1]), d2) / c
        return p1[0] + t * d1[0], p1[1] + t * d1[1]

    for _ in range(len(lines) + 1):
        verts = [lines[0][0]]
        for k in range(1, len(lines)):
            verts.append(join(lines[k - 1], lines[k]))
        verts.append(lines[-1][3])
        bad = None
        for k, ln in enumerate(lines):
            a, b = verts[k], verts[k + 1]
            if (b[0] - a[0]) * ln[1][0] + (b[1] - a[1]) * ln[1][1] <= 1e-6:
                bad = k
                break
        if bad is None:
            return verts, [ln[2] for ln in lines]
        if bad == 0:
            raise OffsetError("the first guide segment is shorter than the bend set-back", "start")
        if bad == len(lines) - 1:
            raise OffsetError("the last guide segment is shorter than the bend set-back", "end")
        del lines[bad]
    raise OffsetError("offset did not converge")  # pragma: no cover


def polyline_length(points: Sequence[Point]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def lateral_offsets(n: int, pitch: float) -> List[float]:
    """Centre-symmetric offsets for n members: -(n-1)/2*pitch ... +(n-1)/2*pitch."""
    return [(k - (n - 1) / 2.0) * pitch for k in range(n)]


def order_members(
    starts: Sequence[Point],
    direction: Point,
    ends: Optional[Sequence[Point]] = None,
    end_direction: Optional[Point] = None,
) -> List[int]:
    """Indices of the members from left to right across the bundle.

    Members are sorted by their start points projected on the normal of the
    bundle's start ``direction`` (left = most negative offset, see
    ``normal``). Ties (start pads in line with the bundle) are broken by the
    end points projected on ``end_direction``'s normal.
    """
    n0 = normal(_unit(*direction))
    ne = normal(_unit(*(end_direction or direction)))

    def key(i: int):
        a = starts[i][0] * n0[0] + starts[i][1] * n0[1]
        b = 0.0
        if ends is not None:
            b = ends[i][0] * ne[0] + ends[i][1] * ne[1]
        return (round(a, 3), b)

    return sorted(range(len(starts)), key=key)


def _seg_point_dist(p: Point, a: Point, b: Point) -> float:
    return cg.seg_point_dist(p[0], p[1], a[0], a[1], b[0], b[1])


def parallel_fraction(
    a: Sequence[Point],
    b: Sequence[Point],
    pitch: float,
    tol: float = 0.02,
    step: float = 0.05,
) -> float:
    """Fraction of polyline ``a``'s length lying at ``pitch`` (+-tol) from polyline ``b``."""
    total = 0.0
    hit = 0.0
    segs_b = list(zip(b, b[1:]))
    if not segs_b:
        return 0.0
    for p, q in zip(a, a[1:]):
        L = math.hypot(q[0] - p[0], q[1] - p[1])
        if L < EPS:
            continue
        n = max(1, int(math.ceil(L / step)))
        for k in range(n):
            t = (k + 0.5) / n
            x = (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))
            d = min(_seg_point_dist(x, s, e) for s, e in segs_b)
            total += L / n
            if abs(d - pitch) <= tol:
                hit += L / n
    return hit / total if total else 0.0


def segments_to_polylines(
    segments: Sequence[Tuple[Point, Point]], tol: float = 1e-4
) -> List[List[Point]]:
    """Chain loose segments into polylines (for measuring routed copper)."""
    segs = [
        ((round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))) for a, b in segments
    ]
    used = [False] * len(segs)
    out: List[List[Point]] = []

    def close(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        chain = [segs[i][0], segs[i][1]]
        grown = True
        while grown:
            grown = False
            for j in range(len(segs)):
                if used[j]:
                    continue
                a, b = segs[j]
                if close(a, chain[-1]):
                    chain.append(b)
                elif close(b, chain[-1]):
                    chain.append(a)
                elif close(b, chain[0]):
                    chain.insert(0, a)
                elif close(a, chain[0]):
                    chain.insert(0, b)
                else:
                    continue
                used[j] = True
                grown = True
        out.append(chain)
    return out


def grid_tail_fix(
    points: List[Tuple[float, float, str]], res: float
) -> List[Tuple[float, float, str]]:
    """Keep the runs of an A* path octilinear when an end anchor is off the grid.

    The router replaces the first/last grid point by the exact anchor, which
    skews the whole first/last run. This puts the grid point back and adds a
    short jog to the exact anchor instead.
    """
    if len(points) < 2 or res <= 0:
        return list(points)

    def snap(p):
        return (round(round(p[0] / res) * res, 4), round(round(p[1] / res) * res, 4), p[2])

    out = list(points)
    for end in (0, -1):
        p = out[end]
        g = snap(p)
        if math.hypot(g[0] - p[0], g[1] - p[1]) < 1e-6:
            continue
        nb = out[1] if end == 0 else out[-2]
        if nb[2] != p[2]:
            continue  # via at the anchor: leave it
        if end == 0:
            out = [p, g] + out[1:]
        else:
            out = out[:-1] + [g, p]
    return out


# ------------------------------------------------------------ bundle planner
def _mean_dir(dirs: Sequence[Optional[Point]]) -> Optional[Point]:
    """Mean of unit vectors, or None when they are missing or cancel out."""
    ds = [d for d in dirs if d]
    if not ds or len(ds) < len(dirs):
        return None
    sx, sy = sum(d[0] for d in ds), sum(d[1] for d in ds)
    if math.hypot(sx, sy) < 0.5 * len(ds):
        return None
    return _unit(sx, sy)


def _in_cone(pts, center: Point, direction: Optional[Point], cos_min: float = 0.25):
    """Points in front of ``center`` along ``direction`` (within ~75 degrees)."""
    if direction is None:
        return pts
    out = []
    for p in pts:
        dx, dy = p[0] - center[0], p[1] - center[1]
        if dx * direction[0] + dy * direction[1] >= cos_min * math.hypot(dx, dy):
            out.append(p)
    return out


def _common_layers(
    per_member: Sequence[Optional[Sequence[str]]], allowed: Sequence[str]
) -> List[str]:
    """Allowed layers every member's pad is on (all allowed layers if none or unknown)."""
    common = set(allowed)
    for lays in per_member:
        if lays:
            common &= set(lays)
    return [L for L in allowed if L in common] or list(allowed)


def _away_from(pts, centers: Sequence[Point], dist: float):
    """Points at least ``dist`` from every centre."""
    d2 = dist * dist
    return [p for p in pts if all((p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 >= d2 for c in centers)]


def _ep_layers(ep: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[List[str]]:
    """Copper layers of an endpoint (a pad's layers, a point's layer) or None."""
    if ep.get("kind") == "point":
        return [ep["layer"]] if ep.get("layer") else None
    try:
        return [L for L, lid in ctx["cu_ids"].items() if ep["pad"].IsOnLayer(lid)]
    except Exception:
        return None


def _pad_outward(ep: Dict[str, Any]) -> Optional[Point]:
    """Unit vector from the pad's footprint origin to the pad (its escape side)."""
    if ep.get("kind") != "pad":
        return None
    try:
        pad = ep["pad"]
        p = pad.GetPosition()
        f = pad.GetParentFootprint().GetPosition()
        dx, dy = cg.mm(p.x - f.x), cg.mm(p.y - f.y)
        if math.hypot(dx, dy) < 0.05:
            return None
        return _unit(dx, dy)
    except Exception:
        return None


def _shapes_of(net: str, plan: Any, via_size: float, cu: Sequence[str]) -> List[cg.Shape]:
    out = []
    for layer, a, b, w in plan.tracks:
        out.append(cg.Shape("capsule", (a[0], a[1], b[0], b[1], w / 2), [layer], net, "track"))
    for x, y in plan.vias:
        out.append(cg.Shape("circle", (x, y, via_size / 2), cu, net, "via"))
    return out


class _PointsPlan:
    """A guide as a bare point list (x, y, layer)."""

    def __init__(self, points):
        self.points = points


def _lane(net: str, trunk: Any, side: int, length: float) -> cg.Shape:
    """Capsule extending a trunk straight back from its start (side 0) or end (side 1)."""
    if side == 0:
        layer, a, b, w = trunk.tracks[0]
        d = _unit(b[0] - a[0], b[1] - a[1])
        p, q = a, (a[0] - length * d[0], a[1] - length * d[1])
    else:
        layer, a, b, w = trunk.tracks[-1]
        d = _unit(b[0] - a[0], b[1] - a[1])
        p, q = b, (b[0] + length * d[0], b[1] + length * d[1])
    return cg.Shape("capsule", (p[0], p[1], q[0], q[1], w / 2), [layer], net, "track")


def _merge(plans: Sequence[Any]):
    from commands.astar_router import _Plan

    m = _Plan()
    for p in plans:
        if p is None:
            continue
        m.tracks += p.tracks
        m.vias += p.vias
        m.length += p.length
        m.points += p.points
    return m


def _plan_from(verts: Sequence[Point], seg_layers: Sequence[str], width: float):
    from commands.astar_router import _Plan

    plan = _Plan()
    for k, (a, b) in enumerate(zip(verts, verts[1:])):
        a = (round(a[0], 6), round(a[1], 6))
        b = (round(b[0], 6), round(b[1], 6))
        plan.tracks.append((seg_layers[k], a, b, width))
        plan.length += math.hypot(b[0] - a[0], b[1] - a[1])
        if k > 0 and seg_layers[k] != seg_layers[k - 1]:
            plan.vias.append(a)
    plan.points = [
        (v[0], v[1], seg_layers[min(k, len(seg_layers) - 1)]) for k, v in enumerate(verts)
    ]
    return plan


class BundlePlanner:
    """Plans a parallel bundle with a RouterCommands instance (see module docstring)."""

    def __init__(self, router: Any, ctx: Dict[str, Any], params: Dict[str, Any]) -> None:
        self.rc = router
        self.ctx = ctx
        self.params = params
        self.notes: List[str] = []
        self.timing: Dict[str, float] = {}
        # intermediate geometry, for diagnostics (guide, trunks, legs)
        self.debug: Dict[str, Any] = {}
        self.deadline = time.time() + 60.0
        self.guide_failure = "no corridor"
        self.pitch = 0.0

    # ----------------------------------------------------------- members
    def _members(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rc, ctx, params = self.rc, self.ctx, self.params
        shared = {
            k: v
            for k, v in params.items()
            if k not in ("connections", "dryRun", "pitch", "mode", "order", "layer", "allowVias")
        }
        members, skipped = [], []
        for i, c in enumerate(params.get("connections") or []):
            net = c.get("net")
            p = dict(shared)
            p.update({k: v for k, v in c.items() if k != "net"})
            frm, to = p.pop("from", None), p.pop("to", None)
            if (frm is None) != (to is None):
                raise ValueError(f"connections[{i}] ({net}): give both from and to, or neither")
            if frm is None:
                pair = rc._missing_connection(net)
                if pair is None:
                    skipped.append({"net": net, "alreadyConnected": True})
                    continue
                frm = {
                    "ref": pair[0].GetParentFootprint().GetReference(),
                    "pad": pair[0].GetNumber(),
                }
                to = {
                    "ref": pair[1].GetParentFootprint().GetReference(),
                    "pad": pair[1].GetNumber(),
                }
            ep_a = cg.parse_point(frm, rc.board)
            ep_b = cg.parse_point(to, rc.board)
            opts = rc._options(ctx, net, p)
            _, ids_a = rc._endpoint_group(ctx, ep_a, net, ctx["cu"])
            _, ids_b = rc._endpoint_group(ctx, ep_b, net, ctx["cu"])
            if ids_a & ids_b:
                skipped.append({"net": net, "alreadyConnected": True})
                continue
            from commands.astar_router import _endpoint_xy

            members.append(
                {
                    "net": net,
                    "params": p,
                    "opts": opts,
                    "from": frm,
                    "to": to,
                    "a": _endpoint_xy(ep_a),
                    "b": _endpoint_xy(ep_b),
                    "labelA": ep_a["label"],
                    "labelB": ep_b["label"],
                    "outA": _pad_outward(ep_a),
                    "outB": _pad_outward(ep_b),
                    "layersA": _ep_layers(ep_a, ctx),
                    "layersB": _ep_layers(ep_b, ctx),
                }
            )
        # orient: every member's "from" on the same side as the first member's
        if members:
            a0, b0 = members[0]["a"], members[0]["b"]
            for m in members[1:]:
                same = math.dist(m["a"], a0) + math.dist(m["b"], b0)
                swap = math.dist(m["a"], b0) + math.dist(m["b"], a0)
                if swap < same:
                    m["a"], m["b"] = m["b"], m["a"]
                    m["from"], m["to"] = m["to"], m["from"]
                    m["labelA"], m["labelB"] = m["labelB"], m["labelA"]
                    m["outA"], m["outB"] = m["outB"], m["outA"]
                    m["layersA"], m["layersB"] = m["layersB"], m["layersA"]
                    m["swapped"] = True
        return members, skipped

    # ----------------------------------------------------- free breakout
    def _free_points(
        self,
        center: Point,
        radius: float,
        res: float,
        layers: Sequence[str],
        half_width: float,
        clr_override: Optional[float],
        avoid: Sequence[Any],
    ) -> List[Tuple[float, float, str]]:
        """Grid points within ``radius`` of ``center`` where the guide fits."""
        ctx = self.ctx
        rules: cg.Rules = ctx["rules"]
        step = res * (2 if radius > 3 else 1)
        cx, cy = center
        i0 = int(math.floor((cx - radius) / step))
        i1 = int(math.ceil((cx + radius) / step))
        j0 = int(math.floor((cy - radius) / step))
        j1 = int(math.ceil((cy + radius) / step))
        xs = np.round(np.arange(i0, i1 + 1) * step, 4)
        ys = np.round(np.arange(j0, j1 + 1) * step, 4)
        gx, gy = np.meshgrid(xs, ys, indexing="ij")
        disk = np.hypot(gx - cx, gy - cy) <= radius
        margin = 0.15 * res + 0.012
        box = (cx - radius - half_width - 2, cy - radius - half_width - 2)
        box2 = (cx + radius + half_width + 2, cy + radius + half_width + 2)

        def near(s: cg.Shape) -> bool:
            bx0, by0, bx1, by1 = s.bbox
            return not (bx1 < box[0] or bx0 > box2[0] or by1 < box[1] or by0 > box2[1])

        # board: inside the outline and away from the edges
        inside = np.zeros(gx.shape, dtype=bool)
        for poly in ctx["outlines"]:
            ring = np.zeros(gx.shape, dtype=bool)
            n = len(poly)
            for k in range(n):
                ax, ay = poly[k]
                bx, by = poly[(k + 1) % n]
                if ay == by:
                    continue
                ring ^= ((ay > gy) != (by > gy)) & (gx < ax + (gy - ay) * (bx - ax) / (by - ay))
            inside ^= ring
        ok_all = disk & inside
        for e in cg.outline_edges(ctx["outlines"]):
            if near(e):
                ok_all &= e.distance_grid(gx, gy) >= rules.edge_clearance + half_width + margin
        out: List[Tuple[float, float, str]] = []
        for L in layers:
            ok = ok_all.copy()
            for s in ctx["shapes"]:
                if L not in s.layers or not near(s):
                    continue
                if s.role == "hole":
                    need = max(rules.hole_clearance, clr_override or 0.0)
                elif s.extra_clearance:
                    need = s.extra_clearance
                else:
                    need = (
                        clr_override
                        if clr_override is not None
                        else rules.clearance(PSEUDO_NET, s.net)
                    )
                ok &= s.distance_grid(gx, gy) >= need + half_width + margin
            for s in ctx["keepouts"]:
                if s.meta.get("noTracks") and L in s.layers and near(s):
                    ok &= s.distance_grid(gx, gy) >= half_width + margin
            for poly, lays in avoid:
                if lays and L not in lays:
                    continue
                reg = cg.Shape("poly", (tuple(poly),), [L], "", "keepout")
                if near(reg):
                    ok &= reg.distance_grid(gx, gy) >= half_width + margin
            for i, j in zip(*np.nonzero(ok)):
                out.append((float(xs[i]), float(ys[j]), L))
        return out

    # ------------------------------------------------------------- guide
    def _guide(self, members, pitch, layer, guide_layers, extra_clr):
        rc, ctx = self.rc, self.ctx
        n = len(members)
        o_max = (n - 1) / 2.0 * pitch
        w_max = max(m["opts"]["width"] for m in members)
        via_max = max(m["opts"]["viaSize"] for m in members)
        base = members[0]["opts"]
        half_w = o_max * MITER45 + w_max / 2 + extra_clr
        half_v = o_max * MITER45 + via_max / 2 + extra_clr
        other_cost = float(self.params.get("otherLayerCost", 1.5))
        prefer_cost = {L: (1.0 if L == layer else other_cost) for L in guide_layers}
        gopts = dict(
            base,
            width=2 * half_w,
            viaSize=2 * half_v,
            layers=list(guide_layers),
            layerCost=prefer_cost,
            viaCost=base["viaCost"] * n,  # a layer change puts n vias side by side
        )
        res = gopts["gridStep"]
        ca = (sum(m["a"][0] for m in members) / n, sum(m["a"][1] for m in members) / n)
        cb = (sum(m["b"][0] for m in members) / n, sum(m["b"][1] for m in members) / n)
        spread_a = max(math.dist(m["a"], ca) for m in members)
        spread_b = max(math.dist(m["b"], cb) for m in members)
        clr = (
            base["clearance"]
            if base["clearance"] is not None
            else max(ctx["rules"].for_net(m["net"])["clearance"] for m in members)
        )
        gap = math.dist(ca, cb)
        attempts = []
        user_r = self.params.get("breakoutRadius")
        out_a = _mean_dir([m.get("outA") for m in members])
        out_b = _mean_dir([m.get("outB") for m in members])
        # keep the trunk ends back from the pads: room for the fan-in legs
        # (and their vias) so the outer trunk ends never sit in front of a pin
        leg_room = float(
            self.params.get("legRoom", 0.8 + (via_max if self.params.get("allowVias") else 0.0))
        )
        keep_off = half_w + clr + leg_room
        # breakout points on the pads' own copper layer first (no leg vias),
        # facing away from the pads; then any guide layer; then any side
        lay_a = _common_layers([m.get("layersA") for m in members], guide_layers)
        lay_b = _common_layers([m.get("layersB") for m in members], guide_layers)
        tries = [(e, True, True) for e in ([0.0] if user_r else [0.5, 2.0, 4.5])]
        if len(guide_layers) > 1 and (lay_a != guide_layers or lay_b != guide_layers):
            tries.append((tries[-1][0], True, False))
        if out_a or out_b:
            tries.append((tries[-1][0], False, False))  # last resort: any side of the pads

        def breakout(center, radius, lays, pads, cone_dir):
            pts = self._free_points(
                center, radius, res, lays, half_w, base["clearance"], base["avoid"]
            )
            pts = _away_from(pts, pads, keep_off)
            return _in_cone(pts, center, cone_dir)

        pads_a = [m["a"] for m in members]
        pads_b = [m["b"] for m in members]
        r0_a = spread_a + keep_off + tries[0][0]
        r0_b = spread_b + keep_off + tries[0][0]
        for t_i, (extra, cone, pad_layers) in enumerate(tries):
            ra = float(user_r) if user_r else spread_a + keep_off + extra
            rb = float(user_r) if user_r else spread_b + keep_off + extra
            if ra + rb >= gap - 0.5:
                attempts.append(
                    {"breakout": [round(ra, 3), round(rb, 3)], "reason": "ends too close"}
                )
                break
            la = lay_a if pad_layers else guide_layers
            lb = lay_b if pad_layers else guide_layers
            src = breakout(ca, ra, la, pads_a, out_a if cone else None)
            dst = breakout(cb, rb, lb, pads_b, out_b if cone else None)
            att = {
                "breakout": [round(ra, 3), round(rb, 3)],
                "keepOffPads": round(keep_off, 3),
                "facingPads": cone,
                "padLayersOnly": pad_layers,
                "startCandidates": len(src),
                "endCandidates": len(dst),
            }
            attempts.append(att)
            if not src or not dst:
                att["reason"] = "no free breakout point for the corridor"
                continue
            m = gopts["searchMargin"]
            window = (
                min(ca[0] - ra, cb[0] - rb) - m,
                min(ca[1] - ra, cb[1] - rb) - m,
                max(ca[0] + ra, cb[0] + rb) + m,
                max(ca[1] + ra, cb[1] + rb) + m,
            )
            last = t_i == len(tries) - 1
            for win in (window, None) if last else (window,):
                plan, st = rc._plan(ctx, PSEUDO_NET, src, dst, gopts, (), win)
                att.setdefault("searches", []).append(
                    dict(
                        {k: st.get(k) for k in ("search", "expanded", "reason", "grid")},
                        window="local" if win else "board",
                    )
                )
                if plan is not None:
                    # a wide breakout leaves long legs: pull each guide end in
                    # towards its pads as far as the corridor allows
                    pts = list(plan.points)
                    ext = []
                    if not user_r and ra > r0_a + 1e-6:
                        near = breakout(ca, r0_a, la, pads_a, out_a if cone else None)
                        pts, ok = self._extend(pts, 0, near, gopts)
                        ext.append(ok)
                    if not user_r and rb > r0_b + 1e-6:
                        near = breakout(cb, r0_b, lb, pads_b, out_b if cone else None)
                        pts, ok = self._extend(pts, 1, near, gopts)
                        ext.append(ok)
                    if ext:
                        att["endsPulledIn"] = ext
                    plan = _PointsPlan(pts)
                    yield plan, att, {
                        "corridorHalfWidthMm": round(half_w, 3),
                        "attempts": attempts,
                        "startCentroid": [round(ca[0], 3), round(ca[1], 3)],
                        "endCentroid": [round(cb[0], 3), round(cb[1], 3)],
                    }
                    break
                if st.get("enclosed"):
                    break
            if time.time() > self.deadline:
                attempts.append({"reason": "time budget exhausted"})
                break
        reason = (attempts[-1].get("reason") if attempts else None) or "no corridor"
        if attempts and "searches" in attempts[-1] and not attempts[-1].get("reason"):
            reason = attempts[-1]["searches"][-1].get("reason") or reason
        self.guide_failure = (
            f"no corridor wide enough for {n} members at pitch {pitch} mm "
            f"(corridor half-width {half_w:.3f} mm + clearance): {reason}"
        )

    def _extend(self, pts, side, targets, gopts):
        """Extend guide end ``side`` (0 = start) to the nearest of ``targets``."""
        if not targets:
            return pts, False
        e = pts[0] if side == 0 else pts[-1]
        xs = [p[0] for p in targets] + [e[0]]
        ys = [p[1] for p in targets] + [e[1]]
        win = (min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3)
        try:
            ext, _ = self.rc._plan(self.ctx, PSEUDO_NET, [e], targets, gopts, (), win)
        except Exception:
            return pts, False
        if ext is None or len(ext.points) < 2:
            return pts, False
        ep = list(ext.points)
        # junction must not double back on the guide
        if side == 0:
            g_dir = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
            x_dir = (ep[0][0] - ep[1][0], ep[0][1] - ep[1][1])
        else:
            g_dir = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
            x_dir = (ep[1][0] - ep[0][0], ep[1][1] - ep[0][1])
        if g_dir[0] * x_dir[0] + g_dir[1] * x_dir[1] < 0:
            return pts, False
        if side == 0:
            return list(reversed(ep))[:-1] + pts, True
        return pts + ep[1:], True

    # -------------------------------------------------------------- plan
    def plan(self) -> Dict[str, Any]:
        t0 = time.time()
        ctx, params = self.ctx, self.params
        members, skipped = self._members()
        if len(members) < 2:
            raise BundleFallback("fewer than two unrouted connections to bundle")
        rules: cg.Rules = ctx["rules"]
        cu = ctx["cu"]
        n = len(members)

        # layer + vias
        base = members[0]["opts"]
        layer = params.get("layer") or params.get("preferLayer") or base["layers"][0]
        if layer not in cu:
            raise ValueError(f"layer {layer} is not a copper layer; board has {cu}")
        if layer not in base["layers"]:
            raise ValueError(f"layer {layer} is not in layersAllowed {base['layers']}")
        allow_vias = bool(params.get("allowVias", False))

        # pitch
        w_max = max(m["opts"]["width"] for m in members)
        clr_override = base["clearance"]
        mutual = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                mutual = max(
                    mutual,
                    (
                        clr_override
                        if clr_override is not None
                        else rules.clearance(members[i]["net"], members[j]["net"])
                    ),
                )
        min_pitch = w_max + mutual
        pitch = params.get("pitch")
        if pitch is None:
            pitch = math.ceil(min_pitch / 0.05 - 1e-9) * 0.05
        pitch = float(pitch)
        if pitch < min_pitch - 1e-9:
            self.notes.append(
                f"pitch {pitch} mm is below width + clearance; using {min_pitch:.3f} mm"
            )
            pitch = min_pitch
        self.pitch = pitch
        guide_layers = [layer]
        if allow_vias:
            via_max = max(m["opts"]["viaSize"] for m in members)
            if pitch + 1e-9 >= via_max + mutual:
                guide_layers = [layer] + [L for L in base["layers"] if L != layer]
            else:
                self.notes.append(
                    f"pitch {pitch} mm < via size + clearance ({via_max + mutual:.3f} mm): "
                    "the bundle trunk stays on one layer (legs may still use vias)"
                )
        # members of a stricter class than the guide's pseudo net
        extra_clr = 0.0
        if clr_override is None:
            extra_clr = max(
                0.0,
                max(rules.for_net(m["net"])["clearance"] for m in members)
                - rules.for_net(PSEUDO_NET)["clearance"],
            )

        # guide candidates (breakout radius / side / layer variants); the first
        # one whose offsets, legs and clearance check all work is used
        self.deadline = t0 + float(params.get("timeBudget", 60.0))
        failures: List[str] = []
        t1 = time.time()
        for gplan, att, ginfo in self._guide(members, pitch, layer, guide_layers, extra_clr):
            try:
                out = self._build(members, skipped, gplan, ginfo, pitch, layer, allow_vias)
            except BundleFallback as exc:
                att["result"] = str(exc)
                failures.append(str(exc))
                continue
            out["timing"]["total"] = round(time.time() - t0, 3)
            out["timing"]["search"] = round(time.time() - t1, 3)
            return out
        if failures:
            raise BundleFallback(failures[-1])
        raise BundleFallback(self.guide_failure)

    def _build(self, members, skipped, gplan, ginfo, pitch, layer, allow_vias):
        rc, ctx, params = self.rc, self.ctx, self.params
        cu = ctx["cu"]
        n = len(members)
        t0 = time.time()
        gpts: List[Point] = []
        glays: List[str] = []
        for x, y, L in gplan.points:
            if gpts and math.hypot(x - gpts[-1][0], y - gpts[-1][1]) < EPS:
                continue  # layer change at the same point
            if gpts:
                glays.append(L)
            gpts.append((x, y))
        if len(gpts) < 2:
            raise BundleFallback("the guide is degenerate (start and end breakouts touch)")
        gpts, glays = clean_polyline(gpts, glays)
        self.debug["guide"] = list(gpts)

        # order + offsets (trim guide ends that are too short for the set-back)
        offsets = lateral_offsets(n, pitch)
        explicit = params.get("order")
        for _ in range(6):
            d0 = _unit(gpts[1][0] - gpts[0][0], gpts[1][1] - gpts[0][1])
            de = _unit(gpts[-1][0] - gpts[-2][0], gpts[-1][1] - gpts[-2][1])
            if explicit:
                names = [m["net"] for m in members]
                if sorted(explicit) != sorted(names):
                    raise ValueError(
                        f"order must list exactly the bundled nets {names} (got {explicit})"
                    )
                order = [names.index(nm) for nm in explicit]
            else:
                order = order_members([m["a"] for m in members], d0, [m["b"] for m in members], de)
            trunks: Dict[int, Tuple[List[Point], List[str]]] = {}
            trim = None
            for k, mi in enumerate(order):
                try:
                    trunks[mi] = offset_polyline(gpts, offsets[k], glays)  # type: ignore[assignment]
                except OffsetError as exc:
                    if exc.where in ("start", "end") and len(gpts) > 2:
                        trim = exc.where
                        break
                    raise BundleFallback(f"offsetting the guide failed: {exc}") from exc
            if trim is None:
                break
            if trim == "start":
                gpts, glays = gpts[1:], glays[1:]
            else:
                gpts, glays = gpts[:-1], glays[:-1]
        else:
            raise BundleFallback("offsetting the guide failed: the guide is too short")
        ne = normal(de)
        end_order = sorted(
            range(n), key=lambda i: members[i]["b"][0] * ne[0] + members[i]["b"][1] * ne[1]
        )
        if end_order != order:
            self.notes.append(
                "the members' order at the end pads differs from the start order; "
                "fan-out legs must cross the bundle (expect a fallback, a via or a long leg)"
            )

        # trunk plans + exact check against the board and each other
        trunk_plans: Dict[int, Any] = {}
        for k, mi in enumerate(order):
            m = members[mi]
            verts, lays = trunks[mi]
            trunk_plans[mi] = _plan_from(verts, lays, m["opts"]["width"])
            m["offset"] = offsets[k]
        for mi in order:
            m = members[mi]
            others = [
                s
                for mj in order
                if mj != mi
                for s in _shapes_of(
                    members[mj]["net"], trunk_plans[mj], members[mj]["opts"]["viaSize"], cu
                )
            ]
            bad = rc._check(ctx, m["net"], trunk_plans[mi], m["opts"], others)
            if bad:
                v = bad[0]
                raise BundleFallback(
                    f"the offset trunk of {m['net']} fails clearance at {v['at']} "
                    f"({v['obstacle']}, gap {v['gapMm']} < {v['requiredMm']} mm)"
                )

        self.debug["trunks"] = {members[mi]["net"]: trunk_plans[mi] for mi in order}
        # legs
        t2 = time.time()
        legs = self._legs(members, order, trunk_plans, layer, allow_vias)
        self.timing["legs"] = round(time.time() - t2, 3)

        # final exact check of every member against the board and the others
        full: Dict[int, Any] = {}
        for mi in order:
            full[mi] = _merge([legs[mi][0], trunk_plans[mi], legs[mi][1]])
        violations = []
        for mi in order:
            m = members[mi]
            others = [
                s
                for mj in order
                if mj != mi
                for s in _shapes_of(
                    members[mj]["net"], full[mj], members[mj]["opts"]["viaSize"], cu
                )
            ]
            bad = rc._check(ctx, m["net"], full[mi], m["opts"], others)
            violations += [dict(v, net=m["net"]) for v in bad]
        if violations:
            v = violations[0]
            raise BundleFallback(
                f"the bundle fails the exact clearance check ({len(violations)} violations, "
                f"first: {v['net']} at {v['at']} vs {v['obstacle']})"
            )

        # report
        out_members = []
        for k, mi in enumerate(order):
            m = members[mi]
            pl = full[mi]
            ls, le = legs[mi]
            out_members.append(
                {
                    "net": m["net"],
                    "from": m["labelA"],
                    "to": m["labelB"],
                    "offsetMm": round(m["offset"], 4),
                    "lengthMm": round(pl.length, 3),
                    "trunkLengthMm": round(trunk_plans[mi].length, 3),
                    "legLengthsMm": [round(ls.length, 3), round(le.length, 3)],
                    "segments": len(pl.tracks),
                    "vias": len(pl.vias),
                    "layersUsed": sorted({t[0] for t in pl.tracks}),
                    "success": True,
                }
            )
        par = []
        for k in range(n - 1):
            a, b = order[k], order[k + 1]
            frac = parallel_fraction(trunks[a][0], trunks[b][0], pitch, tol=0.01)
            full_a = [(t[1], t[2]) for t in full[a].tracks]
            fa = segments_to_polylines(full_a)
            fb = segments_to_polylines([(t[1], t[2]) for t in full[b].tracks])
            tot = sum(polyline_length(p) for p in fa) or 1.0
            at_pitch = 0.0
            for pa in fa:
                L = polyline_length(pa)
                at_pitch += L * max(
                    (parallel_fraction(pa, pb, pitch, tol=0.01) for pb in fb), default=0.0
                )
            par.append(
                {
                    "nets": [members[a]["net"], members[b]["net"]],
                    "trunkAtPitch": round(frac, 3),
                    "routeAtPitch": round(at_pitch / tot, 3),
                }
            )
        self.timing["build"] = round(time.time() - t0, 3)
        return {
            "members": out_members,
            "skipped": skipped,
            "planned": [(members[mi]["net"], full[mi], members[mi]["opts"]) for mi in order],
            "order": [members[mi]["net"] for mi in order],
            "pitch": round(pitch, 4),
            "layer": layer,
            "guide": dict(
                ginfo,
                lengthMm=round(polyline_length(gpts), 3),
                vertices=len(gpts),
                layers=sorted(set(glays)),
            ),
            "parallelism": par,
            "notes": self.notes,
            "timing": self.timing,
        }

    # -------------------------------------------------------------- legs
    def _legs(self, members, order, trunk_plans, layer, allow_vias):
        """Fan-in (side 0) and fan-out (side 1) legs from the pads to the trunk ends.

        Each side is routed member by member with the rest of the bundle as
        obstacles; when one leg fails or detours, the side is retried in
        other member orders (outermost first, innermost first, ...).
        """
        legs: Dict[int, List[Any]] = {mi: [None, None] for mi in order}
        errors: List[str] = []
        n = len(order)
        mid = sorted(order, key=lambda mi: abs(order.index(mi) - (n - 1) / 2.0))
        candidates = []
        for seq in (list(order), list(reversed(order)), mid, list(reversed(mid))):
            if seq not in candidates:
                candidates.append(seq)
        for side in (0, 1):
            done = False
            for seq in candidates:
                for mi in order:
                    legs[mi][side] = None
                ok = True
                for mi in seq:
                    plan, err = self._leg(members, order, trunk_plans, legs, mi, side, allow_vias)
                    if plan is None:
                        errors.append(err)
                        self.debug.setdefault("failedLegs", []).append(
                            (
                                members[mi]["net"],
                                side,
                                [
                                    (members[x]["net"], lg)
                                    for x in order
                                    for lg in legs[x]
                                    if lg is not None
                                ],
                            )
                        )
                        ok = False
                        break
                    legs[mi][side] = plan
                if ok:
                    done = True
                    break
            if not done:
                raise BundleFallback("fan-in/fan-out legs failed: " + "; ".join(errors[:2]))
        self.debug["legs"] = [(members[mi]["net"], lg) for mi in order for lg in legs[mi]]
        return legs

    def _leg(self, members, order, trunk_plans, legs, mi, side, allow_vias):
        from commands.astar_router import _simplify

        rc, ctx = self.rc, self.ctx
        cu = ctx["cu"]
        m = members[mi]
        tp = trunk_plans[mi]
        if side == 0:
            (tx, ty), tl = tp.tracks[0][1], tp.tracks[0][0]
            pad_spec, pad_xy = m["from"], m["a"]
        else:
            (tx, ty), tl = tp.tracks[-1][2], tp.tracks[-1][0]
            pad_spec, pad_xy = m["to"], m["b"]
        what = f"{m['net']} {'fan-in' if side == 0 else 'fan-out'} leg"
        lane = max(0.5, min(2.0, (len(order) - 1) * self.pitch))
        extra = []
        for mj in order:
            if mj == mi:
                continue
            vs = members[mj]["opts"]["viaSize"]
            extra += _shapes_of(members[mj]["net"], trunk_plans[mj], vs, cu)
            for lg in legs[mj]:
                if lg is not None:
                    extra += _shapes_of(members[mj]["net"], lg, vs, cu)
            if legs[mj][side] is None:
                # reserve the straight approach lane behind the other members'
                # trunk ends so a leg never cuts across a neighbour's way in
                extra.append(_lane(members[mj]["net"], trunk_plans[mj], side, lane))
        lp = dict(m["params"])
        lp.pop("preferLayer", None)
        lp.pop("otherLayerCost", None)
        if allow_vias:
            lp["layersAllowed"] = list(m["opts"]["layers"])
            if tl in lp["layersAllowed"]:
                lp["preferLayer"] = tl
        else:
            lp["layersAllowed"] = [tl]
        try:
            opts = rc._options(ctx, m["net"], lp)
            ep = cg.parse_point(pad_spec, rc.board)
            src, _ = rc._endpoint_group(ctx, ep, m["net"], opts["layers"])
        except ValueError as exc:
            return None, f"{what}: {exc}"
        direct = math.dist(pad_xy, (tx, ty))
        # legs are local: search a window around the pad and the trunk end
        # only (full grid, then half grid for fine-pitch pins), never the board
        margin = opts["searchMargin"]
        window = (
            min(pad_xy[0], tx) - margin,
            min(pad_xy[1], ty) - margin,
            max(pad_xy[0], tx) + margin,
            max(pad_xy[1], ty) + margin,
        )
        src = [p for p in src if window[0] <= p[0] <= window[2] and window[1] <= p[1] <= window[3]]
        if not src:
            return None, f"{what}: the pad has no copper on {opts['layers']}"
        steps = [opts["gridStep"]]
        if opts["refine"] and opts["gridStep"] / 2 >= 0.025:
            steps.append(opts["gridStep"] / 2)
        plan, reason = None, ""
        for step in steps:
            if time.time() > self.deadline:
                return None, f"{what}: time budget exhausted"
            o = dict(opts, gridStep=step)
            plan, st = rc._plan(ctx, m["net"], src, [(tx, ty, tl)], o, extra, window)
            if plan is not None:
                plan = _simplify(grid_tail_fix(plan.points, step), m["opts"]["width"])
                if not rc._check(ctx, m["net"], plan, opts, extra):
                    break
                reason = "the grid path fails the exact clearance check"
                plan = None
            else:
                reason = st.get("reason", "no route")
        if plan is None:
            return None, f"{what}: {reason}"
        if plan.length > max(4 * direct, direct + 6.0):
            return None, f"{what} detours ({plan.length:.1f} mm for {direct:.1f} mm)"
        return plan, ""
