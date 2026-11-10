"""Grid A* router for single connections, buses and length reports (MCP_ISSUES R3, R5).

``route_connection`` routes one connection of a net on a grid (default
0.1 mm) with 8 directions (45-degree moves) across the allowed copper layers,
changing layers through vias. Obstacles are every other-net pad / track / via
(inflated by the clearance from the project's net classes plus half the track
width, or the via radius on all copper layers for via sites), rule areas that
forbid tracks or vias, the board edge (copper-to-edge clearance) and caller
``avoidRegions``. Same-net copper is free, so the path may join the existing
copper of either side anywhere: start and goal are the anchor points (pad
centres, via centres, track ends) of the copper already connected to each end.

The search runs in a window around the two ends (``searchMargin``) and falls
back to the whole board. The final path is re-checked with exact geometry
(not the grid) before anything is written; a path that would violate
clearance is refused. Nothing is written with ``dryRun``; on any failure the
board is unchanged.

Ported and generalised from the CD_Mainboard ``route_one_net.py`` prototype.
"""

from __future__ import annotations

import heapq
import logging
from array import array
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - KiCad builds without numpy
    np = None  # type: ignore[assignment]

from utils import copper_geometry as cg
from utils.board_items import delete_board_item

logger = logging.getLogger("kicad_interface")

# Circular order: index difference = number of 45-degree turns.
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
TURN_COST = [0.0, 0.35, 1.2, 8.0, 20.0]
MAX_EXPANSIONS = 4_000_000


def _fail(message: str, details: str = "", **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "message": message}
    if details:
        out["errorDetails"] = details
    out.update(extra)
    return out


class _Plan:
    """A planned route (not yet on the board)."""

    def __init__(self) -> None:
        self.points: List[Tuple[float, float, str]] = []
        self.tracks: List[Tuple[str, Tuple[float, float], Tuple[float, float], float]] = []
        self.vias: List[Tuple[float, float]] = []
        self.length = 0.0


class RouterCommands:
    """route_connection / route_bus / length_match."""

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

    # ------------------------------------------------------------------ setup
    def _context(self) -> Dict[str, Any]:
        import pcbnew

        if np is None:
            raise ValueError("route_connection/route_bus need numpy in KiCad's Python")

        board = self.board
        board.BuildConnectivity()
        shapes, cu_names = cg.collect_copper(board, pcbnew)
        return {
            "pcbnew": pcbnew,
            "shapes": shapes,
            "keepouts": cg.keepout_shapes(board, pcbnew, cu_names),
            "cu": list(cu_names.values()),
            "cu_ids": {name: lid for lid, name in cu_names.items()},
            "outlines": cg.board_outlines(board, pcbnew),
            "rules": cg.Rules(board),
        }

    def _endpoint_group(
        self, ctx: Dict[str, Any], ep: Dict[str, Any], net: str, layers: Sequence[str]
    ):
        """Anchor points (x, y, layer) and item ids of the copper connected to an endpoint."""
        pcbnew = ctx["pcbnew"]
        if ep["kind"] == "point":
            lay = [ep["layer"]] if ep.get("layer") else list(layers)
            return [(ep["x"], ep["y"], L) for L in lay if L in layers], set()
        pad = ep["pad"]
        if pad.GetNetname() != net:
            raise ValueError(f"{ep['label']} is on net '{pad.GetNetname()}', not '{net}'")
        items = [pad]
        try:
            items += list(self.board.GetConnectivity().GetConnectedItems(pad))
        except Exception:
            pass
        anchors: List[Tuple[float, float, str]] = []
        ids: Set[str] = set()
        cu_ids = ctx["cu_ids"]
        for it in items:
            try:
                cls = it.GetClass()
                if it.GetNetname() != net or cls == "ZONE":
                    continue
                ids.add(it.m_Uuid.AsString())
                if cls == "PAD":
                    p = it.GetPosition()
                    for L in layers:
                        if it.IsOnLayer(cu_ids[L]):
                            anchors.append((cg.mm(p.x), cg.mm(p.y), L))
                elif cls == "PCB_VIA":
                    p = it.GetPosition()
                    for L in layers:
                        if it.IsOnLayer(cu_ids[L]):
                            anchors.append((cg.mm(p.x), cg.mm(p.y), L))
                elif cls in ("PCB_TRACK", "PCB_ARC"):
                    L = self.board.GetLayerName(it.GetLayer())
                    if L in layers:
                        for q in (it.GetStart(), it.GetEnd()):
                            anchors.append((cg.mm(q.x), cg.mm(q.y), L))
            except Exception:
                continue
        return anchors, ids

    def _missing_connection(self, net: str) -> Optional[Tuple[Any, Any, float]]:
        """The closest pair of pads of ``net`` that KiCad does not see as connected."""
        conn = self.board.GetConnectivity()
        pads = [p for fp in self.board.GetFootprints() for p in fp.Pads() if p.GetNetname() == net]
        cluster: Dict[str, int] = {}
        clusters: List[List[Any]] = []
        for p in pads:
            uid = p.m_Uuid.AsString()
            if uid in cluster:
                continue
            members = [p]
            try:
                members += [q for q in conn.GetConnectedItems(p) if q.GetClass() == "PAD"]
            except Exception:
                pass
            cid = len(clusters)
            clusters.append([])
            for q in members:
                qid = q.m_Uuid.AsString()
                if qid not in cluster and q.GetNetname() == net:
                    cluster[qid] = cid
                    clusters[cid].append(q)
        if len(clusters) < 2:
            return None
        best = None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                for p in clusters[a]:
                    pp = p.GetPosition()
                    for q in clusters[b]:
                        qp = q.GetPosition()
                        d = math.hypot(pp.x - qp.x, pp.y - qp.y) / cg.NM
                        if best is None or d < best[2]:
                            best = (p, q, d)
        return best

    # ------------------------------------------------------------------ core
    def _plan(
        self,
        ctx: Dict[str, Any],
        net: str,
        src: List[Tuple[float, float, str]],
        dst: List[Tuple[float, float, str]],
        opts: Dict[str, Any],
        extra: Sequence[cg.Shape] = (),
        window: Optional[Tuple[float, float, float, float]] = None,
    ) -> Tuple[Optional[_Plan], Dict[str, Any]]:
        res = float(opts["gridStep"])
        layers: List[str] = opts["layers"]
        nl = len(layers)
        hw = opts["width"] / 2
        vr = opts["viaSize"] / 2
        rules: cg.Rules = ctx["rules"]
        margin = 0.15 * res + 0.002
        t0 = time.time()

        if window is None:
            xs = [x for poly in ctx["outlines"] for x, _ in poly]
            ys = [y for poly in ctx["outlines"] for _, y in poly]
            window = (min(xs), min(ys), max(xs), max(ys))
        x0, y0, x1, y1 = window
        # align the grid to absolute multiples of the step so pad rows on a
        # 0.05/0.1 mm grid fall on grid lines (fine-pitch pins stay reachable)
        x0, y0 = math.floor(x0 / res) * res, math.floor(y0 / res) * res
        nx = int(math.ceil((x1 - x0) / res)) + 1
        ny = int(math.ceil((y1 - y0) / res)) + 1
        gxs = x0 + np.arange(nx) * res
        gys = y0 + np.arange(ny) * res
        trk = np.zeros((nl, nx, ny), dtype=bool)
        via = np.zeros((nx, ny), dtype=bool)

        def mark(grid: np.ndarray, shape: cg.Shape, r: float) -> None:
            bx0, by0, bx1, by1 = shape.bbox
            i0 = max(0, int((bx0 - r - x0) / res))
            i1 = min(nx - 1, int((bx1 + r - x0) / res) + 1)
            j0 = max(0, int((by0 - r - y0) / res))
            j1 = min(ny - 1, int((by1 + r - y0) / res) + 1)
            if i0 > i1 or j0 > j1:
                return
            gx, gy = np.meshgrid(gxs[i0 : i1 + 1], gys[j0 : j1 + 1], indexing="ij")
            grid[i0 : i1 + 1, j0 : j1 + 1] |= shape.distance_grid(gx, gy) < r

        clr_override = opts.get("clearance")
        lindex = {L: k for k, L in enumerate(layers)}
        obstacles = [s for s in ctx["shapes"] if s.net != net or s.role == "hole"]
        obstacles += list(extra)
        for s in obstacles:
            bx0, by0, bx1, by1 = s.bbox
            if bx1 < x0 - 2 or bx0 > x1 + 2 or by1 < y0 - 2 or by0 > y1 + 2:
                continue
            if s.role == "hole":
                clr = max(rules.hole_clearance, clr_override or 0.0)
            elif s.extra_clearance:
                clr = s.extra_clearance
            else:
                clr = clr_override if clr_override is not None else rules.clearance(net, s.net)
            for L in s.layers:
                k = lindex.get(L)
                if k is not None:
                    mark(trk[k], s, clr + hw + margin)
            if s.layers:
                mark(via, s, clr + vr + margin)
        for s in ctx["keepouts"]:
            if s.meta.get("noTracks"):
                for L in s.layers:
                    k = lindex.get(L)
                    if k is not None:
                        mark(trk[k], s, hw + margin)
            if s.meta.get("noVias") and s.layers:
                mark(via, s, vr + margin)
        for poly, layer_names in opts.get("avoid") or []:
            reg = cg.Shape("poly", (tuple(poly),), layer_names or layers, "", "keepout")
            for L in reg.layers:
                k = lindex.get(L)
                if k is not None:
                    mark(trk[k], reg, hw + margin)
            mark(via, reg, vr + margin)
        # board edge: outside the outline, or too close to an edge
        gx, gy = np.meshgrid(gxs, gys, indexing="ij")
        inside = np.zeros((nx, ny), dtype=bool)
        for poly in ctx["outlines"]:
            n = len(poly)
            ring = np.zeros((nx, ny), dtype=bool)
            for i in range(n):
                ax, ay = poly[i]
                bx, by = poly[(i + 1) % n]
                if ay == by:
                    continue
                ring ^= ((ay > gy) != (by > gy)) & (gx < ax + (gy - ay) * (bx - ax) / (by - ay))
            inside ^= ring
        outside = ~inside
        trk |= outside[None, :, :]
        via |= outside
        for e in cg.outline_edges(ctx["outlines"]):
            for k in range(nl):
                mark(trk[k], e, rules.edge_clearance + hw + margin)
            mark(via, e, rules.edge_clearance + vr + margin)
        t_raster = time.time() - t0

        def cell(x: float, y: float) -> Optional[Tuple[int, int]]:
            i, j = int(round((x - x0) / res)), int(round((y - y0) / res))
            if 0 <= i < nx and 0 <= j < ny:
                return i, j
            return None

        starts: Dict[Tuple[int, int, int], Tuple[float, float]] = {}
        goals: Dict[Tuple[int, int, int], Tuple[float, float]] = {}
        for pts, target in ((src, starts), (dst, goals)):
            for x, y, L in pts:
                c = cell(x, y)
                if c is None or L not in lindex:
                    continue
                k = lindex[L]
                target[(c[0], c[1], k)] = (x, y)
                trk[k, c[0], c[1]] = False  # the anchor's own cell is always enterable
        if not starts or not goals:
            return None, {
                "reason": "an endpoint lies outside the search window",
                "raster": t_raster,
            }

        # heuristic: distance (cells) to the nearest goal anchor, subsampled
        goal_cells = list({(i, j) for i, j, _ in goals})
        if len(goal_cells) > 64:
            sx = np.mean([i for i, _, _ in starts])
            sy = np.mean([j for _, j, _ in starts])
            goal_cells.sort(key=lambda c: (c[0] - sx) ** 2 + (c[1] - sy) ** 2)
            goal_cells = goal_cells[:64]
        ii, jj = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
        hmap = np.full((nx, ny), np.inf)
        for gi, gj in goal_cells:
            adx, ady = np.abs(ii - gi), np.abs(jj - gj)
            hmap = np.minimum(
                hmap, np.maximum(adx, ady) + (math.sqrt(2.0) - 1.0) * np.minimum(adx, ady)
            )
        min_cost = min(opts["layerCost"].values()) if opts.get("layerCost") else 1.0
        h = (hmap.ravel() * min_cost).tolist()
        # the window border is blocked, so moves never need bounds checks
        border = np.zeros((nx, ny), dtype=bool)
        border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
        for k in range(nl):
            trk[k] |= border
        via |= border
        border_b = border.ravel().tobytes()
        n_cells = nx * ny
        blocked = trk.reshape(-1).tobytes()  # index k * n_cells + idx
        vblocked = via.ravel().tobytes()
        lcost = [opts.get("layerCost", {}).get(L, 1.0) for L in layers]
        via_cost = opts["viaCost"] / res
        moves = []
        for d, (di, dj) in enumerate(DIRS):
            diag = bool(di and dj)
            moves.append((d, di * ny + dj, di * ny if diag else 0, dj if diag else 0, diag))
        sq2 = math.sqrt(2.0)
        turn_tab = [
            [TURN_COST[min(abs(a - b), 8 - abs(a - b))] for b in range(8)] for a in range(8)
        ]
        turn_tab.append([0.0] * 8)  # arrival direction 8 = start / after a via

        goal_set = {k * n_cells + i * ny + j for i, j, k in goals}
        # State = cell on a layer (s = k * n_cells + idx). The arrival direction
        # of the best path to a state is kept alongside so turns can be charged;
        # not exactly optimal with turn costs, but 8x smaller than a
        # direction-aware search, and complete (every move stays allowed).
        weight = float(opts.get("heuristicWeight", 1.25))
        total = nl * n_cells
        inf = float("inf")
        best = array("d", [inf]) * total
        arrive = bytearray([8]) * total
        closed = bytearray(total)
        came: Dict[int, int] = {}
        pq: List[Tuple[float, float, int]] = []
        for i, j, k in starts:
            st = k * n_cells + i * ny + j
            best[st] = 0.0
            heapq.heappush(pq, (h[i * ny + j] * weight, 0.0, st))
        expanded = 0
        goal_state = None
        touched_edge = False
        t1 = time.time()
        heappush, heappop = heapq.heappush, heapq.heappop
        while pq:
            f, ng_neg, st = heappop(pq)
            if closed[st]:
                continue
            closed[st] = 1
            expanded += 1
            if st in goal_set:
                goal_state = st
                break
            if expanded > MAX_EXPANSIONS:
                break
            k, idx = divmod(st, n_cells)
            base = k * n_cells
            lc = lcost[k]
            g = -ng_neg
            tt = turn_tab[arrive[st]]
            for nd, step, ci, cj, diag in moves:
                ns = st + step
                if blocked[ns]:
                    if border_b[idx + step]:
                        touched_edge = True
                    continue
                if diag and (blocked[st + ci] or blocked[st + cj]):
                    continue  # no corner cutting
                ng = g + (sq2 if diag else 1.0) * lc + tt[nd]
                if ng < best[ns] and not closed[ns]:
                    best[ns] = ng
                    arrive[ns] = nd
                    came[ns] = st
                    heappush(pq, (ng + h[idx + step] * weight, -ng, ns))
            if nl > 1 and not vblocked[idx]:
                for k2 in range(nl):
                    ns = k2 * n_cells + idx
                    if k2 == k or blocked[ns]:
                        continue
                    ng = g + via_cost
                    if ng < best[ns] and not closed[ns]:
                        best[ns] = ng
                        arrive[ns] = 8
                        came[ns] = st
                        heappush(pq, (ng + h[idx] * weight, -ng, ns))
        t_search = time.time() - t1
        stats = {
            "raster": round(t_raster, 3),
            "search": round(t_search, 3),
            "expanded": expanded,
            "grid": [nx, ny, nl],
            "gridStep": res,
        }
        if goal_state is None:
            stats["enclosed"] = not touched_edge and expanded <= MAX_EXPANSIONS
            stats["reason"] = (
                "search limit reached"
                if expanded > MAX_EXPANSIONS
                else "no free path in the grid"
                + (" (the start side is boxed in)" if not touched_edge else "")
            )
            return None, stats

        seq = [goal_state]
        while seq[-1] in came:
            seq.append(came[seq[-1]])
        seq.reverse()
        cells = [divmod(s_, n_cells) for s_ in seq]  # (k, idx)
        cells = _straighten(cells, blocked, n_cells, ny)
        pts: List[Tuple[float, float, str]] = []
        for k, idx in cells:
            i, j = divmod(idx, ny)
            p = (round(x0 + i * res, 4), round(y0 + j * res, 4), layers[k])
            if not pts or pts[-1] != p:
                pts.append(p)
        k0, idx0 = cells[0]
        k1, idx1 = cells[-1]
        sx, sy = starts[(idx0 // ny, idx0 % ny, k0)]
        ex, ey = goals[(idx1 // ny, idx1 % ny, k1)]
        pts[0] = (sx, sy, pts[0][2])
        pts[-1] = (ex, ey, pts[-1][2])
        plan = _simplify(pts, opts["width"])
        return plan, stats

    def _check(
        self,
        ctx: Dict[str, Any],
        net: str,
        plan: _Plan,
        opts: Dict[str, Any],
        extra: Sequence[cg.Shape] = (),
    ) -> List[Dict[str, Any]]:
        """Exact clearance check of a planned path; returns violations."""
        rules: cg.Rules = ctx["rules"]
        hw, vr = opts["width"] / 2, opts["viaSize"] / 2
        clr_override = opts.get("clearance")
        idx = cg.SpatialIndex(
            [s for s in ctx["shapes"] if s.net != net or s.role == "hole"]
            + list(ctx["keepouts"])
            + list(extra)
        )
        edges = cg.SpatialIndex(cg.outline_edges(ctx["outlines"]))
        bad: List[Dict[str, Any]] = []
        tol = 1e-4

        def need(s: cg.Shape) -> float:
            if s.role == "keepout":
                return 0.0
            if s.role == "hole":
                return max(rules.hole_clearance, clr_override or 0.0)
            if s.extra_clearance:
                return s.extra_clearance
            return clr_override if clr_override is not None else rules.clearance(net, s.net)

        for layer, a, b, w in plan.tracks:
            r = w / 2
            box = (
                min(a[0], b[0]) - 3,
                min(a[1], b[1]) - 3,
                max(a[0], b[0]) + 3,
                max(a[1], b[1]) + 3,
            )
            for s in idx.query(*box):
                if layer not in s.layers or (s.role == "keepout" and not s.meta.get("noTracks")):
                    continue
                gap = s.segment_distance(a, b) - r
                if gap < need(s) - tol:
                    bad.append(_viol("track", layer, a, b, s, gap, need(s)))
            for e in edges.query(*box):
                gap = e.segment_distance(a, b) - r
                if gap < rules.edge_clearance - tol:
                    bad.append(_viol("track", layer, a, b, e, gap, rules.edge_clearance))
        for x, y in plan.vias:
            box = (x - 3, y - 3, x + 3, y + 3)
            for s in idx.query(*box):
                if not s.layers or (s.role == "keepout" and not s.meta.get("noVias")):
                    continue
                gap = s.distance(x, y) - vr
                if gap < need(s) - tol:
                    bad.append(_viol("via", None, (x, y), (x, y), s, gap, need(s)))
            for e in edges.query(*box):
                gap = e.distance(x, y) - vr
                if gap < rules.edge_clearance - tol:
                    bad.append(_viol("via", None, (x, y), (x, y), e, gap, rules.edge_clearance))
        return bad

    def _options(self, ctx: Dict[str, Any], net: str, params: Dict[str, Any]) -> Dict[str, Any]:
        rules = ctx["rules"].for_net(net)
        cu = ctx["cu"]
        layers = params.get("layersAllowed")
        if layers:
            unknown = [L for L in layers if L not in cu]
            if unknown:
                raise ValueError(f"unknown copper layers {unknown}; board has {cu}")
        else:
            layers = [cu[0], cu[-1]] if len(cu) > 1 else list(cu)
        via = params.get("via") or {}
        prefer = params.get("preferLayer")
        if prefer and prefer not in layers:
            raise ValueError(f"preferLayer {prefer} is not in layersAllowed {layers}")
        other_cost = float(params.get("otherLayerCost", 1.5))
        layer_cost = {L: (1.0 if (not prefer or L == prefer) else other_cost) for L in layers}
        grid = float(params.get("gridStep", 0.1))
        if not 0.02 <= grid <= 1.0:
            raise ValueError("gridStep must be between 0.02 and 1.0 mm")
        avoid = []
        for reg in params.get("avoidRegions") or []:
            from commands.autoroute_pipeline import normalize_region

            poly = normalize_region(reg)
            lays = reg.get("layers") if isinstance(reg, dict) else None
            avoid.append((poly, lays))
        clearance = params.get("clearance")
        opts = {
            "layers": list(layers),
            "width": float(params.get("width") or rules["track_width"]),
            "viaSize": float(via.get("size") or rules["via_diameter"]),
            "viaDrill": float(via.get("drill") or rules["via_drill"]),
            "clearance": float(clearance) if clearance is not None else None,
            "viaCost": float(params.get("viaCost", 2.0)),
            "gridStep": grid,
            "layerCost": layer_cost,
            "avoid": avoid,
            "searchMargin": float(params.get("searchMargin", 8.0)),
            "refine": bool(params.get("refine", True)),
        }
        if opts["width"] <= 0 or opts["viaSize"] <= opts["viaDrill"]:
            raise ValueError("width must be > 0 and via size > via drill")
        return opts

    def _route_one(
        self,
        ctx: Dict[str, Any],
        net: str,
        params: Dict[str, Any],
        extra: Sequence[cg.Shape] = (),
    ) -> Dict[str, Any]:
        opts = self._options(ctx, net, params)
        frm, to = params.get("from"), params.get("to")
        if (frm is None) != (to is None):
            raise ValueError(
                "give both from and to, or neither (route the closest missing connection)"
            )
        if frm is None:
            pair = self._missing_connection(net)
            if pair is None:
                return {
                    "success": True,
                    "alreadyConnected": True,
                    "message": f"{net} has no missing connection",
                    "net": net,
                }
            ep_a = {"kind": "pad", "pad": pair[0], "label": _pad_label(pair[0])}
            ep_b = {"kind": "pad", "pad": pair[1], "label": _pad_label(pair[1])}
        else:
            ep_a, ep_b = cg.parse_point(frm, self.board), cg.parse_point(to, self.board)
        src, src_ids = self._endpoint_group(ctx, ep_a, net, opts["layers"])
        dst, dst_ids = self._endpoint_group(ctx, ep_b, net, opts["layers"])
        if not src or not dst:
            raise ValueError("an endpoint has no copper on the allowed layers " f"{opts['layers']}")
        if src_ids & dst_ids:
            return {
                "success": True,
                "alreadyConnected": True,
                "message": f"{ep_a['label']} and {ep_b['label']} are already connected on {net}",
                "net": net,
            }
        a0 = _endpoint_xy(ep_a)
        b0 = _endpoint_xy(ep_b)
        m = opts["searchMargin"]
        window = (
            min(a0[0], b0[0]) - m,
            min(a0[1], b0[1]) - m,
            max(a0[0], b0[0]) + m,
            max(a0[1], b0[1]) + m,
        )

        def near(pts):
            return [
                p for p in pts if window[0] <= p[0] <= window[2] and window[1] <= p[1] <= window[3]
            ]

        # Strategy: local window at gridStep; if no path, the same window at half
        # the step (narrow pin-escape slots); then the whole board, unless the
        # local search proved the start side is boxed in.
        attempts = []
        plan = None
        tries = [(opts["gridStep"], window)]
        if opts["refine"] and opts["gridStep"] / 2 >= 0.025:
            tries.append((opts["gridStep"] / 2, window))
        tries.append((opts["gridStep"], None))
        for step, win in tries:
            if win is None and attempts and all(a.get("enclosed") for a in attempts):
                break
            o = dict(opts, gridStep=step)
            s_pts, d_pts = (near(src), near(dst)) if win is not None else (src, dst)
            plan, st = self._plan(ctx, net, s_pts, d_pts, o, extra, win)
            st["window"] = "local" if win is not None else "board"
            attempts.append(st)
            if plan is not None:
                break
        stats = {"attempts": attempts}
        if plan is None:
            last = attempts[-1]
            return _fail(
                f"No route found for {net} from {ep_a['label']} to {ep_b['label']}",
                last.get("reason", ""),
                stats=stats,
                options=_public(opts),
            )
        violations = self._check(ctx, net, plan, opts, extra)
        out = {
            "success": not violations,
            "net": net,
            "from": ep_a["label"],
            "to": ep_b["label"],
            "plan": plan,
            "lengthMm": round(plan.length, 3),
            "segments": len(plan.tracks),
            "vias": len(plan.vias),
            "layersUsed": sorted({t[0] for t in plan.tracks}),
            "path": [{"x": round(x, 4), "y": round(y, 4), "layer": L} for x, y, L in plan.points],
            "stats": stats,
            "options": _public(opts),
        }
        if violations:
            out["message"] = (
                "The grid path fails the exact clearance check; nothing was written. "
                "Try a smaller gridStep or a larger clearance margin."
            )
            out["clearanceViolations"] = violations[:10]
        return out

    def _write(self, net: str, plan: _Plan, opts: Dict[str, Any], locked: bool) -> List[Any]:
        import pcbnew

        board = self.board
        netinfo = board.FindNet(net)
        cu_ids = {board.GetLayerName(lid): lid for lid in board.GetEnabledLayers().CuStack()}
        added: List[Any] = []
        try:
            for layer, a, b, w in plan.tracks:
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
                t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
                t.SetWidth(pcbnew.FromMM(w))
                t.SetLayer(cu_ids[layer])
                t.SetNet(netinfo)
                t.SetLocked(locked)
                board.Add(t)
                added.append(t)
            for x, y in plan.vias:
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                try:
                    v.SetWidth(pcbnew.FromMM(opts["viaSize"]))
                except TypeError:
                    v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(opts["viaSize"]))
                v.SetDrill(pcbnew.FromMM(opts["viaDrill"]))
                v.SetNet(netinfo)
                v.SetLocked(locked)
                board.Add(v)
                added.append(v)
        except Exception:
            for item in added:
                try:
                    delete_board_item(board, item)
                except Exception:
                    pass
            raise
        return added

    # ------------------------------------------------------------------ tools
    def route_connection(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route one connection of a net with the grid A* router (see module docstring)."""
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        net = params.get("net")
        if not net:
            return _fail("net is required")
        if self.board.FindNet(net) is None:
            return _fail(f"Net '{net}' not found on the board")
        t0 = time.time()
        try:
            ctx = self._context()
            out = self._route_one(ctx, net, params)
        except ValueError as exc:
            return _fail("Invalid route_connection request", str(exc))
        except Exception as exc:
            logger.exception("route_connection failed")
            return _fail("route_connection failed", f"{type(exc).__name__}: {exc}")
        if not out.get("success") or out.get("alreadyConnected"):
            out.pop("plan", None)
            out["seconds"] = round(time.time() - t0, 3)
            return out
        plan = out.pop("plan")
        dry = bool(params.get("dryRun", False))
        locked = bool(params.get("locked", False))
        if not dry:
            opts = self._options(ctx, net, params)
            try:
                self._write(net, plan, opts, locked)
            except Exception as exc:
                return _fail("Writing the route failed; the board is unchanged", str(exc))
        out["written"] = not dry
        out["dryRun"] = dry
        out["locked"] = locked
        out["seconds"] = round(time.time() - t0, 3)
        out["message"] = (
            f"{'Planned' if dry else 'Routed'} {net} {out['from']} -> {out['to']}: "
            f"{out['lengthMm']} mm, {out['segments']} segments, {out['vias']} vias"
        )
        return out

    def route_bus(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route several connections in order; each later one treats earlier ones as obstacles.

        ``pitch`` (centre-to-centre, mm) sets the spacing between bus members:
        their mutual clearance becomes ``pitch - width`` (never below the rule
        clearance). All connections are written together, or none.
        """
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        conns = params.get("connections") or []
        if not isinstance(conns, list) or not conns:
            return _fail("connections must be a non-empty list of {net, from?, to?}")
        shared = {k: v for k, v in params.items() if k not in ("connections", "dryRun", "pitch")}
        pitch = params.get("pitch")
        t0 = time.time()
        try:
            ctx = self._context()
        except Exception as exc:
            return _fail("route_bus failed", str(exc))
        extra: List[cg.Shape] = []
        planned = []
        results = []
        for i, c in enumerate(conns):
            net = c.get("net") if isinstance(c, dict) else None
            if not net or self.board.FindNet(net) is None:
                return _fail(f"connections[{i}]: unknown or missing net {net!r}", results=results)
            p = dict(shared)
            p.update({k: v for k, v in c.items() if k != "net"})
            try:
                out = self._route_one(ctx, net, p, extra)
            except ValueError as exc:
                return _fail(f"connections[{i}] ({net}): {exc}", results=results)
            plan = out.pop("plan", None)
            results.append(out)
            if not out.get("success"):
                return _fail(
                    f"connections[{i}] ({net}) could not be routed; nothing was written",
                    out.get("errorDetails", out.get("message", "")),
                    results=results,
                )
            if plan is None:
                continue
            opts = self._options(ctx, net, p)
            mutual = None
            if pitch:
                mutual = max(float(pitch) - opts["width"], 0.0)
            for layer, a, b, w in plan.tracks:
                s = cg.Shape("capsule", (a[0], a[1], b[0], b[1], w / 2), [layer], net, "track")
                if mutual:
                    s.extra_clearance = max(mutual, ctx["rules"].clearance(net, net))
                extra.append(s)
            for x, y in plan.vias:
                extra.append(cg.Shape("circle", (x, y, opts["viaSize"] / 2), ctx["cu"], net, "via"))
            planned.append((net, plan, opts))
        dry = bool(params.get("dryRun", False))
        locked = bool(params.get("locked", False))
        if not dry:
            written: List[Any] = []
            try:
                for net, plan, opts in planned:
                    written += self._write(net, plan, opts, locked)
            except Exception as exc:
                for item in written:
                    try:
                        delete_board_item(self.board, item)
                    except Exception:
                        pass
                return _fail("Writing the bus failed; the board is unchanged", str(exc))
        return {
            "success": True,
            "message": f"{'Planned' if dry else 'Routed'} {len(planned)} of {len(conns)} connections",
            "dryRun": dry,
            "written": not dry,
            "results": results,
            "seconds": round(time.time() - t0, 3),
        }

    def length_match(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Report routed lengths of nets / pairs against a target and tolerance (no tuning)."""
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        nets = list(params.get("nets") or [])
        pairs = [list(p) for p in (params.get("pairs") or [])]
        for p in pairs:
            if len(p) != 2:
                return _fail("each pair must be [positiveNet, negativeNet]")
            nets += [n for n in p if n not in nets]
        if not nets:
            return _fail("give nets and/or pairs")
        missing = [n for n in nets if self.board.FindNet(n) is None]
        if missing:
            return _fail(f"nets not found: {missing}")
        tol = float(params.get("tolerance", 0.5))
        lengths = {n: 0.0 for n in nets}
        counts = {n: 0 for n in nets}
        vias = {n: 0 for n in nets}
        for t in self.board.GetTracks():
            n = t.GetNetname()
            if n not in lengths:
                continue
            if t.GetClass() == "PCB_VIA":
                vias[n] += 1
            else:
                lengths[n] += t.GetLength() / cg.NM
                counts[n] += 1
        target = params.get("target")
        target = float(target) if target is not None else max(lengths.values())
        rows = []
        for n in nets:
            delta = lengths[n] - target
            rows.append(
                {
                    "net": n,
                    "lengthMm": round(lengths[n], 3),
                    "deltaMm": round(delta, 3),
                    "withinTolerance": abs(delta) <= tol,
                    "addMm": round(max(0.0, -delta - tol), 3) if delta < -tol else 0.0,
                    "segments": counts[n],
                    "vias": vias[n],
                    "routed": counts[n] > 0,
                }
            )
        pair_rows = []
        skew_tol = float(params.get("pairTolerance", tol))
        for p, n in pairs:
            skew = lengths[p] - lengths[n]
            pair_rows.append(
                {
                    "positive": p,
                    "negative": n,
                    "skewMm": round(skew, 3),
                    "withinTolerance": abs(skew) <= skew_tol,
                    "shorter": n if skew > 0 else p,
                }
            )
        ok = all(r["withinTolerance"] for r in rows) and all(
            r["withinTolerance"] for r in pair_rows
        )
        return {
            "success": True,
            "allWithinTolerance": ok,
            "targetMm": round(target, 3),
            "toleranceMm": tol,
            "nets": rows,
            "pairs": pair_rows,
            "note": (
                "Lengths are the sum of routed track/arc lengths per net (vias and pad "
                "internals excluded). This tool reports only; it does not add tuning meanders."
            ),
        }


def _octilinear_cells(i0: int, j0: int, i1: int, j1: int, diag_first: bool):
    """Cells of a two-segment 0/45/90-degree path from (i0, j0) to (i1, j1)."""
    di, dj = i1 - i0, j1 - j0
    m = min(abs(di), abs(dj))
    si = (di > 0) - (di < 0)
    sj = (dj > 0) - (dj < 0)
    rest = (abs(di) - m, abs(dj) - m)
    straight = (si, 0) if rest[0] else (0, sj)
    n_straight = max(rest)
    legs = [((si, sj), m), (straight, n_straight)]
    if not diag_first:
        legs.reverse()
    out = []
    i, j = i0, j0
    for (a, b), n in legs:
        for _ in range(n):
            out.append((i, j, a, b))
            i, j = i + a, j + b
    out.append((i, j, 0, 0))
    return out


def _straighten(cells, blocked, n_cells: int, ny: int, max_span: int = 400, patience: int = 30):
    """Replace grid staircases by two-segment octilinear runs where the grid is free.

    ``cells`` is the A* path as (layer, flat index). Runs never cross a layer
    change, and a diagonal step is only taken where both orthogonal
    neighbours are free (no corner cutting), as in the search.
    """
    if len(cells) < 4:
        return cells

    def free_run(k: int, a: int, b: int, diag_first: bool):
        i0, j0 = divmod(a, ny)
        i1, j1 = divmod(b, ny)
        run = _octilinear_cells(i0, j0, i1, j1, diag_first)
        base = k * n_cells
        for i, j, di, dj in run:
            idx = i * ny + j
            if blocked[base + idx]:
                return None
            if di and dj and (blocked[base + idx + di * ny] or blocked[base + idx + dj]):
                return None
        return [(k, i * ny + j) for i, j, _, _ in run]

    out = [cells[0]]
    i = 0
    n = len(cells)
    while i < n - 1:
        k = cells[i][0]
        best_j, best_run = i + 1, None
        misses = 0
        j = i + 2
        while j < n and j - i <= max_span and cells[j][0] == k and misses < patience:
            run = free_run(k, cells[i][1], cells[j][1], True) or free_run(
                k, cells[i][1], cells[j][1], False
            )
            if run is not None and len(run) <= j - i + 1:
                best_j, best_run = j, run
                misses = 0
            else:
                misses += 1
            j += 1
        if best_run is None:
            out.append(cells[i + 1])
            i += 1
        else:
            out.extend(best_run[1:])
            i = best_j
    return out


def _viol(kind, layer, a, b, s: cg.Shape, gap: float, need: float) -> Dict[str, Any]:
    return {
        "item": kind,
        "layer": layer,
        "at": [round(a[0], 3), round(a[1], 3)],
        "to": [round(b[0], 3), round(b[1], 3)],
        "obstacle": s.role + (f" {s.ref}" if s.ref else "") + (f" [{s.net}]" if s.net else ""),
        "gapMm": round(gap, 4),
        "requiredMm": round(need, 4),
    }


def _pad_label(pad: Any) -> str:
    return f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}"


def _endpoint_xy(ep: Dict[str, Any]) -> Tuple[float, float]:
    if ep["kind"] == "point":
        return ep["x"], ep["y"]
    p = ep["pad"].GetPosition()
    return cg.mm(p.x), cg.mm(p.y)


def _public(opts: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in opts.items() if k not in ("avoid",)}
    out["avoidRegions"] = len(opts.get("avoid") or [])
    return out


def _simplify(pts: List[Tuple[float, float, str]], width: float) -> _Plan:
    """Collapse collinear grid steps into segments; layer changes become vias."""
    plan = _Plan()
    keep = [pts[0]]
    for k in range(1, len(pts) - 1):
        a, c, e = keep[-1], pts[k], pts[k + 1]
        if c[2] != a[2] or c[2] != e[2]:
            keep.append(c)
            continue
        d1 = (c[0] - a[0], c[1] - a[1])
        d2 = (e[0] - c[0], e[1] - c[1])
        if abs(d1[0] * d2[1] - d1[1] * d2[0]) > 1e-9 or (d1[0] * d2[0] + d1[1] * d2[1]) < 0:
            keep.append(c)
    keep.append(pts[-1])
    plan.points = keep
    for a, c in zip(keep, keep[1:]):
        if a[2] != c[2]:
            plan.vias.append((a[0], a[1]))
            continue
        if abs(a[0] - c[0]) < 1e-9 and abs(a[1] - c[1]) < 1e-9:
            continue
        plan.tracks.append((a[2], (a[0], a[1]), (c[0], c[1]), width))
        plan.length += math.hypot(c[0] - a[0], c[1] - a[1])
    return plan
