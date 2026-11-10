"""Plane fan-out: a short stub and a via from SMD pads of plane nets (MCP_ISSUES R1).

Freerouting does not reliably connect SMD pads to inner planes; on the
CD_Mainboard it left 50-100 plane connections open per run until every
GND/plane pad had a pre-placed stub and (locked) via. ``fanout_to_planes``
does that generically:

* for every SMD pad on one of ``nets`` that has no same-net via within
  ``servedRadius`` of its centre, try via sites at increasing distance from the
  pad edge (up to ``maxStub``), outward from the footprint centre first, then
  sideways, then inward, and connect each with a stub on the pad's layer;
* exposed (centre) pads get a grid of vias inside the pad instead;
* fine-pitch pins (nearest pad of the same footprint closer than the pitch
  threshold) are skipped -- they are normally tied to a neighbouring
  decoupling capacitor whose pads are fanned out;
* ``regionPolicy: "zone"`` (default) only places a via where it lands inside a
  zone of the same net on another copper layer (the plane it is meant to
  reach); ``"any"`` places it anywhere.

Every site is checked against all other-net copper (pads, tracks, vias, on
every layer for the via, on the pad's layer for the stub) with the clearance
from the project's net classes, against same-net drills (hole-to-hole), the
board edge (copper-to-edge clearance), rule areas that forbid vias/tracks,
optionally footprint courtyards, and the vias placed earlier in the same run.

Ported and generalised from the CD_Mainboard ``fanout_planes.py`` prototype.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from utils import copper_geometry as cg
from utils.board_items import delete_board_item

logger = logging.getLogger("kicad_interface")


def _fail(message: str, details: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "message": message}
    if details:
        out["errorDetails"] = details
    return out


class FanoutCommands:
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

    def fanout_to_planes(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        try:
            return self._fanout(params)
        except ValueError as exc:
            return _fail("Invalid fanout_to_planes request", str(exc))
        except Exception as exc:
            logger.exception("fanout_to_planes failed")
            return _fail(
                "fanout_to_planes failed; the board is unchanged", f"{type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    def _fanout(self, params: Dict[str, Any]) -> Dict[str, Any]:
        import pcbnew

        board = self.board
        nets = params.get("nets")
        if not nets or not isinstance(nets, list):
            raise ValueError("nets must be a non-empty list of net names")
        missing = [n for n in nets if board.FindNet(n) is None]
        if missing:
            raise ValueError(f"nets not found: {missing}")
        rules = cg.Rules(board)
        via_p = params.get("via") or {}
        max_stub = float(params.get("maxStub", 1.6))
        served_r = float(params.get("servedRadius", 1.0))
        policy = params.get("regionPolicy", "zone")
        if policy not in ("zone", "any"):
            raise ValueError("regionPolicy must be 'zone' or 'any'")
        fine = params.get("skipFinePitch", True)
        fine_pitch = 0.0 if fine is False else (0.65 if fine is True else float(fine))
        ep = params.get("exposedPadGrid", True)
        ep_pitch = (
            1.2 if ep is True else (float(ep.get("pitch", 1.2)) if isinstance(ep, dict) else 0.0)
        )
        large_vias = int(params.get("largePadVias", 2))
        locked = bool(params.get("locked", True))
        dry = bool(params.get("dryRun", False))
        only_refs = set(params.get("refs") or [])
        court = params.get("avoidCourtyards", False)
        limit = int(params.get("limit", 100))

        shapes, cu_names = cg.collect_copper(board, pcbnew)
        cu = list(cu_names.values())
        cu_ids = {name: lid for lid, name in cu_names.items()}
        keepouts = cg.keepout_shapes(board, pcbnew, cu_names)
        outlines = cg.board_outlines(board, pcbnew)
        edges = cg.SpatialIndex(cg.outline_edges(outlines))
        index = cg.SpatialIndex(shapes + keepouts)

        zones: Dict[str, List[Tuple[str, List[Tuple[float, float]]]]] = {}
        for z in board.Zones():
            try:
                if z.GetIsRuleArea() or z.GetNetname() not in nets:
                    continue
                outline = z.Outline()
                for lid, name in cu_names.items():
                    if not z.GetLayerSet().Contains(lid):
                        continue
                    for i in range(outline.OutlineCount()):
                        zones.setdefault(z.GetNetname(), []).append(
                            (name, cg._poly_points(outline.COutline(i)))
                        )
            except Exception:
                continue

        courtyards: List[Tuple[str, Tuple[float, float, float, float]]] = []
        if court:
            refs = set(court) if isinstance(court, list) else None
            for fp in board.GetFootprints():
                if refs is not None and fp.GetReference() not in refs:
                    continue
                for lay in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                    try:
                        poly = fp.GetCourtyard(lay)
                        if poly.OutlineCount():
                            bb = poly.BBox()
                            courtyards.append(
                                (
                                    fp.GetReference(),
                                    (
                                        cg.mm(bb.GetLeft()),
                                        cg.mm(bb.GetTop()),
                                        cg.mm(bb.GetRight()),
                                        cg.mm(bb.GetBottom()),
                                    ),
                                )
                            )
                    except Exception:
                        continue

        new_shapes: List[cg.Shape] = []
        planned: List[Dict[str, Any]] = []
        skipped = {
            "alreadyServed": 0,
            "finePitch": 0,
            "notInPlaneRegion": 0,
            "noRoom": 0,
            "notSmd": 0,
        }
        no_room: List[str] = []

        def via_ok(
            net: str, x: float, y: float, vr: float, drill: float, own_ref: str
        ) -> Optional[str]:
            if not cg.inside_board(x, y, outlines):
                return "outside board"
            for e in edges.query(x - 3, y - 3, x + 3, y + 3):
                if e.distance(x, y) - vr < rules.edge_clearance - 1e-6:
                    return "board edge"
            for ref, (cx0, cy0, cx1, cy1) in courtyards:
                if ref != own_ref and cx0 - vr < x < cx1 + vr and cy0 - vr < y < cy1 + vr:
                    return f"courtyard {ref}"
            reach = vr + 2.0
            for s in index.query(x - reach, y - reach, x + reach, y + reach) + [
                s for s in new_shapes if abs(s.bbox[0] - x) < 5 and abs(s.bbox[1] - y) < 5
            ]:
                if s.role == "keepout":
                    if s.meta.get("noVias") and s.distance(x, y) < vr:
                        return "rule area"
                    continue
                if not s.layers:
                    continue
                if s.role == "hole":
                    if s.distance(x, y) - vr < rules.hole_clearance:
                        return f"hole {s.ref}"
                    continue
                if s.net == net:
                    other_drill = s.meta.get("drill")
                    if (
                        other_drill
                        and s.role in ("via", "pad")
                        and s.meta.get("pth", s.role == "via")
                    ):
                        cx = (s.bbox[0] + s.bbox[2]) / 2
                        cy = (s.bbox[1] + s.bbox[3]) / 2
                        if (
                            math.hypot(cx - x, cy - y)
                            < drill / 2 + other_drill / 2 + rules.hole_to_hole
                        ):
                            return "hole-to-hole"
                    continue
                if s.distance(x, y) - vr < rules.clearance(net, s.net) - 1e-6:
                    return f"{s.role} {s.ref or ''}[{s.net}]".strip()
            return None

        def stub_ok(
            net: str, layer: str, a: Tuple[float, float], b: Tuple[float, float], hw: float
        ) -> bool:
            x0, y0 = min(a[0], b[0]) - 2, min(a[1], b[1]) - 2
            x1, y1 = max(a[0], b[0]) + 2, max(a[1], b[1]) + 2
            for s in index.query(x0, y0, x1, y1) + new_shapes:
                if layer not in s.layers:
                    continue
                if s.role == "keepout":
                    if s.meta.get("noTracks") and s.segment_distance(a, b) < hw:
                        return False
                    continue
                if s.net == net and s.role != "hole":
                    continue
                need = rules.hole_clearance if s.role == "hole" else rules.clearance(net, s.net)
                if s.segment_distance(a, b) - hw < need - 1e-6:
                    return False
            for e in edges.query(x0, y0, x1, y1):
                if e.segment_distance(a, b) - hw < rules.edge_clearance - 1e-6:
                    return False
            return True

        def in_region(net: str, x: float, y: float, pad_layer: str) -> bool:
            if policy == "any":
                return True
            return any(
                lay != pad_layer and cg.point_in_polygon(x, y, pts)
                for lay, pts in zones.get(net, [])
            )

        def add_via(net: str, x: float, y: float, vr: float, drill: float) -> None:
            new_shapes.append(cg.Shape("circle", (x, y, vr), cu, net, "via", meta={"drill": drill}))

        net_vias: Dict[str, List[Tuple[float, float]]] = {}
        for s in shapes:
            if s.role == "via" and s.net in nets:
                net_vias.setdefault(s.net, []).append(
                    ((s.bbox[0] + s.bbox[2]) / 2, (s.bbox[1] + s.bbox[3]) / 2)
                )

        smd = getattr(pcbnew, "PAD_ATTRIB_SMD", None)
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            if only_refs and ref not in only_refs:
                continue
            fpos = fp.GetPosition()
            fcx, fcy = cg.mm(fpos.x), cg.mm(fpos.y)
            pads = list(fp.Pads())
            centres = [(cg.mm(p.GetPosition().x), cg.mm(p.GetPosition().y)) for p in pads]
            for pi, pad in enumerate(pads):
                net = pad.GetNetname()
                if net not in nets:
                    continue
                label = f"{ref}.{pad.GetNumber()}"
                if pad.GetAttribute() != smd:
                    skipped["notSmd"] += 1
                    continue
                x, y = centres[pi]
                rules_n = rules.for_net(net)
                vsize = float(via_p.get("size") or rules_n["via_diameter"])
                vdrill = float(via_p.get("drill") or rules_n["via_drill"])
                vr = vsize / 2
                layer = cu_names.get(pad.GetLayer()) or (
                    cu[0] if pad.IsOnLayer(cu_ids[cu[0]]) else cu[-1]
                )
                bb = pad.GetBoundingBox()
                w, h = cg.mm(bb.GetWidth()), cg.mm(bb.GetHeight())
                served = [
                    v
                    for v in net_vias.get(net, [])
                    if math.hypot(v[0] - x, v[1] - y) < max(served_r, min(w, h) / 2)
                ]
                centred = math.hypot(x - fcx, y - fcy) < 0.5 and len(pads) > 3
                if centred and min(w, h) >= 1.5 and ep_pitch > 0:
                    margin = vr + 0.1
                    nx = max(1, int((w - 2 * margin) / ep_pitch) + 1)
                    ny = max(1, int((h - 2 * margin) / ep_pitch) + 1)
                    want = nx * ny
                    have = sum(
                        1
                        for v in net_vias.get(net, [])
                        if abs(v[0] - x) < w / 2 and abs(v[1] - y) < h / 2
                    )
                    if have >= max(1, want // 2):
                        skipped["alreadyServed"] += 1
                        continue
                    got = 0
                    for i in range(nx):
                        for j in range(ny):
                            vx = x + (i - (nx - 1) / 2) * ep_pitch
                            vy = y + (j - (ny - 1) / 2) * ep_pitch
                            if via_ok(net, vx, vy, vr, vdrill, ref) is None:
                                add_via(net, vx, vy, vr, vdrill)
                                planned.append(
                                    {
                                        "pad": label,
                                        "net": net,
                                        "kind": "exposedPadGrid",
                                        "via": {"x": round(vx, 4), "y": round(vy, 4)},
                                        "_v": (vx, vy, vsize, vdrill),
                                    }
                                )
                                got += 1
                    if not got:
                        skipped["noRoom"] += 1
                        no_room.append(f"{label} ({net})")
                    continue
                if served:
                    skipped["alreadyServed"] += 1
                    continue
                if fine_pitch > 0:
                    nearest = min(
                        (
                            math.hypot(cx - x, cy - y)
                            for k, (cx, cy) in enumerate(centres)
                            if k != pi
                        ),
                        default=99.0,
                    )
                    if nearest < fine_pitch:
                        skipped["finePitch"] += 1
                        continue
                stub_w = float(params.get("stubWidth") or 0) or min(
                    max(rules_n["track_width"], 0.25), max(min(w, h), 0.15)
                )
                dx, dy = x - fcx, y - fcy
                if abs(dx) >= abs(dy):
                    first = (math.copysign(1.0, dx or 1.0), 0.0)
                else:
                    first = (0.0, math.copysign(1.0, dy))
                ox, oy = first
                dirs = [first, (oy, ox), (-oy, -ox), (-ox, -oy)]
                want = large_vias if w * h >= 2.5 and large_vias > 1 else 1
                got = 0
                region_miss = False
                for ddx, ddy in dirs:
                    half = (w if ddx else h) / 2
                    gap = vr + 0.05
                    gaps = []
                    while gap <= max_stub + 1e-9:
                        gaps.append(gap)
                        gap += 0.15
                    for gap in gaps:
                        d = half + gap
                        offsets = [0.0]
                        if want > 1:
                            perp = (h if ddx else w) / 2 - vr
                            offsets = [-perp, perp] if perp > 0.45 else [0.0]
                        for off in offsets:
                            vx = x + ddx * d + ddy * off
                            vy = y + ddy * d + ddx * off
                            if not in_region(net, vx, vy, layer):
                                region_miss = True
                                continue
                            if via_ok(net, vx, vy, vr, vdrill, ref) is not None:
                                continue
                            start = (x + ddy * off, y + ddx * off)
                            if not stub_ok(net, layer, start, (vx, vy), stub_w / 2):
                                continue
                            add_via(net, vx, vy, vr, vdrill)
                            new_shapes.append(
                                cg.Shape(
                                    "capsule",
                                    (start[0], start[1], vx, vy, stub_w / 2),
                                    [layer],
                                    net,
                                    "track",
                                )
                            )
                            planned.append(
                                {
                                    "pad": label,
                                    "net": net,
                                    "kind": "stub",
                                    "via": {"x": round(vx, 4), "y": round(vy, 4)},
                                    "stub": {
                                        "from": {"x": round(start[0], 4), "y": round(start[1], 4)},
                                        "to": {"x": round(vx, 4), "y": round(vy, 4)},
                                        "width": round(stub_w, 4),
                                        "layer": layer,
                                    },
                                    "_v": (vx, vy, vsize, vdrill),
                                    "_t": (start, (vx, vy), stub_w, layer),
                                }
                            )
                            got += 1
                            if got >= want:
                                break
                        if got:
                            break
                    if got:
                        break
                if not got:
                    if region_miss and policy == "zone" and not zones.get(net):
                        skipped["notInPlaneRegion"] += 1
                    elif region_miss and not _any_site_in_region(
                        zones.get(net, []), layer, x, y, max_stub + max(w, h)
                    ):
                        skipped["notInPlaneRegion"] += 1
                    else:
                        skipped["noRoom"] += 1
                        no_room.append(f"{label} ({net})")

        if not dry and planned:
            self._write(planned, locked, cu_ids)
        vias = len(planned)
        stubs = sum(1 for p in planned if p["kind"] == "stub")
        items = [{k: v for k, v in p.items() if not k.startswith("_")} for p in planned]
        return {
            "success": True,
            "dryRun": dry,
            "message": (
                f"{'Would place' if dry else 'Placed'} {vias} via(s) and {stubs} stub(s) on "
                f"{len({p['pad'] for p in planned})} pad(s); {skipped['noRoom']} pad(s) had no room"
            ),
            "placed": {"vias": vias, "stubs": stubs, "pads": len({p["pad"] for p in planned})},
            "skipped": skipped,
            "noRoom": no_room[:limit],
            "items": items[:limit],
            "itemsTruncated": max(0, len(items) - limit),
            "locked": locked,
            "rules": {
                "source": rules.source,
                "edgeClearance": rules.edge_clearance,
                "holeToHole": rules.hole_to_hole,
            },
        }

    def _write(
        self, planned: Sequence[Dict[str, Any]], locked: bool, cu_ids: Dict[str, int]
    ) -> None:
        import pcbnew

        board = self.board
        added: List[Any] = []
        try:
            for p in planned:
                netinfo = board.FindNet(p["net"])
                if "_t" in p:
                    a, b, w, layer = p["_t"]
                    t = pcbnew.PCB_TRACK(board)
                    t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
                    t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
                    t.SetWidth(pcbnew.FromMM(w))
                    t.SetLayer(cu_ids[layer])
                    t.SetNet(netinfo)
                    t.SetLocked(locked)
                    board.Add(t)
                    added.append(t)
                x, y, size, drill = p["_v"]
                v = pcbnew.PCB_VIA(board)
                v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
                try:
                    v.SetWidth(pcbnew.FromMM(size))
                except TypeError:
                    v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(size))
                v.SetDrill(pcbnew.FromMM(drill))
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


def _any_site_in_region(zones, pad_layer: str, x: float, y: float, reach: float) -> bool:
    """Whether any point within ``reach`` of the pad lies in a same-net zone on another layer."""
    steps = 8
    for i in range(steps + 1):
        for j in range(steps + 1):
            px = x - reach + 2 * reach * i / steps
            py = y - reach + 2 * reach * j / steps
            if any(lay != pad_layer and cg.point_in_polygon(px, py, pts) for lay, pts in zones):
                return True
    return False
