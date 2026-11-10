"""Serpentine (meander) length tuning: ``tune_length``.

``length_match`` only reports lengths; this module adds the length. For each
net that is shorter than its target it replaces part of one or more straight
track segments of the net with a meander whose added length is exactly the
missing length (to floating-point precision; coordinates are then rounded to
1 nm when written).

Geometry (all in a segment-local frame: x along the segment from its start A,
y along the left normal, so a "side" is +1 or -1)::

    trombone (single-sided)            accordion (double-sided)
       _     _     _                      _       _
    __| |___| |___| |__               __| |   | |   |__
                                           |_|   |_|

A meander is a sequence of *tops*: a flat run of width ``spacing`` at height
``h`` on one side, joined to the baseline (or, in an accordion, straight to
the next top on the other side) by legs perpendicular to the segment. Every
90-degree corner is then cut by a 45-degree chamfer (default, KiCad style), an
arc (``corner: "round"``), or left square. A top adds ``2h`` minus the corner
savings; the added length grows monotonically with ``h``, so the common
amplitude that hits the target exactly is found by bisection.

Slots (the positions a top may occupy) are laid out along the usable part of
each segment (away from its ends). For every slot and side the largest
amplitude whose *filled* footprint rectangle keeps clearance to other-net
copper, keepouts, holes, the board edge and same-net copper (other than the
segment itself) is found by bisection: a filled rectangle is conservative,
so any lower top in that slot is clear too, which is what lets the amplitude
shrink next to obstacles. The finished meander is then checked once more
with the A* router's exact clearance check (``RouterCommands._check``) before
anything is written.

Segments are used longest first (or the caller's explicit list), the missing
length is spread across as many as needed, and every net's meanders are
written in one transaction: the new pieces are added, the replaced segments
detached, and on any error the board is put back as it was.

Differential pairs: only the shorter member is tuned, with small, tight bumps
(the partner is other-net copper, so the side facing it fails the clearance
test and bumps go to the outside).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from utils import copper_geometry as cg
from utils.board_items import delete_board_item, detach_board_item

logger = logging.getLogger("kicad_interface")

Point = Tuple[float, float]
# ("line", a, b) or ("arc", a, mid, b, centre, radius)
Prim = Tuple[Any, ...]

STYLES = ("trombone", "accordion")
CORNERS = ("chamfer", "round", "square")
EPS = 1e-9


def _fail(message: str, details: str = "", **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "message": message}
    if details:
        out["errorDetails"] = details
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Pure geometry (segment-local frame)
# ---------------------------------------------------------------------------


def meander_polyline(length: float, tops: Sequence[Tuple[float, float, int, float]]) -> List[Point]:
    """Square-cornered meander from (0, 0) to (length, 0).

    ``tops`` = [(x, width, side, h)] sorted by x, non-overlapping. A top that
    starts exactly where the previous one ends is joined to it directly (one
    leg from height to height, as in an accordion); otherwise the path
    returns to the baseline in between.
    """
    pts: List[Point] = [(0.0, 0.0)]
    ordered = sorted(tops, key=lambda t: t[0])
    for i, (x, s, side, h) in enumerate(ordered):
        y = side * h
        if abs(pts[-1][1]) < EPS:
            pts.append((x, 0.0))
        pts.append((x, y))
        pts.append((x + s, y))
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        if nxt is None or abs(nxt[0] - (x + s)) > 1e-7:
            pts.append((x + s, 0.0))
    pts.append((length, 0.0))
    return _clean(pts)


def _clean(pts: List[Point]) -> List[Point]:
    """Drop repeated points and collinear interior points (same direction)."""
    out: List[Point] = []
    for p in pts:
        if out and math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) < 1e-9:
            continue
        out.append(p)
    k = 1
    while k < len(out) - 1:
        a, b, c = out[k - 1], out[k], out[k + 1]
        d1 = (b[0] - a[0], b[1] - a[1])
        d2 = (c[0] - b[0], c[1] - b[1])
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        dot = d1[0] * d2[0] + d1[1] * d2[1]
        if abs(cross) < 1e-12 and dot > 0:
            del out[k]
        else:
            k += 1
    return out


def corner_primitives(pts: Sequence[Point], size: float, corner: str = "chamfer") -> List[Prim]:
    """Lines (and arcs) for a polyline with each corner cut back by up to ``size``.

    The cut-back at a corner is limited to half of each adjacent segment, so
    two corners never overlap. ``corner``: chamfer (45-degree line), round
    (tangent arc, 90-degree corners only; others are chamfered) or square.
    """
    prims: List[Prim] = []
    cur = pts[0]
    for i in range(1, len(pts) - 1):
        a, p, b = pts[i - 1], pts[i], pts[i + 1]
        lin = math.hypot(p[0] - a[0], p[1] - a[1])
        lout = math.hypot(b[0] - p[0], b[1] - p[1])
        c = 0.0 if corner == "square" else min(size, lin / 2, lout / 2)
        if c <= 1e-7:
            prims.append(("line", cur, p))
            cur = p
            continue
        uin = ((p[0] - a[0]) / lin, (p[1] - a[1]) / lin)
        uout = ((b[0] - p[0]) / lout, (b[1] - p[1]) / lout)
        q1 = (p[0] - c * uin[0], p[1] - c * uin[1])
        q2 = (p[0] + c * uout[0], p[1] + c * uout[1])
        prims.append(("line", cur, q1))
        if corner == "round" and abs(uin[0] * uout[0] + uin[1] * uout[1]) < 1e-9:
            cx = p[0] + c * (uout[0] - uin[0])
            cy = p[1] + c * (uout[1] - uin[1])
            dx, dy = p[0] - cx, p[1] - cy
            d = math.hypot(dx, dy)
            mid = (cx + c * dx / d, cy + c * dy / d)
            prims.append(("arc", q1, mid, q2, (cx, cy), c))
        else:
            prims.append(("line", q1, q2))
        cur = q2
    prims.append(("line", cur, pts[-1]))
    return [
        pr
        for pr in prims
        if pr[0] == "arc" or math.hypot(pr[2][0] - pr[1][0], pr[2][1] - pr[1][1]) > 1e-9
    ]


def prim_length(pr: Prim) -> float:
    if pr[0] == "line":
        return math.hypot(pr[2][0] - pr[1][0], pr[2][1] - pr[1][1])
    a, b, centre, r = pr[1], pr[3], pr[4], pr[5]
    a0 = math.atan2(a[1] - centre[1], a[0] - centre[0])
    a1 = math.atan2(b[1] - centre[1], b[0] - centre[0])
    sweep = abs((a1 - a0 + math.pi) % (2 * math.pi) - math.pi)
    return r * sweep


def path_length(prims: Sequence[Prim]) -> float:
    return sum(prim_length(p) for p in prims)


def meander(
    length: float,
    tops: Sequence[Tuple[float, float, int, float]],
    corner_size: float,
    corner: str,
) -> Tuple[List[Prim], float]:
    """Primitives of a meander on a segment of ``length`` and the length it adds."""
    prims = corner_primitives(meander_polyline(length, tops), corner_size, corner)
    return prims, path_length(prims) - length


def solve_amplitude(
    length: float,
    slots: Sequence[Tuple[float, float, int, float]],
    target: float,
    corner_size: float,
    corner: str,
    iterations: int = 60,
) -> Tuple[float, List[Tuple[float, float, int, float]], float]:
    """Common amplitude h* so the slots (each capped at its own max height) add ``target``.

    ``slots`` = [(x, width, side, h_max)]. Returns (h*, tops, added). If even
    the full heights add less than ``target`` they are returned as they are.
    """

    def tops_at(h: float):
        return [(x, s, side, min(h, hm)) for x, s, side, hm in slots]

    hi = max((hm for _, _, _, hm in slots), default=0.0)
    _, full = meander(length, tops_at(hi), corner_size, corner)
    if full <= target + 1e-12:
        return hi, tops_at(hi), full
    lo = 0.0
    for _ in range(iterations):
        mid = (lo + hi) / 2
        _, added = meander(length, tops_at(mid), corner_size, corner)
        if added < target:
            lo = mid
        else:
            hi = mid
    h = (lo + hi) / 2
    _, added = meander(length, tops_at(h), corner_size, corner)
    return h, tops_at(h), added


def to_world(prims: Sequence[Prim], a: Point, u: Point, n: Point) -> List[Prim]:
    def w(p: Point) -> Point:
        return (a[0] + p[0] * u[0] + p[1] * n[0], a[1] + p[0] * u[1] + p[1] * n[1])

    out: List[Prim] = []
    for pr in prims:
        if pr[0] == "line":
            out.append(("line", w(pr[1]), w(pr[2])))
        else:
            out.append(("arc", w(pr[1]), w(pr[2]), w(pr[3]), w(pr[4]), pr[5]))
    return out


def prim_chords(pr: Prim, pieces: int = 4) -> List[Tuple[Point, Point]]:
    """Straight pieces of a primitive (an arc as a short chord chain)."""
    if pr[0] == "line":
        return [(pr[1], pr[2])]
    a, b, centre, r = pr[1], pr[3], pr[4], pr[5]
    a0 = math.atan2(a[1] - centre[1], a[0] - centre[0])
    a1 = math.atan2(b[1] - centre[1], b[0] - centre[0])
    sweep = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    pts = [
        (
            centre[0] + r * math.cos(a0 + sweep * k / pieces),
            centre[1] + r * math.sin(a0 + sweep * k / pieces),
        )
        for k in range(pieces + 1)
    ]
    pts[0], pts[-1] = a, b
    return list(zip(pts, pts[1:]))


def region_distance(poly: Sequence[Point], s: cg.Shape) -> float:
    """Distance from a filled convex polygon to a shape's area (0 when they touch)."""
    g = s.geom
    if s.kind == "circle":
        rep = (g[0], g[1])
    elif s.kind == "rect":
        rep = ((g[0] + g[2]) / 2, (g[1] + g[3]) / 2)
    elif s.kind == "capsule":
        rep = (g[0], g[1])
    else:
        rep = g[0][0]
    if cg.point_in_polygon(rep[0], rep[1], poly):
        return 0.0
    n = len(poly)
    return min(s.segment_distance(poly[i], poly[(i + 1) % n]) for i in range(n))


# ---------------------------------------------------------------------------
# Board side
# ---------------------------------------------------------------------------


def net_lengths(board: Any, nets: Sequence[str]) -> Dict[str, float]:
    """Sum of routed track/arc lengths per net, mm (as length_match measures it)."""
    out = {n: 0.0 for n in nets}
    for t in board.GetTracks():
        n = t.GetNetname()
        if n in out and t.GetClass() != "PCB_VIA":
            out[n] += t.GetLength() / cg.NM
    return out


class _SegPlan:
    __slots__ = ("track", "uuid", "layer", "width", "a", "b", "prims", "added", "tops", "info")

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class LengthTuner:
    """tune_length: add serpentine meanders to reach a target length or pair skew."""

    def __init__(self, board: Any = None, board_provider: Any = None) -> None:
        self._board = board
        self._board_provider = board_provider

    @property
    def board(self) -> Any:
        return self._board_provider() if self._board_provider else self._board

    @board.setter
    def board(self, value: Any) -> None:
        self._board = value
        self._board_provider = None

    # ------------------------------------------------------------- requests
    def _jobs(self, params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """[(net, target, pair?)] from net/nets + targetLength/matchTo, and pairs."""
        board = self.board
        nets: List[str] = []
        if params.get("net"):
            nets.append(str(params["net"]))
        for n in params.get("nets") or []:
            if n not in nets:
                nets.append(str(n))
        pairs = [list(p) for p in (params.get("pairs") or [])]
        for p in pairs:
            if len(p) != 2:
                raise ValueError("each pair must be [positiveNet, negativeNet]")
        match = params.get("matchTo")
        match_nets = [match] if isinstance(match, str) else list(match or [])
        every = nets + [n for p in pairs for n in p] + match_nets
        if not nets and not pairs:
            raise ValueError("give net (or nets) and/or pairs")
        missing = sorted({n for n in every if board.FindNet(n) is None})
        if missing:
            raise ValueError(f"nets not found: {missing}")
        lengths = net_lengths(board, list(dict.fromkeys(every)))
        jobs: List[Dict[str, Any]] = []
        if nets:
            target = params.get("targetLength")
            if target is not None and match_nets:
                raise ValueError("give targetLength or matchTo, not both")
            if target is not None:
                target = float(target)
                source = "targetLength"
            elif match_nets:
                ref = max(match_nets, key=lambda n: lengths[n])
                target = lengths[ref]
                source = f"matchTo {ref}"
            elif len(nets) > 1:
                ref = max(nets, key=lambda n: lengths[n])
                target = lengths[ref]
                source = f"longest of nets ({ref})"
            else:
                raise ValueError("give targetLength or matchTo (a net or a list of nets)")
            for n in nets:
                jobs.append({"net": n, "target": target, "source": source, "pair": None})
        tol = float(params.get("tolerance", 0.1))
        ptol = float(params.get("pairTolerance", tol))
        for p, n in pairs:
            skew = lengths[p] - lengths[n]
            short, long_ = (n, p) if skew > 0 else (p, n)
            if any(j["net"] == short for j in jobs):
                raise ValueError(f"{short} is both a net to tune and a pair member")
            jobs.append(
                {
                    "net": short,
                    "target": lengths[long_],
                    "source": f"pair skew vs {long_}",
                    "pair": [p, n],
                    "tolerance": ptol,
                }
            )
        return jobs, lengths

    # ----------------------------------------------------------- planning
    def _shape_opts(
        self, ctx: Dict[str, Any], net: str, width: float, params: Dict[str, Any], pair: bool
    ):
        rules: cg.Rules = ctx["rules"]
        clr = params.get("clearance")
        clr = float(clr) if clr is not None else None
        same = rules.clearance(net, net)
        other = clr if clr is not None else rules.for_net(net)["clearance"]
        min_pitch = width + max(other, same)
        if params.get("spacing") is not None:
            pitch = float(params["spacing"])
            if pitch < 2 * width - 1e-9:
                raise ValueError(f"spacing {pitch} mm is below twice the track width ({width} mm)")
        elif pair:
            pitch = max(2 * width, min_pitch)
        else:
            pitch = max(3 * width, min_pitch)
        if params.get("amplitude") is not None:
            amp = float(params["amplitude"])
            if amp <= 0:
                raise ValueError("amplitude must be > 0")
        elif pair:
            amp = max(2 * width, 0.3)
        else:
            amp = 1.0
        corner = params.get("corner") or "chamfer"
        style = params.get("style") or "trombone"
        if style == "rounded":  # alias: trombone with arc corners
            style, corner = "trombone", "round"
        if style not in STYLES:
            raise ValueError(f"style must be one of {STYLES} (or 'rounded'), got {style!r}")
        if corner not in CORNERS:
            raise ValueError(f"corner must be one of {CORNERS}, got {corner!r}")
        csize = pitch / 2 if corner == "round" else pitch / 3
        return {
            "pitch": pitch,
            "amplitude": amp,
            "minAmplitude": min(amp, width / 2),
            "style": style,
            "corner": corner,
            "cornerSize": csize,
            "clearance": clr,
            "lead": max(pitch, width + max(other, same)) + csize,
        }

    def _need(self, ctx: Dict[str, Any], net: str, s: cg.Shape, clr: Optional[float]) -> float:
        rules: cg.Rules = ctx["rules"]
        if s.role == "keepout":
            return 0.0
        if s.role == "hole":
            return max(rules.hole_clearance, clr or 0.0)
        if s.extra_clearance:
            return s.extra_clearance
        if s.net == net:
            return rules.clearance(net, net)
        return clr if clr is not None else rules.clearance(net, s.net)

    def _plan_segment(
        self,
        ctx: Dict[str, Any],
        net: str,
        track: Any,
        want: float,
        so: Dict[str, Any],
        obstacles: cg.SpatialIndex,
        edges: cg.SpatialIndex,
        region: Optional[List[Point]],
        tol: float,
    ) -> Tuple[Optional[_SegPlan], str]:
        board = self.board
        sx, sy = track.GetStart().x, track.GetStart().y
        ex, ey = track.GetEnd().x, track.GetEnd().y
        a = (sx / cg.NM, sy / cg.NM)
        b = (ex / cg.NM, ey / cg.NM)
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        width = track.GetWidth() / cg.NM
        layer = board.GetLayerName(track.GetLayer())
        r = width / 2
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        nrm = (-u[1], u[0])
        s = so["pitch"]
        cs = so["cornerSize"]
        lead = so["lead"]
        usable = L - 2 * lead
        if usable < s:
            return None, f"too short for a meander ({L:.3f} mm; needs {2 * lead + s:.3f} mm)"
        trombone = so["style"] == "trombone"
        step = 2 * s if trombone else s
        count = int((usable - s) // step) + 1
        span = (count - 1) * step + s
        x0 = lead + (usable - span) / 2
        xs = [x0 + k * step for k in range(count)]
        clr = so["clearance"]

        def world(px: float, py: float) -> Point:
            return (a[0] + px * u[0] + py * nrm[0], a[1] + px * u[1] + py * nrm[1])

        def is_self(sh: cg.Shape) -> bool:
            if sh.role != "track" or sh.net != net or sh.kind != "capsule":
                return False
            g = sh.geom
            p, q = (g[0], g[1]), (g[2], g[3])
            close = lambda m, n: abs(m[0] - n[0]) < 1e-6 and abs(m[1] - n[1]) < 1e-6  # noqa: E731
            return (close(p, a) and close(q, b)) or (close(p, b) and close(q, a))

        def clear(x: float, side: int, h: float) -> bool:
            poly = [
                world(x - cs, 0.0),
                world(x + s + cs, 0.0),
                world(x + s + cs, side * h),
                world(x - cs, side * h),
            ]
            xsP = [p[0] for p in poly]
            ysP = [p[1] for p in poly]
            box = (min(xsP) - 3, min(ysP) - 3, max(xsP) + 3, max(ysP) + 3)
            for sh in obstacles.query(*box):
                if layer not in sh.layers:
                    continue
                if sh.role == "keepout" and not sh.meta.get("noTracks"):
                    continue
                if is_self(sh):
                    continue
                if region_distance(poly, sh) - r < self._need(ctx, net, sh, clr) - 1e-4:
                    return False
            rules: cg.Rules = ctx["rules"]
            for e in edges.query(*box):
                if region_distance(poly, e) - r < rules.edge_clearance - 1e-4:
                    return False
            if region is not None:
                for p in poly[:2]:
                    if not cg.point_in_polygon(p[0], p[1], region):
                        return False
            return True

        hmax, hmin = so["amplitude"], so["minAmplitude"]

        def best_height(x: float, side: int) -> float:
            if clear(x, side, hmax):
                return hmax
            if not clear(x, side, hmin):
                return 0.0
            lo, hi = hmin, hmax
            for _ in range(10):
                mid = (lo + hi) / 2
                if clear(x, side, mid):
                    lo = mid
                else:
                    hi = mid
            return lo

        heights = {side: [best_height(x, side) for x in xs] for side in (1, -1)}
        if trombone:
            options = [
                [(x, s, side, heights[side][k]) for k, x in enumerate(xs)] for side in (1, -1)
            ]
        else:
            options = []
            for phase in (1, -1):
                row = []
                for k, x in enumerate(xs):
                    side = phase if k % 2 == 0 else -phase
                    row.append((x, s, side, heights[side][k]))
                options.append(row)
        options.sort(key=lambda row: -sum(t[3] for t in row))
        slots = [t for t in options[0] if t[3] >= hmin - 1e-9]
        if not slots:
            return None, "no room for a meander on either side (clearance)"
        centre = L / 2
        slots.sort(key=lambda t: (-round(t[3], 6), abs(t[0] + s / 2 - centre)))
        chosen: List[Tuple[float, float, int, float]] = []
        added = 0.0
        for t in slots:
            chosen.append(t)
            _, added = meander(L, chosen, cs, so["corner"])
            if added >= want - 1e-12:
                break
        h, tops, added = solve_amplitude(L, sorted(chosen), want, cs, so["corner"])
        if h < hmin - 1e-9:
            # a tiny remainder: one bump at the smallest sensible amplitude, if
            # the overshoot stays within tolerance
            tops = [(x, w_, side, min(hmin, hm)) for x, w_, side, hm in sorted(chosen)[:1]]
            _, added = meander(L, tops, cs, so["corner"])
            if added - want > tol:
                return None, (
                    f"the remaining {want:.3f} mm is less than the smallest meander "
                    f"({added:.3f} mm) and the overshoot exceeds the tolerance"
                )
            h = tops[0][3]
        prims_local, added = meander(L, tops, cs, so["corner"])
        prims = to_world(prims_local, a, u, nrm)
        # snap the two ends to the original endpoints exactly
        first, last = prims[0], prims[-1]
        prims[0] = (first[0], a) + tuple(first[2:])
        prims[-1] = tuple(last[:-1]) + (b,) if last[0] == "line" else last
        sides = sorted({t[2] for t in tops})
        info = {
            "uuid": track.m_Uuid.AsString(),
            "layer": layer,
            "widthMm": round(width, 4),
            "start": [round(a[0], 4), round(a[1], 4)],
            "end": [round(b[0], 4), round(b[1], 4)],
            "segmentLengthMm": round(L, 3),
            "meanders": len(tops),
            "addedMm": round(added, 4),
            "amplitudeMm": round(max(t[3] for t in tops), 4),
            "minAmplitudeMm": round(min(t[3] for t in tops), 4),
            "side": "both" if len(sides) > 1 else ("left" if sides[0] > 0 else "right"),
            "pieces": len(prims),
        }
        return (
            _SegPlan(
                track=track,
                uuid=info["uuid"],
                layer=layer,
                width=width,
                a=a,
                b=b,
                prims=prims,
                added=added,
                tops=tops,
                info=info,
            ),
            "",
        )

    def _verify(
        self,
        ctx: Dict[str, Any],
        router: Any,
        net: str,
        sp: _SegPlan,
        so: Dict[str, Any],
        extra_other: List[cg.Shape],
        same_index: cg.SpatialIndex,
    ) -> List[Dict[str, Any]]:
        """Exact clearance of the finished meander (router check + same-net spacing)."""
        from commands.astar_router import _Plan, _viol

        plan = _Plan()
        for pr in sp.prims:
            for p, q in prim_chords(pr):
                plan.tracks.append((sp.layer, p, q, sp.width))
        opts = {"width": sp.width, "viaSize": 0.0, "clearance": so["clearance"]}
        bad = router._check(ctx, net, plan, opts, extra_other)
        # same-net copper: only the parts that leave the original centre line
        a, b = sp.a, sp.b
        r = sp.width / 2
        need = ctx["rules"].clearance(net, net)
        for layer, p, q, w in plan.tracks:
            on_line = (
                cg.seg_point_dist(p[0], p[1], a[0], a[1], b[0], b[1]) < 1e-6
                and cg.seg_point_dist(q[0], q[1], a[0], a[1], b[0], b[1]) < 1e-6
            )
            if on_line:
                continue
            box = (
                min(p[0], q[0]) - 3,
                min(p[1], q[1]) - 3,
                max(p[0], q[0]) + 3,
                max(p[1], q[1]) + 3,
            )
            for s in same_index.query(*box):
                if layer not in s.layers:
                    continue
                if s.role == "track" and s.kind == "capsule":
                    g = s.geom
                    if (
                        cg.seg_point_dist(g[0], g[1], a[0], a[1], b[0], b[1]) < 1e-6
                        and cg.seg_point_dist(g[2], g[3], a[0], a[1], b[0], b[1]) < 1e-6
                    ):
                        continue  # the segment being replaced (or a piece of it)
                gap = s.segment_distance(p, q) - r
                if gap < need - 1e-4:
                    bad.append(_viol("track", layer, p, q, s, gap, need))
        return bad

    def _plan_net(
        self,
        ctx: Dict[str, Any],
        router: Any,
        job: Dict[str, Any],
        length: float,
        params: Dict[str, Any],
        extra: List[cg.Shape],
        region: Optional[List[Point]],
    ) -> Dict[str, Any]:
        board = self.board
        net = job["net"]
        tol = float(job.get("tolerance", params.get("tolerance", 0.1)))
        target = job["target"]
        delta = target - length
        row: Dict[str, Any] = {
            "net": net,
            "targetMm": round(target, 3),
            "targetSource": job["source"],
            "beforeMm": round(length, 3),
            "toleranceMm": tol,
            "segments": [],
            "skipped": [],
            "plans": [],
        }
        if job.get("pair"):
            row["pair"] = job["pair"]
        if delta <= tol:
            row.update(
                {
                    "status": "withinTolerance" if delta >= -tol else "tooLong",
                    "plannedMm": round(length, 3),
                    "addedMm": 0.0,
                    "remainingMm": round(delta, 3),
                    "meanders": 0,
                }
            )
            if delta < -tol:
                row["note"] = (
                    f"{net} is already {-delta:.3f} mm longer than the target; tuning only adds "
                    "length. Tune the other nets up to this one instead (matchTo)."
                )
            return row
        layers = params.get("layers")
        cu = ctx["cu"]
        if layers:
            unknown = [L for L in layers if L not in cu]
            if unknown:
                raise ValueError(f"unknown copper layers {unknown}; board has {cu}")
        explicit = [str(u) for u in (params.get("segments") or [])]
        tracks = [
            t
            for t in board.GetTracks()
            if t.GetNetname() == net and t.GetClass() in ("PCB_TRACK", "PCB_ARC")
        ]
        by_uuid = {t.m_Uuid.AsString(): t for t in tracks}
        if explicit:
            unknown = [u for u in explicit if u not in by_uuid]
            if unknown:
                raise ValueError(f"segments not found on {net}: {unknown}")
            cands = [by_uuid[u] for u in explicit]
        else:
            cands = sorted(tracks, key=lambda t: -t.GetLength())
        allow_locked = bool(params.get("allowLocked", False))
        rules: cg.Rules = ctx["rules"]
        # obstacles: everything except this net's tracks/pads is checked at
        # the rules' clearance; this net's own copper at its own clearance
        # (so a meander never runs into, and shortcuts to, the rest of the net)
        obstacles = cg.SpatialIndex(list(ctx["shapes"]) + list(ctx["keepouts"]) + list(extra))
        edges = cg.SpatialIndex(cg.outline_edges(ctx["outlines"]))
        same_index = cg.SpatialIndex(
            [s for s in ctx["shapes"] if s.net == net and s.role != "hole"]
            + [s for s in extra if s.net == net]
        )
        extra_other = [s for s in extra if s.net != net]
        remaining = delta
        examined = 0
        for t in cands:
            if remaining <= tol:
                break
            uid = t.m_Uuid.AsString()
            layer = board.GetLayerName(t.GetLayer())
            reason = ""
            if t.GetClass() == "PCB_ARC":
                reason = "arc segment (only straight segments are tuned)"
            elif layers and layer not in layers:
                reason = f"layer {layer} not in layers"
            elif t.IsLocked() and not allow_locked:
                reason = "locked (pass allowLocked to tune it)"
            elif t.GetLength() <= 0:
                reason = "zero length"
            if reason:
                if explicit or t.GetClass() != "PCB_ARC":
                    row["skipped"].append({"uuid": uid, "layer": layer, "reason": reason})
                continue
            examined += 1
            width = t.GetWidth() / cg.NM
            so = self._shape_opts(ctx, net, width, params, bool(job.get("pair")))
            if (
                region is None
                and not explicit
                and t.GetLength() / cg.NM < 2 * so["lead"] + so["pitch"]
            ):
                # the remaining candidates are all shorter: stop listing them
                row["skipped"].append(
                    {
                        "uuid": uid,
                        "layer": layer,
                        "reason": "too short for a meander (this and all shorter segments)",
                    }
                )
                break
            sp, why = self._plan_segment(ctx, net, t, remaining, so, obstacles, edges, region, tol)
            if sp is None:
                row["skipped"].append({"uuid": uid, "layer": layer, "reason": why})
                continue
            bad = self._verify(ctx, router, net, sp, so, extra_other, same_index)
            if bad:
                row["skipped"].append(
                    {
                        "uuid": uid,
                        "layer": layer,
                        "reason": "the finished meander fails the exact clearance check",
                        "violations": bad[:3],
                    }
                )
                continue
            sp.info["style"] = so["style"]
            sp.info["corner"] = so["corner"]
            sp.info["spacingMm"] = round(so["pitch"], 4)
            row["plans"].append(sp)
            row["segments"].append(sp.info)
            remaining -= sp.added
            for pr in sp.prims:
                for p, q in prim_chords(pr):
                    s = cg.Shape(
                        "capsule", (p[0], p[1], q[0], q[1], sp.width / 2), [layer], net, "track"
                    )
                    extra.append(s)
                    obstacles.add(s)
                    same_index.add(s)
        added = delta - remaining
        if len(row["skipped"]) > 25:
            row["skippedCount"] = len(row["skipped"])
            row["skipped"] = row["skipped"][:25]
        row.update(
            {
                "plannedMm": round(length + added, 3),
                "addedMm": round(added, 3),
                "remainingMm": round(remaining, 3),
                "meanders": sum(sp.info["meanders"] for sp in row["plans"]),
                "status": (
                    "tuned"
                    if abs(remaining) <= tol
                    else ("partial" if row["plans"] else "notTuned")
                ),
            }
        )
        if row["status"] != "tuned":
            row["note"] = (
                "Not enough room on the examined segments: try a larger amplitude, the "
                "accordion style, a smaller spacing, other segments, or reroute with more length."
            )
        return row

    # -------------------------------------------------------------- writing
    def _write(self, plans: List[Tuple[str, _SegPlan]], locked: bool) -> int:
        import pcbnew

        board = self.board

        def v(p: Point):
            return pcbnew.VECTOR2I(int(round(p[0] * cg.NM)), int(round(p[1] * cg.NM)))

        added: List[Any] = []
        detached: List[Any] = []
        try:
            for net, sp in plans:
                orig = sp.track
                netinfo = orig.GetNet()
                lid = orig.GetLayer()
                wid = orig.GetWidth()
                for pr in sp.prims:
                    if pr[0] == "line":
                        t = pcbnew.PCB_TRACK(board)
                        t.SetStart(v(pr[1]))
                        t.SetEnd(v(pr[2]))
                    else:
                        t = pcbnew.PCB_ARC(board)
                        t.SetStart(v(pr[1]))
                        t.SetMid(v(pr[2]))
                        t.SetEnd(v(pr[3]))
                    t.SetWidth(wid)
                    t.SetLayer(lid)
                    t.SetNet(netinfo)
                    t.SetLocked(locked)
                    board.Add(t)
                    added.append(t)
            for _, sp in plans:
                detached.append(detach_board_item(board, sp.track))
        except Exception:
            for item in added:
                try:
                    delete_board_item(board, item)
                except Exception:
                    pass
            for item in detached:
                try:
                    board.Add(item)
                except Exception:
                    logger.exception("tune_length rollback could not restore a segment")
            raise
        for item in detached:
            board.Add(item)
            delete_board_item(board, item)
        return len(added)

    # ----------------------------------------------------------------- tool
    def tune_length(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add serpentine meanders so nets reach a target length / pairs lose their skew."""
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        from commands.astar_router import RouterCommands

        t0 = time.time()
        dry = bool(params.get("dryRun", False))
        locked = bool(params.get("locked", False))
        router = RouterCommands(board_provider=lambda: self.board)
        try:
            jobs, lengths = self._jobs(params)
            region = None
            if params.get("region"):
                from commands.autoroute_pipeline import normalize_region

                region = normalize_region(params["region"])
            ctx = router._context()
            extra: List[cg.Shape] = []
            rows = [
                self._plan_net(ctx, router, job, lengths[job["net"]], params, extra, region)
                for job in jobs
            ]
        except ValueError as exc:
            return _fail("Invalid tune_length request", str(exc))
        except Exception as exc:
            logger.exception("tune_length failed")
            return _fail("tune_length failed", f"{type(exc).__name__}: {exc}")
        plans = [(r["net"], sp) for r in rows for sp in r.pop("plans")]
        pieces = 0
        if plans and not dry:
            try:
                pieces = self._write(plans, locked)
            except Exception as exc:
                logger.exception("tune_length write failed")
                return _fail("Writing the meanders failed; the board is unchanged", str(exc))
            after = net_lengths(self.board, [r["net"] for r in rows])
            for r in rows:
                r["afterMm"] = round(after[r["net"]], 3)
                r["remainingMm"] = round(r["targetMm"] - after[r["net"]], 3)
        meanders = sum(r["meanders"] for r in rows)
        tuned_ok = all(r["status"] in ("tuned", "withinTolerance") for r in rows)
        out: Dict[str, Any] = {
            "success": True,
            "dryRun": dry,
            "written": bool(plans) and not dry,
            "allWithinTolerance": tuned_ok,
            "nets": rows,
            "segmentsReplaced": len(plans),
            "meanders": meanders,
            "piecesWritten": pieces,
            "locked": locked,
            "seconds": round(time.time() - t0, 3),
            "message": (
                f"{'Planned' if dry else 'Placed'} {meanders} meander(s) on {len(plans)} "
                f"segment(s) of {len(rows)} net(s); "
                + ("all within tolerance" if tuned_ok else "some nets did not reach the target")
            ),
            "note": (
                "Clearance uses the project's net-class clearances (custom .kicad_dru rules "
                "are not evaluated) and ignores zone fills (refill zones after tuning); run DRC."
            ),
        }
        if not plans and not dry:
            out["_skipAutoSave"] = True
        return out
