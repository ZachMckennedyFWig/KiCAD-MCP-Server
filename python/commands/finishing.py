"""Board finishing tools (MCP_ISSUES Part 2 §2.7: F1, F2, F4).

* ``stitch_ground``    -- stitching grid, edge ring and via fences for a pour net.
* ``tidy_silkscreen``  -- re-place / resize reference designators using stroke geometry.
* ``add_pin_labels``   -- silk labels beside connector pins.
* ``cleanup_dangling`` -- trim dangling track stubs back to their junction, drop
                          orphan vias, verified against KiCad connectivity.

Reference prototypes: ``finish_pours.py``, ``via_fence.py``, ``silk_place.py``,
``silk_labels.py`` in the CD_Mainboard project.

Every tool works on the loaded board in memory, supports ``dryRun`` (computes
the full result, then restores the board) and restores the board if anything
fails part-way. A successful non-dry run is auto-saved through the interface's
guarded ``_auto_save_board`` (backup + external-change check), the same path
other board mutations use.

Units: millimetres in, millimetres out; board coordinates (origin top-left,
y down).
"""

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

import pcbnew

from commands.finishing_geom import (
    BBox,
    ParamError,
    ShapeIndex,
    as_obstacle,
    board_outline,
    circle_obstacle,
    disk_hits,
    obstacle_box,
    segment_obstacle,
    box_of,
    copper_layers,
    grow,
    in_box,
    inset_poly,
    is_arc,
    is_track,
    is_via,
    layer_id,
    layer_name,
    netclass_clearance_fn,
    no_board,
    num,
    pad_layers,
    parse_point,
    parse_region,
    point_seg_dist,
    poly_contains,
    poly_rings,
    project_t,
    resolve_net,
    restore_fills,
    seg_intersection_t,
    snapshot_fills,
    shape_box,
    to_mm,
    to_nm,
    vec,
    via_width,
    walk_polyline,
    walk_ring_evenly,
    zone_copper_layers,
)
from utils.board_items import delete_board_item, detach_board_item

logger = logging.getLogger("kicad_interface")

GND_FALLBACKS = ("GND", "GROUND", "VSS", "/GND", "AGND")


def _fp_box(fp: Any) -> BBox:
    """Footprint bounding box without its texts (signature differs across KiCad versions)."""
    try:
        return box_of(fp.GetBoundingBox(False))
    except TypeError:
        return box_of(fp.GetBoundingBox(False, False))


class FinishingCommands:
    """Finishing tools. Holds the interface so it always sees the current board."""

    def __init__(self, interface: Any = None) -> None:
        self.interface = interface

    @property
    def board(self) -> Any:
        return getattr(self.interface, "board", None)

    # ------------------------------------------------------------------ #
    # shared plumbing
    # ------------------------------------------------------------------ #

    def _persist(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-save after a committed mutation (guarded, with backup)."""
        saver = getattr(self.interface, "_auto_save_board", None)
        if not callable(saver):
            result["saved"] = False
            return result
        try:
            status = saver()
        except Exception as e:  # never turn a committed edit into a failure
            logger.warning(f"auto-save after finishing tool failed: {e}")
            status = {"saved": False, "error": str(e)}
        try:
            self.interface._last_auto_save_status = status
        except Exception:
            pass
        result["saved"] = bool(status.get("saved"))
        if not status.get("saved"):
            if status.get("warning"):
                result.setdefault("warnings", []).append(status["warning"])
            result["autoSave"] = status
        return result

    @staticmethod
    def _failure(name: str, err: Exception) -> Dict[str, Any]:
        if isinstance(err, ParamError):
            return err.response()
        import traceback

        logger.error(f"{name} failed: {err}\n{traceback.format_exc()}")
        return {"success": False, "message": f"{name} failed", "errorDetails": str(err)}

    # ================================================================== #
    # F1 stitch_ground
    # ================================================================== #

    def stitch_ground(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Stitching grid + edge ring + via fences for one pour net (default GND).

        Every candidate via is checked against all copper of other nets on
        every layer (net-class clearance + ``keepClear``), hole-to-hole
        spacing, rule areas that forbid vias, courtyards (per
        ``courtyardPolicy``), the board edge, and is only placed where the
        net's *filled* pour covers the whole via on at least ``minLayers``
        copper layers (and on every layer in ``requireLayers``).
        """
        if not self.board:
            return no_board()
        try:
            return self._stitch_ground(params or {})
        except Exception as e:
            return self._failure("stitch_ground", e)

    def _stitch_ground(self, params: Dict[str, Any]) -> Dict[str, Any]:
        board = self.board
        net, net_name = resolve_net(board, params.get("net"), GND_FALLBACKS)
        netcode = net.GetNetCode()

        via_spec = params.get("via") or {}
        if not isinstance(via_spec, dict):
            raise ParamError("'via' must be an object", "e.g. {size: 0.6, drill: 0.3} in mm")
        via_size = to_nm(num(via_spec, "size", 0.6))
        via_drill = to_nm(num(via_spec, "drill", 0.3))
        if via_drill <= 0 or via_drill >= via_size:
            raise ParamError("Invalid via geometry", "via.drill must be > 0 and < via.size")
        r = via_size // 2

        grid = num(params, "grid", 2.5)
        edge_pitch = num(params, "edgePitch", 2.0)
        fence_pitch = num(params, "fencePitch", 1.5)
        keep_clear = to_nm(num(params, "keepClear", 0.1))
        fence_nudge = to_nm(num(params, "fenceNudge", 0.4))
        min_layers = int(num(params, "minLayers", 2))
        policy = str(params.get("courtyardPolicy", "fence_allowed"))
        if policy not in ("avoid", "allow", "fence_allowed"):
            raise ParamError(
                "Invalid courtyardPolicy", "use 'avoid', 'allow' or 'fence_allowed' (default)"
            )
        lock = bool(params.get("lock", True))
        refill = bool(params.get("refill", True))
        dry_run = bool(params.get("dryRun", False))
        limit = int(num(params, "limit", 100))
        region = parse_region(params.get("region"))
        fences = self._parse_fences(board, params.get("fencePolylines"))

        outline = board_outline(board)
        if outline is None:
            raise ParamError("Board outline is missing or not closed", "define Edge.Cuts first")

        ds = board.GetDesignSettings()
        edge_clr = int(getattr(ds, "m_CopperEdgeClearance", 0) or 0)
        hole_clr = int(getattr(ds, "m_HoleToHoleMin", 0) or 0)
        nc_clr = netclass_clearance_fn(board)
        own_clr = nc_clr(net_name)
        min_spacing_default = max(via_size + own_clr, via_drill + hole_clr) / 1e6
        min_spacing = to_nm(num(params, "minSpacing", min_spacing_default))
        edge_inset_default = (edge_clr + r + to_nm(0.25)) / 1e6
        edge_inset = to_nm(num(params, "edgeInset", edge_inset_default))

        coppers = copper_layers(board)
        require_layers = [layer_id(board, n) for n in (params.get("requireLayers") or [])]

        # ---- pours of this net, per copper layer ---------------------------
        own_zones = [
            z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() == netcode
        ]
        if not own_zones:
            raise ParamError(
                f"Net '{net_name}' has no copper zones",
                "add pours first (add_copper_pour / add_zone); stitching vias are only "
                "placed inside the net's filled pours",
            )
        # Snapshot fills first: a dry run or a failure must leave them as found.
        fill_snap = snapshot_fills(board)
        filled_before = False
        if any(not z.IsFilled() for z in own_zones):
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
            filled_before = True
        fills: Dict[int, List[Any]] = {}
        for z in own_zones:
            for lid in zone_copper_layers(z):
                poly = z.GetFilledPolysList(lid)
                if poly is None or poly.OutlineCount() == 0:
                    continue
                poly = pcbnew.SHAPE_POLY_SET(poly)
                try:
                    poly.BuildBBoxCaches()
                except Exception:
                    pass
                fills.setdefault(lid, []).append(poly)
        pour_layers = sorted(fills)
        need = max(min_layers, 1)

        # ---- obstacles -------------------------------------------------------
        copper_idx = ShapeIndex()  # payload: (obstacle, clearance)
        hole_idx = ShapeIndex()  # payload: obstacle of a drilled hole
        same_vias = ShapeIndex()  # payload: (x, y)
        keepouts: List[Tuple[BBox, Any]] = []
        courtyards: List[Tuple[BBox, Any]] = []

        def clr_to(other_net: str) -> int:
            return max(own_clr, nc_clr(other_net)) + keep_clear

        def add_copper(ob: Tuple[Any, ...], clr: int) -> None:
            copper_idx.add(grow(obstacle_box(ob), clr), (ob, clr))

        def add_hole(ob: Tuple[Any, ...]) -> None:
            hole_idx.add(grow(obstacle_box(ob), hole_clr), ob)

        for t in board.GetTracks():
            if is_via(t):
                pos = t.GetPosition()
                if t.GetNetCode() == netcode:
                    same_vias.add((pos.x, pos.y, pos.x, pos.y), (pos.x, pos.y))
                else:
                    add_copper(
                        circle_obstacle(pos.x, pos.y, via_width(t) // 2), clr_to(t.GetNetname())
                    )
                add_hole(circle_obstacle(pos.x, pos.y, int(t.GetDrillValue()) // 2))
            elif t.GetNetCode() != netcode:
                if is_arc(t):
                    ob = as_obstacle(t.GetEffectiveShape(t.GetLayer()))
                else:
                    ob = segment_obstacle(t.GetStart(), t.GetEnd(), t.GetWidth() / 2)
                add_copper(ob, clr_to(t.GetNetname()))

        for fp in board.GetFootprints():
            for pad in fp.Pads():
                # Same-net pads are obstacles too: no via-in-pad from a stitcher.
                clr = clr_to(pad.GetNetname()) if pad.GetNetCode() != netcode else own_clr
                on = pad_layers(pad, coppers)
                for lid in sorted(set(on[:1] + on[-1:])):
                    add_copper(as_obstacle(pad.GetEffectiveShape(lid)), clr)
                if pad.HasHole():
                    add_hole(as_obstacle(pad.GetEffectiveHoleShape()))
            if policy != "allow":
                for crt in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
                    poly = fp.GetCourtyard(crt)
                    if poly is not None and poly.OutlineCount() > 0:
                        courtyards.append((box_of(poly.BBox()), poly))

        for z in board.Zones():
            if z.GetIsRuleArea() and z.GetDoNotAllowVias():
                poly = z.Outline()
                keepouts.append((box_of(poly.BBox()), poly))

        edge_ok_poly = inset_poly(outline, edge_clr + r)

        # ---- candidates ------------------------------------------------------
        cands: List[Tuple[str, float, float, List[Tuple[float, float]]]] = []
        for poly_pts, closed in fences:
            for x, y, nx, ny in walk_polyline(poly_pts, to_nm(fence_pitch), closed):
                nudges = [(0.0, 0.0)]
                if fence_nudge:
                    nudges += [(nx * fence_nudge, ny * fence_nudge)]
                    nudges += [(-nx * fence_nudge, -ny * fence_nudge)]
                cands.append(("fence", x, y, nudges))
        if edge_pitch and edge_pitch > 0:
            ring_poly = inset_poly(outline, edge_inset)
            for ring in poly_rings(ring_poly):
                for x, y in walk_ring_evenly(ring, to_nm(edge_pitch)):
                    cands.append(("edge", x, y, [(0.0, 0.0)]))
        if grid and grid > 0:
            g = to_nm(grid)
            bb = box_of(outline.BBox())
            y = bb[1] + g / 2
            while y < bb[3]:
                x = bb[0] + g / 2
                while x < bb[2]:
                    cands.append(("grid", x, y, [(0.0, 0.0)]))
                    x += g
                y += g
        if not cands:
            raise ParamError(
                "Nothing to place", "give grid, edgePitch and/or fencePolylines (all mm, > 0)"
            )

        max_copper_clr = max([p[1] for _, p in copper_idx.items] or [0])
        ring = [(math.cos(a * math.pi / 4), math.sin(a * math.pi / 4)) for a in range(8)]

        def pour_layers_at(x: float, y: float) -> List[int]:
            hits = []
            for lid, polys in fills.items():
                for poly in polys:
                    if poly_contains(poly, x, y) and all(
                        poly_contains(poly, x + cx * r, y + cy * r) for cx, cy in ring
                    ):
                        hits.append(lid)
                        break
            return hits

        def check(kind: str, x: float, y: float) -> Optional[str]:
            if region is not None and not in_box(int(x), int(y), region):
                return "outsideRegion"
            if not poly_contains(edge_ok_poly, x, y):
                return "edge"
            sp = min_spacing
            for ox, oy in same_vias.query((int(x - sp), int(y - sp), int(x + sp), int(y + sp))):
                if math.hypot(ox - x, oy - y) < sp:
                    return "spacing"
            if policy == "avoid" or (policy == "fence_allowed" and kind != "fence"):
                for box, poly in courtyards:
                    if box[0] - r <= x <= box[2] + r and box[1] - r <= y <= box[3] + r:
                        if poly.Collide(vec(x, y), r):
                            return "courtyard"
            for box, poly in keepouts:
                if box[0] - r <= x <= box[2] + r and box[1] - r <= y <= box[3] + r:
                    if poly.Collide(vec(x, y), r):
                        return "keepout"
            have = pour_layers_at(x, y)
            if len(have) < need or any(lid not in have for lid in require_layers):
                return "noPour"
            qbox = grow((int(x), int(y), int(x), int(y)), r + max_copper_clr)
            for ob, clr in copper_idx.query(qbox):
                if disk_hits(ob, x, y, r, clr):
                    return "collision"
            hbox = grow((int(x), int(y), int(x), int(y)), via_drill // 2 + hole_clr)
            for ob in hole_idx.query(hbox):
                if disk_hits(ob, x, y, via_drill // 2, hole_clr):
                    return "holeClearance"
            return None

        placed: List[Tuple[str, int, int]] = []
        rejected: Dict[str, int] = {}
        counts = {"fence": 0, "edge": 0, "grid": 0}
        tried = {"fence": 0, "edge": 0, "grid": 0}
        for kind, x, y, nudges in cands:
            tried[kind] += 1
            reason = None
            for dx, dy in nudges:
                why = check(kind, x + dx, y + dy)
                if why is None:
                    px, py = int(round(x + dx)), int(round(y + dy))
                    placed.append((kind, px, py))
                    same_vias.add((px, py, px, py), (px, py))
                    counts[kind] += 1
                    reason = None
                    break
                reason = reason or why
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1

        result: Dict[str, Any] = {
            "success": True,
            "dryRun": dry_run,
            "net": net_name,
            "unit": "mm",
            "via": {"size": to_mm(via_size), "drill": to_mm(via_drill)},
            "clearance": to_mm(own_clr + keep_clear),
            "minSpacing": to_mm(min_spacing),
            "pourLayers": [layer_name(board, lid) for lid in pour_layers],
            "minLayers": need,
            "placed": dict(counts, total=len(placed)),
            "candidates": tried,
            "rejected": rejected,
            "vias": [
                {"x": to_mm(px, 3), "y": to_mm(py, 3), "kind": kind}
                for kind, px, py in placed[: max(limit, 0)]
            ],
            "viasTruncated": len(placed) > max(limit, 0),
            "zonesFilledBefore": filled_before,
        }
        if not placed and len(pour_layers) < need:
            result.setdefault("warnings", []).append(
                f"'{net_name}' is poured on {len(pour_layers)} layer(s) but minLayers={need}; "
                "lower minLayers or add pours"
            )
        if dry_run or not placed:
            if filled_before:
                restore_fills(fill_snap)
            return result

        added: List[Any] = []
        try:
            f_cu, b_cu = pcbnew.F_Cu, pcbnew.B_Cu
            for kind, px, py in placed:
                via = pcbnew.PCB_VIA(board)
                via.SetPosition(pcbnew.VECTOR2I(px, py))
                via.SetWidth(via_size)
                via.SetDrill(via_drill)
                via.SetLayerPair(f_cu, b_cu)
                via.SetNet(net)
                via.SetLocked(lock)
                board.Add(via)
                added.append(via)
            if refill:
                pcbnew.ZONE_FILLER(board).Fill(board.Zones())
                result["zonesRefilled"] = True
        except Exception:
            for via in added:
                try:
                    delete_board_item(board, via)
                except Exception:
                    pass
            restore_fills(fill_snap)
            raise
        return self._persist(result)

    @staticmethod
    def _parse_fences(board: Any, raw: Any) -> List[Tuple[List[Tuple[float, float]], bool]]:
        """fencePolylines: list of point lists, ``{points, closed}`` or ``{zoneName}``."""
        if not raw:
            return []
        if not isinstance(raw, list):
            raise ParamError("'fencePolylines' must be a list")
        out: List[Tuple[List[Tuple[float, float]], bool]] = []
        for i, entry in enumerate(raw):
            closed = False
            if isinstance(entry, dict) and entry.get("zoneName"):
                name = str(entry["zoneName"])
                zones = [z for z in board.Zones() if z.GetZoneName() == name]
                if not zones:
                    raise ParamError(f"fencePolylines[{i}]: no zone named '{name}'")
                outline = zones[0].Outline()
                for ring in poly_rings(outline)[:1]:
                    out.append(([(float(x), float(y)) for x, y in ring], True))
                continue
            if isinstance(entry, dict):
                closed = bool(entry.get("closed", False))
                entry = entry.get("points")
            if not isinstance(entry, list) or len(entry) < 2:
                raise ParamError(
                    f"fencePolylines[{i}] needs at least two points",
                    "each polyline is [[x, y], ...] or {points: [...], closed: bool} (mm)",
                )
            pts = [parse_point(p, f"fencePolylines[{i}]") for p in entry]
            out.append(([(to_nm(x), to_nm(y)) for x, y in pts], closed))
        return out

    # ================================================================== #
    # F2 tidy_silkscreen / add_pin_labels
    # ================================================================== #

    def tidy_silkscreen(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Re-place (and optionally resize) reference designators on the silkscreen."""
        if not self.board:
            return no_board()
        try:
            return self._tidy_silkscreen(params or {})
        except Exception as e:
            return self._failure("tidy_silkscreen", e)

    def add_pin_labels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Silk text labels beside chosen pads of one footprint (e.g. a connector)."""
        if not self.board:
            return no_board()
        try:
            return self._add_pin_labels(params or {})
        except Exception as e:
            return self._failure("add_pin_labels", e)

    # ---- silk collision model --------------------------------------------

    class _Silk:
        """Static silk obstacles for one board side, tested with stroke geometry."""

        def __init__(
            self,
            board: Any,
            back: bool,
            gap: int,
            edge_clearance: int,
            avoid_courtyards: bool,
            skip_text: Callable[[Any], bool],
        ) -> None:
            self.back = back
            self.silk = pcbnew.B_SilkS if back else pcbnew.F_SilkS
            mask = pcbnew.B_Mask if back else pcbnew.F_Mask
            cu = pcbnew.B_Cu if back else pcbnew.F_Cu
            crt = pcbnew.B_CrtYd if back else pcbnew.F_CrtYd
            self.gap = gap
            self.index = ShapeIndex()  # payload: (kind, shape, owner, clearance)
            outline = board_outline(board)
            self.edge = inset_poly(outline, edge_clearance) if outline is not None else None

            def add(kind: str, shape: Any, owner: str, clr: int) -> None:
                try:
                    box = grow(shape_box(shape), clr)
                except Exception:
                    return
                self.index.add(box, (kind, shape, owner, clr))

            for fp in board.GetFootprints():
                ref = fp.GetReference()
                for pad in fp.Pads():
                    if pad.IsOnLayer(mask):
                        add("pad", pad.GetEffectiveShape(mask), ref, gap)
                    elif pad.IsOnLayer(cu):
                        add("pad", pad.GetEffectiveShape(cu), ref, gap)
                for g in fp.GraphicalItems():
                    if g.GetLayer() != self.silk:
                        continue
                    if isinstance(g, pcbnew.PCB_TEXT) and (not g.IsVisible() or skip_text(g)):
                        continue
                    add("silk", g.GetEffectiveShape(self.silk), ref, gap)
                for f in fp.GetFields():
                    if f.GetLayer() == self.silk and f.IsVisible() and not skip_text(f):
                        add("text", f.GetEffectiveShape(self.silk), ref, gap)
                if avoid_courtyards:
                    poly = fp.GetCourtyard(crt)
                    if poly is not None and poly.OutlineCount() > 0:
                        add("courtyard", poly, ref, 0)
            for d in board.GetDrawings():
                if d.GetLayer() != self.silk:
                    continue
                if isinstance(d, pcbnew.PCB_TEXT) and not d.IsVisible():
                    continue
                try:
                    add("silk", d.GetEffectiveShape(self.silk), "", gap)
                except Exception:
                    continue
            for t in board.GetTracks():
                if is_via(t):
                    try:
                        tented = t.IsTented(mask)
                    except Exception:
                        tented = True
                    if not tented:
                        add("pad", t.GetEffectiveShape(cu), "", gap)

        def collision(self, shape: Any, owner: str) -> Optional[str]:
            box = shape_box(shape)
            if self.edge is not None:
                for x, y in (
                    (box[0], box[1]),
                    (box[2], box[1]),
                    (box[0], box[3]),
                    (box[2], box[3]),
                ):
                    if not poly_contains(self.edge, x, y):
                        return "edge"
            for kind, other, other_owner, clr in self.index.query(grow(box, self.gap)):
                if kind == "courtyard" and other_owner == owner:
                    continue
                # Polygon sets (courtyards) must be the receiver so their
                # interior counts; everything else is tested from the text.
                hit = (
                    other.Collide(shape, clr) if kind == "courtyard" else shape.Collide(other, clr)
                )
                if hit:
                    return kind
            return None

        def add_text(self, shape: Any, owner: str) -> None:
            self.index.add(grow(shape_box(shape), self.gap), ("text", shape, owner, self.gap))

    @staticmethod
    def _text_state(t: Any) -> Dict[str, Any]:
        # Copy vectors: SWIG may hand back live references (see GetStart()).
        pos, size = t.GetPosition(), t.GetTextSize()
        return {
            "pos": pcbnew.VECTOR2I(int(pos.x), int(pos.y)),
            "angle": t.GetTextAngleDegrees(),
            "size": pcbnew.VECTOR2I(int(size.x), int(size.y)),
            "thick": t.GetTextThickness(),
            "visible": t.IsVisible(),
            "upright": t.IsKeepUpright() if hasattr(t, "IsKeepUpright") else None,
            "hj": t.GetHorizJustify(),
            "vj": t.GetVertJustify(),
        }

    @staticmethod
    def _restore_text(t: Any, s: Dict[str, Any]) -> None:
        t.SetTextSize(s["size"])
        t.SetTextThickness(s["thick"])
        t.SetHorizJustify(s["hj"])
        t.SetVertJustify(s["vj"])
        t.SetTextAngleDegrees(s["angle"])
        t.SetPosition(s["pos"])
        t.SetVisible(s["visible"])
        if s["upright"] is not None and hasattr(t, "SetKeepUpright"):
            t.SetKeepUpright(s["upright"])

    def _tidy_silkscreen(self, params: Dict[str, Any]) -> Dict[str, Any]:
        board = self.board
        height = num(params, "textHeight", None)
        width = num(params, "textWidth", None)
        thickness = num(params, "textThickness", None)
        if height is not None and height <= 0:
            raise ParamError("'textHeight' must be > 0 (mm)")
        if width is None and height is not None:
            width = height
        if thickness is None and height is not None:
            thickness = max(0.1, round(height * 0.15 / 0.8, 3))
        keep_upright = bool(params.get("keepUpright", True))
        hide = bool(params.get("hideIfNoFit", True))
        move_all = bool(params.get("moveAll", False))
        include_hidden = bool(params.get("includeHidden", False))
        gap = to_nm(num(params, "gap", 0.15))
        edge_clr = to_nm(num(params, "edgeClearance", 0.25))
        large_area = num(params, "largePartArea", 25.0)
        avoid_court = str(params.get("courtyards", "avoid")) != "ignore"
        dry_run = bool(params.get("dryRun", False))
        limit = int(num(params, "limit", 50))
        side_filter = str(params.get("side", "both"))
        if side_filter not in ("front", "back", "both"):
            raise ParamError("Invalid side", "use 'front', 'back' or 'both'")
        wanted = params.get("refs")
        wanted_set = {str(r) for r in wanted} if wanted else None

        fps_by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
        if wanted_set:
            missing = sorted(r for r in wanted_set if r not in fps_by_ref)
            if missing:
                raise ParamError("Unknown refs", ", ".join(missing))

        targets = []
        for fp in board.GetFootprints():
            ref_text = fp.Reference()
            lay = ref_text.GetLayer()
            if lay not in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                continue
            back = lay == pcbnew.B_SilkS
            if side_filter == "front" and back or side_filter == "back" and not back:
                continue
            if wanted_set is not None and fp.GetReference() not in wanted_set:
                continue
            if not ref_text.IsVisible() and not include_hidden:
                continue
            targets.append((fp, ref_text, back))

        target_ids = {id(t[1]) for t in targets}
        target_uuids = {t[1].m_Uuid.AsString() for t in targets}

        def skip_text(item: Any) -> bool:
            try:
                return item.m_Uuid.AsString() in target_uuids
            except Exception:
                return id(item) in target_ids

        sides = {}
        for back in (False, True):
            if any(t[2] == back for t in targets):
                sides[back] = self._Silk(board, back, gap, edge_clr, avoid_court, skip_text)

        def court_box(fp: Any, back: bool) -> BBox:
            poly = fp.GetCourtyard(pcbnew.B_CrtYd if back else pcbnew.F_CrtYd)
            if poly is not None and poly.OutlineCount() > 0:
                return box_of(poly.BBox())
            return _fp_box(fp)

        def area(fp: Any, back: bool) -> float:
            b = court_box(fp, back)
            return (b[2] - b[0]) * (b[3] - b[1])

        targets.sort(key=lambda t: -area(t[0], t[2]))
        saved = [(t[1], self._text_state(t[1])) for t in targets]
        report = {"kept": 0, "moved": 0, "hidden": [], "unresolved": [], "resized": 0}
        moves: List[Dict[str, Any]] = []
        colliding_before = 0
        try:
            for fp, text, back in targets:
                silk = sides[back]
                ref = fp.GetReference()
                state = self._text_state(text)
                if height is not None:
                    text.SetTextSize(pcbnew.VECTOR2I(to_nm(width), to_nm(height)))
                    text.SetTextThickness(to_nm(thickness))
                    report["resized"] += 1
                if keep_upright and hasattr(text, "SetKeepUpright"):
                    text.SetKeepUpright(True)
                if include_hidden:
                    text.SetVisible(True)
                before = silk.collision(text.GetEffectiveShape(silk.silk), ref)
                if before:
                    colliding_before += 1
                if before is None and not move_all and state["visible"]:
                    report["kept"] += 1
                    silk.add_text(text.GetEffectiveShape(silk.silk), ref)
                    continue
                text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
                text.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
                spot = self._place_text(text, silk, ref, court_box(fp, back), large_area)
                if spot is not None:
                    report["moved"] += 1
                    if len(moves) < limit:
                        moves.append(
                            {
                                "ref": ref,
                                "from": {
                                    "x": to_mm(state["pos"].x, 3),
                                    "y": to_mm(state["pos"].y, 3),
                                    "angle": round(state["angle"], 1),
                                },
                                "to": {
                                    "x": to_mm(spot[0], 3),
                                    "y": to_mm(spot[1], 3),
                                    "angle": spot[2],
                                },
                            }
                        )
                    silk.add_text(text.GetEffectiveShape(silk.silk), ref)
                    continue
                if hide:
                    self._restore_text(text, state)
                    if height is not None:
                        text.SetTextSize(pcbnew.VECTOR2I(to_nm(width), to_nm(height)))
                        text.SetTextThickness(to_nm(thickness))
                    text.SetVisible(False)
                    report["hidden"].append(ref)
                else:
                    self._restore_text(text, state)
                    if height is not None:
                        text.SetTextSize(pcbnew.VECTOR2I(to_nm(width), to_nm(height)))
                        text.SetTextThickness(to_nm(thickness))
                    report["unresolved"].append(ref)
                    if text.IsVisible():
                        silk.add_text(text.GetEffectiveShape(silk.silk), ref)
        except Exception:
            for t, s in saved:
                self._restore_text(t, s)
            raise

        result: Dict[str, Any] = {
            "success": True,
            "dryRun": dry_run,
            "unit": "mm",
            "processed": len(targets),
            "collidingBefore": colliding_before,
            "kept": report["kept"],
            "moved": report["moved"],
            "resized": report["resized"],
            "hidden": sorted(report["hidden"]),
            "unresolved": sorted(report["unresolved"]),
            "moves": moves,
            "movesTruncated": report["moved"] > len(moves),
        }
        if height is not None:
            result["textSize"] = {"height": height, "width": width, "thickness": thickness}
        changed = report["moved"] or report["hidden"] or report["resized"] or include_hidden
        if dry_run:
            for t, s in saved:
                self._restore_text(t, s)
            return result
        if not changed:
            return result
        return self._persist(result)

    @staticmethod
    def _place_text(
        text: Any, silk: "_Silk", owner: str, court: BBox, large_area: float
    ) -> Optional[Tuple[int, int, float]]:
        """Try candidate spots around ``court``; leave the text at the first clear one."""
        text.SetTextAngleDegrees(0)
        text.SetPosition(pcbnew.VECTOR2I(0, 0))
        b0 = shape_box(text.GetEffectiveShape(silk.silk))
        tw, th = b0[2] - b0[0], b0[3] - b0[1]
        x0, y0, x1, y1 = court
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        cands: List[Tuple[float, float, float]] = []
        if (x1 - x0) * (y1 - y0) / 1e12 >= large_area:
            cands += [(cx, cy, 0), (cx, cy, 90)]
        for d in (to_nm(0.15), to_nm(0.5), to_nm(1.0), to_nm(1.6)):
            above, below = y0 - d - th / 2, y1 + d + th / 2
            cands += [
                (cx, above, 0),
                (cx, below, 0),
                (x0 - d - th / 2, cy, 90),
                (x1 + d + th / 2, cy, 90),
                (x0 - d - tw / 2, cy, 0),
                (x1 + d + tw / 2, cy, 0),
                (x0 + tw / 2, above, 0),
                (x1 - tw / 2, above, 0),
                (x0 + tw / 2, below, 0),
                (x1 - tw / 2, below, 0),
                (x0 - d - th / 2, y0 + tw / 2, 90),
                (x1 + d + th / 2, y1 - tw / 2, 90),
            ]
        for x, y, ang in cands:
            text.SetTextAngleDegrees(ang)
            text.SetPosition(vec(x, y))
            if silk.collision(text.GetEffectiveShape(silk.silk), owner) is None:
                return int(round(x)), int(round(y)), float(ang)
        return None

    def _add_pin_labels(self, params: Dict[str, Any]) -> Dict[str, Any]:
        board = self.board
        ref = params.get("ref") or params.get("reference")
        if not ref:
            raise ParamError("'ref' is required", "the connector's reference, e.g. J3")
        fp = board.FindFootprintByReference(str(ref))
        if fp is None or not isinstance(fp, pcbnew.FOOTPRINT):
            raise ParamError(f"Footprint '{ref}' not found")
        labels = params.get("labels")
        if not isinstance(labels, dict) or not labels:
            raise ParamError("'labels' must be a non-empty object", 'e.g. {"1": "5V", "2": "GND"}')
        side = str(params.get("side", "auto"))
        if side not in ("auto", "left", "right", "top", "bottom"):
            raise ParamError("Invalid side", "use auto, left, right, top or bottom")
        height = to_nm(num(params, "textHeight", 1.0))
        thick = to_nm(num(params, "textThickness", max(0.1, round(height / 1e6 * 0.15, 3))))
        gap = to_nm(num(params, "gap", 0.15))
        offset = to_nm(num(params, "offset", 0.3))
        edge_clr = to_nm(num(params, "edgeClearance", 0.25))
        dry_run = bool(params.get("dryRun", False))
        back = fp.IsFlipped()
        silk_layer = pcbnew.B_SilkS if back else pcbnew.F_SilkS
        if params.get("layer"):
            silk_layer = layer_id(board, params["layer"])
            back = silk_layer == pcbnew.B_SilkS

        pads: Dict[str, List[Any]] = {}
        for pad in fp.Pads():
            pads.setdefault(str(pad.GetNumber()), []).append(pad)
        unknown = sorted(str(k) for k in labels if str(k) not in pads)
        if unknown:
            raise ParamError(
                f"Pads not found on {ref}: {', '.join(unknown)}",
                f"pads: {', '.join(sorted(pads))}",
            )

        # Courtyards (the connector's own included) are obstacles by default:
        # silk inside a part's body is hidden under it once assembled.
        avoid_body = bool(params.get("avoidBody", True))
        silk = self._Silk(board, back, gap, edge_clr, avoid_body, lambda _t: False)
        silk.silk = silk_layer
        xs = [p.GetPosition().x for p in fp.Pads()]
        ys = [p.GetPosition().y for p in fp.Pads()]
        pcx, pcy = sum(xs) / len(xs), sum(ys) / len(ys)
        spread_x, spread_y = max(xs) - min(xs), max(ys) - min(ys)
        court = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        if court is not None and court.OutlineCount() > 0:
            cb = box_of(court.BBox())
        else:
            cb = _fp_box(fp)
        body_cx, body_cy = (cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2

        def directions(pad: Any) -> List[str]:
            if side != "auto":
                order = [side]
                opposite = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
                order += [opposite[side]]
                order += [d for d in ("left", "right", "top", "bottom") if d not in order]
                return order
            if spread_y >= spread_x:  # pins in a column: label left/right
                first = "left" if body_cx > pcx else "right"
                if abs(body_cx - pcx) < to_nm(0.2):
                    first = "right"
                second = "left" if first == "right" else "right"
                return [first, second, "top", "bottom"]
            first = "top" if body_cy > pcy else "bottom"
            if abs(body_cy - pcy) < to_nm(0.2):
                first = "top"
            second = "bottom" if first == "top" else "top"
            return [first, second, "left", "right"]

        placed: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        created: List[Any] = []
        try:
            for pad_num, label in labels.items():
                pad = pads[str(pad_num)][0]
                pb = box_of(pad.GetBoundingBox())
                px, py = (pb[0] + pb[2]) / 2, (pb[1] + pb[3]) / 2
                t = pcbnew.PCB_TEXT(board)
                t.SetText(str(label))
                t.SetLayer(silk_layer)
                t.SetTextSize(pcbnew.VECTOR2I(height, height))
                t.SetTextThickness(thick)
                t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
                t.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
                if back:
                    t.SetMirrored(True)
                if hasattr(t, "SetKeepUpright"):
                    t.SetKeepUpright(True)
                t.SetPosition(pcbnew.VECTOR2I(0, 0))
                b0 = shape_box(t.GetEffectiveShape(silk_layer))
                tw, th = b0[2] - b0[0], b0[3] - b0[1]
                found = None
                for direction in directions(pad):
                    # Horizontal text first at growing offsets, then rotated.
                    beyond = {
                        "left": pb[0] - cb[0],
                        "right": cb[2] - pb[2],
                        "top": pb[1] - cb[1],
                        "bottom": cb[3] - pb[3],
                    }[direction]
                    extras = [0, to_nm(0.4), to_nm(0.9)]
                    if beyond > 0:
                        extras += [beyond, beyond + to_nm(0.4), beyond + to_nm(0.9)]
                    for ang in (0, 90):
                        for extra in extras:
                            d = offset + extra
                            along = tw / 2 if ang == 0 else th / 2  # extent along x
                            across = th / 2 if ang == 0 else tw / 2  # extent along y
                            if direction in ("left", "right"):
                                sgn = -1 if direction == "left" else 1
                                edge = pb[0] if sgn < 0 else pb[2]
                                x, y = edge + sgn * (d + along), py
                            else:
                                sgn = -1 if direction == "top" else 1
                                edge = pb[1] if sgn < 0 else pb[3]
                                x, y = px, edge + sgn * (d + across)
                            t.SetTextAngleDegrees(ang)
                            t.SetPosition(vec(x, y))
                            if silk.collision(t.GetEffectiveShape(silk_layer), "") is None:
                                found = (x, y, ang, direction)
                                break
                        if found:
                            break
                    if found:
                        break
                if not found:
                    skipped.append(
                        {"pad": str(pad_num), "text": str(label), "reason": "no clear spot"}
                    )
                    continue
                silk.add_text(t.GetEffectiveShape(silk_layer), "")
                placed.append(
                    {
                        "pad": str(pad_num),
                        "text": str(label),
                        "x": to_mm(found[0], 3),
                        "y": to_mm(found[1], 3),
                        "angle": found[2],
                        "side": found[3],
                    }
                )
                if not dry_run:
                    board.Add(t)
                    created.append(t)
        except Exception:
            for t in created:
                try:
                    delete_board_item(board, t)
                except Exception:
                    pass
            raise
        result = {
            "success": True,
            "dryRun": dry_run,
            "unit": "mm",
            "ref": str(ref),
            "layer": layer_name(board, silk_layer),
            "placed": placed,
            "skipped": skipped,
        }
        if dry_run or not created:
            return result
        return self._persist(result)

    # ================================================================== #
    # F4 cleanup_dangling
    # ================================================================== #

    def cleanup_dangling(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Trim dangling stubs back to their junction and remove orphan vias, safely."""
        if not self.board:
            return no_board()
        try:
            return self._cleanup_dangling(params or {})
        except Exception as e:
            return self._failure("cleanup_dangling", e)

    @staticmethod
    def _pad_groups(board: Any, conn: Any) -> Dict[str, int]:
        """Net name -> number of separately connected pad groups (1 = fully connected)."""
        pads_by_net: Dict[int, List[Any]] = {}
        names: Dict[int, str] = {}
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                nc = pad.GetNetCode()
                if nc > 0:
                    pads_by_net.setdefault(nc, []).append(pad)
                    names[nc] = pad.GetNetname()
        out: Dict[str, int] = {}
        for nc, pads in pads_by_net.items():
            seen = set()
            groups = 0
            for pad in pads:
                key = pad.m_Uuid.AsString()
                if key in seen:
                    continue
                groups += 1
                seen.add(key)
                for item in conn.GetConnectedItems(pad):
                    if isinstance(item, pcbnew.PAD):
                        seen.add(item.m_Uuid.AsString())
            out[names[nc]] = groups
        return out

    def _cleanup_dangling(self, params: Dict[str, Any]) -> Dict[str, Any]:
        board = self.board
        nets = params.get("nets")
        net_filter = {str(n) for n in nets} if nets else None
        region = parse_region(params.get("region"))
        dry_run = bool(params.get("dryRun", False))
        remove_vias = bool(params.get("removeVias", True))
        max_iter = int(num(params, "maxIterations", 50))
        limit = int(num(params, "limit", 50))
        if net_filter:
            unknown = sorted(n for n in net_filter if board.FindNet(n) is None)
            if unknown:
                raise ParamError("Unknown nets", ", ".join(unknown))

        board.BuildConnectivity()
        conn = board.GetConnectivity()
        groups_before = self._pad_groups(board, conn)
        unconnected_before = int(conn.GetUnconnectedCount(False))

        def in_scope(item: Any) -> bool:
            if net_filter is not None and item.GetNetname() not in net_filter:
                return False
            if region is None:
                return True
            if is_via(item):
                p = item.GetPosition()
                return in_box(p.x, p.y, region)
            s, e = item.GetStart(), item.GetEnd()
            return in_box(s.x, s.y, region) or in_box(e.x, e.y, region)

        removed: List[Any] = []  # detached with Remove(); re-added on rollback
        trimmed: List[Tuple[Any, bool, Any]] = []  # (track, start?, old point)
        actions: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        touched = set()

        def describe(item: Any, action: str, **extra: Any) -> Dict[str, Any]:
            d: Dict[str, Any] = {"action": action, "net": item.GetNetname()}
            if is_via(item):
                p = item.GetPosition()
                d.update(type="via", x=to_mm(p.x, 3), y=to_mm(p.y, 3))
            else:
                s, e = item.GetStart(), item.GetEnd()
                d.update(
                    type="arc" if is_arc(item) else "track",
                    layer=layer_name(board, item.GetLayer()),
                    start={"x": to_mm(s.x, 3), "y": to_mm(s.y, 3)},
                    end={"x": to_mm(e.x, 3), "y": to_mm(e.y, 3)},
                )
            d.update(extra)
            return d

        def detach(item: Any) -> None:
            removed.append(detach_board_item(board, item))

        try:
            for _ in range(max(max_iter, 1)):
                changed = False
                board.BuildConnectivity()
                conn = board.GetConnectivity()
                net_items = self._net_items(board)
                for t in list(board.GetTracks()):
                    if not is_track(t) or not in_scope(t):
                        continue
                    st, en = t.GetStart(), t.GetEnd()
                    key = (t.m_Uuid.AsString(), st.x, st.y, en.x, en.y)
                    if key in touched:
                        continue
                    # ignoreTracksInPads=True is what DRC's track_dangling uses;
                    # the out-parameter names the dangling anchor.
                    free_pt = pcbnew.VECTOR2I(0, 0)
                    try:
                        dangling = conn.TestTrackEndpointDangling(t, True, free_pt)
                    except TypeError:
                        dangling, free_pt = conn.TestTrackEndpointDangling(t, True), None
                    if not dangling:
                        continue
                    free_xy = (free_pt.x, free_pt.y) if free_pt is not None else None
                    plan = self._trim_plan(board, t, net_items.get(t.GetNetCode(), []), free_xy)
                    touched.add(key)
                    if plan is None:
                        unresolved.append(describe(t, "unresolved", reason="no free end found"))
                        continue
                    kind, at_start, point = plan
                    if kind == "remove":
                        actions.append(describe(t, "removed"))
                        detach(t)
                    else:
                        # GetStart()/GetEnd() return live references: copy.
                        ref_pt = t.GetStart() if at_start else t.GetEnd()
                        old = pcbnew.VECTOR2I(int(ref_pt.x), int(ref_pt.y))
                        new_pt = pcbnew.VECTOR2I(int(point[0]), int(point[1]))
                        if at_start:
                            t.SetStart(new_pt)
                        else:
                            t.SetEnd(new_pt)
                        trimmed.append((t, at_start, old))
                        actions.append(
                            describe(
                                t,
                                "trimmed",
                                trimmedBy=to_mm(math.hypot(old.x - point[0], old.y - point[1]), 3),
                            )
                        )
                    changed = True
                if remove_vias:
                    for v in list(board.GetTracks()):
                        if not is_via(v) or not in_scope(v):
                            continue
                        why = self._orphan_via_reason(board, conn, v)
                        if why:
                            actions.append(describe(v, "removed", reason=why))
                            detach(v)
                            changed = True
                if not changed:
                    break

            board.BuildConnectivity()
            conn = board.GetConnectivity()
            groups_after = self._pad_groups(board, conn)
            unconnected_after = int(conn.GetUnconnectedCount(False))
            worse = {
                n: {"before": groups_before.get(n, 0) - 1, "after": c - 1}
                for n, c in groups_after.items()
                if c > groups_before.get(n, c)
            }
            rolled_back = bool(worse) or unconnected_after > unconnected_before
        except Exception:
            self._undo_cleanup(board, removed, trimmed)
            raise

        result: Dict[str, Any] = {
            "success": not rolled_back,
            "dryRun": dry_run,
            "unit": "mm",
            "removedTracks": sum(
                1 for a in actions if a["action"] == "removed" and a["type"] != "via"
            ),
            "trimmedTracks": sum(1 for a in actions if a["action"] == "trimmed"),
            "removedVias": sum(
                1 for a in actions if a["action"] == "removed" and a["type"] == "via"
            ),
            "unresolved": unresolved[:limit],
            "connectivity": {
                "unconnectedBefore": unconnected_before,
                "unconnectedAfter": unconnected_after,
                "netsWorse": worse,
            },
            "actions": actions[:limit],
            "actionsTruncated": len(actions) > limit,
        }
        if rolled_back or dry_run:
            self._undo_cleanup(board, removed, trimmed)
            board.BuildConnectivity()
            if rolled_back:
                result["rolledBack"] = True
                result["message"] = (
                    "Cleanup would have broken connectivity; every change was rolled back"
                )
            return result
        # Commit: hand detached items back to C++ and delete them properly
        # (dropping a Remove()d proxy lets Python free it -- see board_items).
        for item in removed:
            board.Add(item)
            delete_board_item(board, item)
        if not actions:
            return result
        return self._persist(result)

    @staticmethod
    def _undo_cleanup(board: Any, removed: List[Any], trimmed: List[Tuple[Any, bool, Any]]) -> None:
        for t, at_start, old in reversed(trimmed):
            if at_start:
                t.SetStart(old)
            else:
                t.SetEnd(old)
        for item in reversed(removed):
            board.Add(item)
        removed.clear()
        trimmed.clear()

    @staticmethod
    def _net_items(board: Any) -> Dict[int, List[Tuple[str, Any]]]:
        """Netcode -> [(kind, item)] for tracks, vias, pads and filled zones."""
        out: Dict[int, List[Tuple[str, Any]]] = {}
        for t in board.GetTracks():
            out.setdefault(t.GetNetCode(), []).append(("via" if is_via(t) else "track", t))
        for fp in board.GetFootprints():
            for p in fp.Pads():
                out.setdefault(p.GetNetCode(), []).append(("pad", p))
        for z in board.Zones():
            if not z.GetIsRuleArea():
                out.setdefault(z.GetNetCode(), []).append(("zone", z))
        return out

    @staticmethod
    def _touches_at(item_kind: str, item: Any, layer: int, x: float, y: float) -> bool:
        pt = vec(x, y)
        if item_kind == "zone":
            try:
                return bool(item.IsOnLayer(layer) and item.HitTestFilledArea(layer, pt, 0))
            except Exception:
                return False
        if not item.IsOnLayer(layer):
            return False
        return bool(item.GetEffectiveShape(layer).Collide(pt, 0))

    def _trim_plan(
        self,
        board: Any,
        track: Any,
        items: List[Tuple[str, Any]],
        free_xy: Optional[Tuple[int, int]] = None,
    ) -> Optional[Tuple[str, bool, Optional[Tuple[float, float]]]]:
        """Decide how to shorten a dangling track.

        Returns ``("remove", at_start, None)`` when nothing but the far end
        touches it, ``("trim", at_start, point)`` to move the free end back to
        the last junction, or None when no free end can be identified.
        """
        layer = track.GetLayer()
        s, e = track.GetStart(), track.GetEnd()
        hw = track.GetWidth() / 2
        me = track.m_Uuid.AsString()
        others = [(k, it) for k, it in items if not (k == "track" and it.m_Uuid.AsString() == me)]

        def connected(x: float, y: float) -> bool:
            for kind, it in others:
                if self._touches_at(kind, it, layer, x, y):
                    return True
                if kind == "track" and it.IsOnLayer(layer) and not is_arc(it):
                    for q in (it.GetStart(), it.GetEnd()):
                        if math.hypot(q.x - x, q.y - y) <= hw:
                            return True
            return False

        tol = 1000  # 1 um
        if free_xy is not None and math.hypot(free_xy[0] - s.x, free_xy[1] - s.y) <= tol:
            s_ok, e_ok = False, connected(e.x, e.y)
        elif free_xy is not None and math.hypot(free_xy[0] - e.x, free_xy[1] - e.y) <= tol:
            s_ok, e_ok = connected(s.x, s.y), False
        else:
            s_ok, e_ok = connected(s.x, s.y), connected(e.x, e.y)
        if s_ok and e_ok:
            return None
        if not s_ok and not e_ok:
            return ("remove", False, None)
        free_is_start = not s_ok
        fx, fy = (e.x, e.y) if free_is_start else (s.x, s.y)  # connected end F
        ex, ey = (s.x, s.y) if free_is_start else (e.x, e.y)  # free end E
        if is_arc(track):
            return (
                ("remove", free_is_start, None)
                if not self._has_mid_contact(track, others, layer)
                else None
            )
        length = math.hypot(ex - fx, ey - fy)
        if length <= 0:
            return ("remove", free_is_start, None)
        shape = track.GetEffectiveShape(layer)
        best = 0.0
        for kind, it in others:
            if kind == "zone":
                if not it.IsOnLayer(layer):
                    continue
                steps = max(2, int(length / to_nm(0.05)))
                for i in range(steps, -1, -1):  # from E towards F
                    t = i / steps
                    if it.HitTestFilledArea(layer, vec(fx + (ex - fx) * t, fy + (ey - fy) * t), 0):
                        best = max(best, t)
                        break
                continue
            if not it.IsOnLayer(layer):
                continue
            try:
                if not it.GetEffectiveShape(layer).Collide(shape, 0):
                    continue
            except Exception:
                continue
            if kind in ("via", "pad"):
                p = it.GetPosition()
                best = max(best, project_t(p.x, p.y, fx, fy, ex, ey))
                continue
            # another track touching ours
            ow = it.GetWidth() / 2
            if is_arc(it):
                for q in (it.GetStart(), it.GetEnd()):
                    if point_seg_dist(q.x, q.y, fx, fy, ex, ey) <= hw + ow:
                        best = max(best, project_t(q.x, q.y, fx, fy, ex, ey))
                continue
            a, b = it.GetStart(), it.GetEnd()
            for q in (a, b):
                if point_seg_dist(q.x, q.y, fx, fy, ex, ey) <= hw + ow:
                    best = max(best, project_t(q.x, q.y, fx, fy, ex, ey))
            cross = seg_intersection_t((fx, fy), (ex, ey), (a.x, a.y), (b.x, b.y))
            if cross is not None:
                best = max(best, cross)
        if best * length < max(hw, to_nm(0.01)):
            return ("remove", free_is_start, None)
        if best >= 1.0 - 1e-6:
            return None
        return ("trim", free_is_start, (fx + (ex - fx) * best, fy + (ey - fy) * best))

    def _has_mid_contact(self, track: Any, others: List[Tuple[str, Any]], layer: int) -> bool:
        shape = track.GetEffectiveShape(layer)
        hits = 0
        for kind, it in others:
            if kind == "zone":
                continue
            try:
                if it.IsOnLayer(layer) and it.GetEffectiveShape(layer).Collide(shape, 0):
                    hits += 1
            except Exception:
                continue
        return hits > 1

    def _orphan_via_reason(self, board: Any, conn: Any, via: Any) -> Optional[str]:
        """'orphan' when nothing touches the via, 'stub' when only one track does."""
        pos = via.GetPosition()
        nc = via.GetNetCode()
        for z in board.Zones():
            if z.GetIsRuleArea() or nc <= 0 or z.GetNetCode() != nc:
                continue
            for lid in zone_copper_layers(z):
                if not via.IsOnLayer(lid):
                    continue
                # A stitching via inside its own net's zone is valid even when
                # the fill is stale or missing.
                if z.HitTestFilledArea(lid, pos, 0) or poly_contains(z.Outline(), pos.x, pos.y):
                    return None
        tracks = list(conn.GetConnectedTracks(via))
        pads = list(conn.GetConnectedPads(via))
        if pads:
            return None
        if not tracks:
            return "orphan"
        if len(tracks) == 1:
            return "stub"
        return None
