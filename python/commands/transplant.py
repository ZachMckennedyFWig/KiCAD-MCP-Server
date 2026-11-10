"""Reference-layout transplant (MCP_ISSUES Part 2 §2.6 R4).

``transplant_layout`` copies routing (tracks, arcs, vias, zones, rule areas)
from a reference board around an anchor footprint onto the loaded board
around a matching anchor, using the anchor-to-anchor rigid transform
(rotation + translation + optional mirror when the anchors sit on opposite
sides).

Before copying it checks every source pad in the region against the target
board: a target pad must sit at the transformed position (+/- tolerance) and
carry the mapped net. The net map is derived from that pad correspondence
when not given. That check is what caught a wrong net on a capacitor and a
reversed resistor in the RP2350 transplant this tool is modelled on
(``transplant_rpi_core.py``).

After copying it reports what tends to go wrong with copied layouts:
pour-island splits, orphaned tracks/vias, rule areas that routers treat as
obstacles, and shorts to existing copper (which roll the copy back unless
``force``).

The transform is fitted to the anchor pads (least squares, 2D Kabsch), so it
does not depend on how KiCad reports footprint orientation for flipped parts.
"""

import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pcbnew

from commands.finishing_geom import (
    ParamError,
    ShapeIndex,
    box_of,
    grow,
    is_arc,
    is_via,
    layer_name,
    no_board,
    num,
    parse_region,
    restore_fills,
    shape_box,
    snapshot_fills,
    to_mm,
    to_nm,
    vec,
    via_width,
    zone_copper_layers,
)
from utils.board_items import delete_board_item, detach_board_item

logger = logging.getLogger("kicad_interface")

INCLUDE_ALL = ("tracks", "vias", "zones", "rule_areas")


class Transform:
    """p -> R(theta) * M * (p - src_c) + dst_c, raw board coordinates (y down)."""

    def __init__(
        self, theta: float, mirror: bool, src_c: Tuple[float, float], dst_c: Tuple[float, float]
    ):
        self.theta = theta
        self.mirror = mirror
        self.src_c = src_c
        self.dst_c = dst_c
        self.cos = math.cos(theta)
        self.sin = math.sin(theta)

    def apply(self, x: float, y: float) -> Tuple[float, float]:
        dx, dy = x - self.src_c[0], y - self.src_c[1]
        if self.mirror:
            dx = -dx
        return (
            self.cos * dx - self.sin * dy + self.dst_c[0],
            self.sin * dx + self.cos * dy + self.dst_c[1],
        )

    def inverse(self, x: float, y: float) -> Tuple[float, float]:
        dx, dy = x - self.dst_c[0], y - self.dst_c[1]
        rx = self.cos * dx + self.sin * dy
        ry = -self.sin * dx + self.cos * dy
        if self.mirror:
            rx = -rx
        return rx + self.src_c[0], ry + self.src_c[1]

    def vec(self, p: Any) -> Any:
        x, y = self.apply(p.x, p.y)
        return vec(x, y)

    @property
    def kicad_degrees(self) -> float:
        """The same rotation as an EDA_ANGLE (KiCad rotates by R(-a) in raw coords)."""
        return -math.degrees(self.theta)


def fit_transform(
    src: Sequence[Tuple[float, float]], dst: Sequence[Tuple[float, float]], mirror: bool
) -> Tuple[Transform, float]:
    """Least-squares rigid fit (optionally mirrored); returns (transform, max residual nm)."""
    n = len(src)
    sx = sum(p[0] for p in src) / n
    sy = sum(p[1] for p in src) / n
    tx = sum(p[0] for p in dst) / n
    ty = sum(p[1] for p in dst) / n
    a = b = 0.0
    for (px, py), (qx, qy) in zip(src, dst):
        px, py = px - sx, py - sy
        if mirror:
            px = -px
        qx, qy = qx - tx, qy - ty
        a += px * qx + py * qy
        b += px * qy - py * qx
    theta = math.atan2(b, a) if (a or b) else 0.0
    # Snap to the nearest 0.01 deg multiple of 90 deg when that is what it is.
    deg = math.degrees(theta)
    snapped = round(deg / 90.0) * 90.0
    if abs(deg - snapped) < 0.01:
        theta = math.radians(snapped)
    t = Transform(theta, mirror, (sx, sy), (tx, ty))
    resid = 0.0
    for (px, py), (qx, qy) in zip(src, dst):
        x, y = t.apply(px, py)
        resid = max(resid, math.hypot(x - qx, y - qy))
    return t, resid


def point_in_poly(x: float, y: float, poly: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi:
            inside = not inside
        j = i
    return inside


def _pad_label(pad: Any) -> str:
    fp = pad.GetParentFootprint()
    return f"{fp.GetReference() if fp is not None else '?'}.{pad.GetNumber()}"


def _xy(p: Any) -> Dict[str, float]:
    return {"x": to_mm(p[0], 3), "y": to_mm(p[1], 3)}


class TransplantCommands:
    """``transplant_layout``. Holds the interface so it always sees the current board."""

    def __init__(self, interface: Any = None) -> None:
        self.interface = interface

    @property
    def board(self) -> Any:
        return getattr(self.interface, "board", None)

    def transplant_layout(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Copy routing around a reference anchor footprint onto the loaded board."""
        if not self.board:
            return no_board()
        try:
            return self._transplant(params or {})
        except ParamError as e:
            return e.response()
        except Exception as e:
            import traceback

            logger.error(f"transplant_layout failed: {e}\n{traceback.format_exc()}")
            return {"success": False, "message": "transplant_layout failed", "errorDetails": str(e)}

    # ------------------------------------------------------------------ #

    def _persist(self, result: Dict[str, Any]) -> Dict[str, Any]:
        from commands.finishing import FinishingCommands

        return FinishingCommands(self.interface)._persist(result)

    def _transplant(self, params: Dict[str, Any]) -> Dict[str, Any]:
        tgt = self.board
        src_path = params.get("sourceBoard")
        if not src_path:
            raise ParamError("'sourceBoard' is required", "path to the reference .kicad_pcb")
        src_path = os.path.abspath(os.path.expanduser(str(src_path)))
        if not os.path.isfile(src_path):
            raise ParamError(f"Source board not found: {src_path}")
        try:
            current = os.path.abspath(tgt.GetFileName() or "")
        except Exception:
            current = ""
        if current and os.path.normcase(current) == os.path.normcase(src_path):
            raise ParamError("sourceBoard is the loaded board", "copy from a different board file")
        src_ref = params.get("sourceAnchorRef")
        tgt_ref = params.get("targetAnchorRef")
        if not src_ref or not tgt_ref:
            raise ParamError("'sourceAnchorRef' and 'targetAnchorRef' are required")
        include = params.get("include") or list(INCLUDE_ALL)
        bad = [i for i in include if i not in INCLUDE_ALL]
        if bad:
            raise ParamError("Invalid include entries", f"{bad}; use {list(INCLUDE_ALL)}")
        tol = to_nm(num(params, "tolerance", 0.05))
        verify = bool(params.get("verify", True))
        force = bool(params.get("force", False))
        dry_run = bool(params.get("dryRun", False))
        replace = bool(params.get("replaceExisting", False))
        refill = bool(params.get("refill", True))
        limit = int(num(params, "limit", 50))
        margin = to_nm(num(params, "regionMargin", 3.0))
        frame = str(params.get("regionFrame", "source"))
        if frame not in ("source", "target"):
            raise ParamError("Invalid regionFrame", "use 'source' or 'target'")
        given_map = params.get("netMap") or {}
        if not isinstance(given_map, dict):
            raise ParamError("'netMap' must be an object", '{"sourceNet": "targetNet"}')

        src = pcbnew.LoadBoard(src_path)
        if src is None or not hasattr(src, "GetFootprints"):
            raise ParamError(f"Could not load source board {src_path}")
        src_fp = src.FindFootprintByReference(str(src_ref))
        tgt_fp = tgt.FindFootprintByReference(str(tgt_ref))
        if src_fp is None or not isinstance(src_fp, pcbnew.FOOTPRINT):
            raise ParamError(f"Anchor '{src_ref}' not found on the source board")
        if tgt_fp is None or not isinstance(tgt_fp, pcbnew.FOOTPRINT):
            raise ParamError(f"Anchor '{tgt_ref}' not found on the loaded board")

        mirror = bool(src_fp.IsFlipped()) != bool(tgt_fp.IsFlipped())
        xform, resid, anchor_issues = self._anchor_transform(src_fp, tgt_fp, mirror, tol)

        # ---- region (source coordinates) ------------------------------------
        region_box = parse_region(params.get("region"))
        if region_box is None:
            crt = src_fp.GetCourtyard(pcbnew.B_CrtYd if src_fp.IsFlipped() else pcbnew.F_CrtYd)
            if crt is not None and crt.OutlineCount() > 0:
                b = box_of(crt.BBox())
            else:
                try:
                    b = box_of(src_fp.GetBoundingBox(False))
                except TypeError:
                    b = box_of(src_fp.GetBoundingBox(False, False))
            region_box = grow(b, margin)
            frame = "source"
        corners = [
            (region_box[0], region_box[1]),
            (region_box[2], region_box[1]),
            (region_box[2], region_box[3]),
            (region_box[0], region_box[3]),
        ]
        if frame == "target":
            src_poly = [xform.inverse(x, y) for x, y in corners]
        else:
            src_poly = [(float(x), float(y)) for x, y in corners]
        tgt_poly = [xform.apply(x, y) for x, y in src_poly]

        def in_region(points: Sequence[Tuple[float, float]]) -> bool:
            return all(point_in_poly(x, y, src_poly) for x, y in points)

        # ---- pad correspondence + net map ------------------------------------
        verification, derived, mismatches = self._verify_pads(
            src, tgt, xform, src_poly, tgt_poly, tol, given_map
        )
        mismatches = anchor_issues + mismatches
        net_map: Dict[str, str] = dict(derived)
        net_map.update({str(k): str(v) for k, v in given_map.items()})
        missing_targets = sorted({v for v in net_map.values() if tgt.FindNet(v) is None})
        if missing_targets:
            raise ParamError(
                "netMap names nets that are not on the loaded board", ", ".join(missing_targets)
            )

        result: Dict[str, Any] = {
            "success": True,
            "dryRun": dry_run,
            "unit": "mm",
            "sourceBoard": src_path,
            "transform": {
                "rotationDeg": round(xform.kicad_degrees % 360, 4),
                "mirrored": mirror,
                "sourceCentre": _xy(xform.src_c),
                "targetCentre": _xy(xform.dst_c),
                "anchorResidualMax": to_mm(resid, 4),
                "note": "rotationDeg uses KiCad's convention (counter-clockwise, like footprint orientation); applied after the mirror when mirrored",
            },
            "region": {
                "frame": "source",
                "polygon": [_xy(p) for p in src_poly],
                "targetPolygon": [_xy(p) for p in tgt_poly],
            },
            "verification": dict(
                verification,
                mismatchCount=len(mismatches),
                mismatches=mismatches[:limit],
            ),
            "netMap": {
                "count": len(net_map),
                "derived": sum(1 for k in derived if k not in given_map),
                "given": len(given_map),
                "renamed": {k: v for k, v in sorted(net_map.items()) if k != v},
            },
        }
        if bool(params.get("fullNetMap", False)):
            result["netMap"]["all"] = dict(sorted(net_map.items()))
        if verify and mismatches and not force:
            result["success"] = False
            result["message"] = (
                f"{len(mismatches)} pad mismatch(es) between source and target; nothing was "
                "copied. Fix the placement/nets, pass an explicit netMap, or force=true"
            )
            return result

        # ---- gather source items ---------------------------------------------
        skipped: Dict[str, Any] = {"unmappedNet": {}, "layerMissing": 0}
        items: List[Tuple[str, Any]] = []
        for t in src.GetTracks():
            if is_via(t):
                if "vias" not in include:
                    continue
                p = t.GetPosition()
                pts = [(p.x, p.y)]
                kind = "via"
            else:
                if "tracks" not in include:
                    continue
                pts = [(t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y)]
                if is_arc(t):
                    pts.append((t.GetMid().x, t.GetMid().y))
                kind = "arc" if is_arc(t) else "track"
            if not in_region(pts):
                continue
            net = t.GetNetname()
            if net not in net_map:
                skipped["unmappedNet"][net] = skipped["unmappedNet"].get(net, 0) + 1
                continue
            items.append((kind, t))
        for z in src.Zones():
            rule = bool(z.GetIsRuleArea())
            if ("rule_areas" if rule else "zones") not in include:
                continue
            outline = z.Outline()
            pts = []
            for i in range(outline.OutlineCount()):
                ch = outline.Outline(i)
                pts += [(ch.CPoint(k).x, ch.CPoint(k).y) for k in range(ch.PointCount())]
            if not pts or not in_region(pts):
                continue
            net = z.GetNetname()
            if not rule and net not in net_map:
                skipped["unmappedNet"][net] = skipped["unmappedNet"].get(net, 0) + 1
                continue
            items.append(("rule_area" if rule else "zone", z))

        # ---- apply (always transactional) -----------------------------------
        tgt.BuildConnectivity()
        unconnected_before = int(tgt.GetConnectivity().GetUnconnectedCount(False))
        added: List[Any] = []
        removed: List[Any] = []
        copied = {"tracks": 0, "arcs": 0, "vias": 0, "zones": 0, "ruleAreas": 0}
        pairs: List[Tuple[Any, Any]] = []  # (source zone, target zone)
        fills = snapshot_fills(tgt)

        def undo() -> None:
            self._rollback(tgt, added, removed)
            restore_fills(fills)
            tgt.BuildConnectivity()

        try:
            if replace:
                removed = self._remove_existing(tgt, tgt_poly, set(net_map.values()))
            for kind, item in items:
                made = self._copy_item(tgt, kind, item, xform, mirror, net_map)
                if made is None:
                    skipped["layerMissing"] += 1
                    continue
                if kind not in ("zone", "rule_area"):  # zones are added by _copy_item
                    tgt.Add(made)
                added.append(made)
                key = {"track": "tracks", "arc": "arcs", "via": "vias", "zone": "zones"}.get(
                    kind, "ruleAreas"
                )
                copied[key] += 1
                if kind == "zone":
                    pairs.append((item, made))
            if refill and (pairs or added):
                pcbnew.ZONE_FILLER(tgt).Fill(tgt.Zones())
            tgt.BuildConnectivity()
            report = self._post_report(src, tgt, added, pairs, limit)
            report["connectivity"] = {
                "unconnectedBefore": unconnected_before,
                "unconnectedAfter": int(tgt.GetConnectivity().GetUnconnectedCount(False)),
            }
        except Exception:
            undo()
            raise

        result.update(
            copied=copied,
            skipped={
                "unmappedNet": skipped["unmappedNet"],
                "layerMissing": skipped["layerMissing"],
            },
            removedExisting=len(removed),
            report=report,
        )
        shorts = report.get("shorts", [])
        if shorts and not force:
            undo()
            result["success"] = False
            result["rolledBack"] = True
            result["message"] = (
                f"Copied copper would short {len(shorts)} time(s) against existing copper of "
                "other nets; the copy was rolled back (force=true keeps it)"
            )
            return result
        if dry_run:
            undo()
            return result
        if not added and not removed:
            return result
        for item in removed:
            tgt.Add(item)
            delete_board_item(tgt, item)
        return self._persist(result)

    # ------------------------------------------------------------------ #

    def _anchor_transform(
        self, src_fp: Any, tgt_fp: Any, mirror: bool, tol: int
    ) -> Tuple[Transform, float, List[Dict[str, Any]]]:
        def by_number(fp: Any) -> Dict[str, List[Tuple[int, int]]]:
            out: Dict[str, List[Tuple[int, int]]] = {}
            for p in fp.Pads():
                pos = p.GetPosition()
                out.setdefault(str(p.GetNumber()), []).append((pos.x, pos.y))
            return out

        sp, tp = by_number(src_fp), by_number(tgt_fp)
        issues: List[Dict[str, Any]] = []
        src_pts, dst_pts = [], []
        for number, plist in sp.items():
            other = tp.get(number)
            if not other or len(other) != len(plist):
                issues.append(
                    {
                        "type": "anchorPadMissing",
                        "sourcePad": f"{src_fp.GetReference()}.{number}",
                        "detail": "target anchor has no pad with this number (or a different count)",
                    }
                )
                continue
            # Several pads can share a number (exposed-pad arrays): pair them
            # by order of position, which the rigid fit tolerates.
            src_pts += sorted(plist)
            dst_pts += sorted(other)
        distinct = {p for p in src_pts}
        if len(distinct) >= 2:
            xform, resid = fit_transform(src_pts, dst_pts, mirror)
            # Pads sharing a number may pair in the wrong order after rotation;
            # refit with only the unique numbers when that happens.
            if resid > tol:
                uniq_s, uniq_t = [], []
                for number, plist in sp.items():
                    if len(plist) == 1 and len(tp.get(number) or []) == 1:
                        uniq_s.append(plist[0])
                        uniq_t.append(tp[number][0])
                if len(set(uniq_s)) >= 2:
                    xform, resid = fit_transform(uniq_s, uniq_t, mirror)
        else:
            sp0, tp0 = src_fp.GetPosition(), tgt_fp.GetPosition()
            d = tgt_fp.GetOrientationDegrees() - src_fp.GetOrientationDegrees()
            xform = Transform(-math.radians(d), mirror, (sp0.x, sp0.y), (tp0.x, tp0.y))
            resid = 0.0
        if resid > tol:
            issues.append(
                {
                    "type": "anchorGeometry",
                    "detail": (
                        f"anchor pads do not fit a rigid transform within tolerance "
                        f"(max residual {to_mm(resid, 4)} mm): different footprints?"
                    ),
                }
            )
        return xform, resid, issues

    def _verify_pads(
        self,
        src: Any,
        tgt: Any,
        xform: Transform,
        src_poly: List[Tuple[float, float]],
        tgt_poly: List[Tuple[float, float]],
        tol: int,
        given_map: Dict[str, str],
    ) -> Tuple[Dict[str, Any], Dict[str, str], List[Dict[str, Any]]]:
        index = ShapeIndex(cell_nm=to_nm(1.0))
        tgt_pads = []
        for fp in tgt.GetFootprints():
            for p in fp.Pads():
                pos = p.GetPosition()
                index.add((pos.x, pos.y, pos.x, pos.y), (pos.x, pos.y, p))
                tgt_pads.append(p)
        pairs: List[Tuple[Any, Any, Tuple[float, float]]] = []
        mismatches: List[Dict[str, Any]] = []
        checked = 0
        matched_tgt = set()
        for fp in src.GetFootprints():
            for p in fp.Pads():
                pos = p.GetPosition()
                if not point_in_poly(pos.x, pos.y, src_poly):
                    continue
                if not str(p.GetNumber()) and not p.GetNetname():
                    continue  # paste apertures / mechanical pads: not electrical
                checked += 1
                qx, qy = xform.apply(pos.x, pos.y)
                # Prefer a pad with the same number when several share a spot
                # (exposed pads carry unnumbered aperture pads on top).
                best, best_key = None, None
                for x, y, cand in index.query(grow((int(qx), int(qy), int(qx), int(qy)), tol)):
                    d = math.hypot(x - qx, y - qy)
                    if d > tol:
                        continue
                    key = (str(cand.GetNumber()) != str(p.GetNumber()), not cand.GetNetname(), d)
                    if best_key is None or key < best_key:
                        best, best_key = cand, key
                if best is None:
                    mismatches.append(
                        {
                            "type": "missingTargetPad",
                            "sourcePad": _pad_label(p),
                            "sourceNet": p.GetNetname(),
                            "expectedAt": _xy((qx, qy)),
                        }
                    )
                    continue
                matched_tgt.add(best.m_Uuid.AsString())
                pairs.append((p, best, (qx, qy)))

        # derive the net map by majority vote over pad pairs
        votes: Dict[str, Dict[str, int]] = {}
        for sp, tp, _ in pairs:
            sn, tn = sp.GetNetname(), tp.GetNetname()
            if not sn:
                continue
            votes.setdefault(sn, {})
            votes[sn][tn] = votes[sn].get(tn, 0) + 1
        derived: Dict[str, str] = {}
        for sn, tally in votes.items():
            if sn in given_map:
                continue
            # majority wins; on a tie prefer the same net name, then any named net
            best_net = max(tally.items(), key=lambda kv: (kv[1], kv[0] == sn, kv[0] != ""))[0]
            if best_net:
                derived[sn] = best_net
        for sp, tp, at in pairs:
            sn, tn = sp.GetNetname(), tp.GetNetname()
            if not sn:
                continue
            expected = given_map.get(sn, derived.get(sn))
            if expected is None:
                mismatches.append(
                    {
                        "type": "unconnectedTargetPad",
                        "sourcePad": _pad_label(sp),
                        "targetPad": _pad_label(tp),
                        "sourceNet": sn,
                        "targetNet": tn,
                        "at": _xy(at),
                    }
                )
            elif tn != expected:
                mismatches.append(
                    {
                        "type": "netMismatch",
                        "sourcePad": _pad_label(sp),
                        "targetPad": _pad_label(tp),
                        "sourceNet": sn,
                        "expectedNet": expected,
                        "targetNet": tn,
                        "at": _xy(at),
                    }
                )
        # two source nets folded onto one target net is a likely mistake
        inverse: Dict[str, List[str]] = {}
        for sn, tn in list(derived.items()) + [(k, v) for k, v in given_map.items()]:
            inverse.setdefault(tn, []).append(sn)
        merges = {tn: sorted(set(sns)) for tn, sns in inverse.items() if len(set(sns)) > 1}
        for tn, sns in merges.items():
            mismatches.append(
                {
                    "type": "netMerge",
                    "targetNet": tn,
                    "sourceNets": sns,
                    "detail": "several source nets map to one target net",
                }
            )
        extra = 0
        for p in tgt_pads:
            pos = p.GetPosition()
            if point_in_poly(pos.x, pos.y, tgt_poly) and p.m_Uuid.AsString() not in matched_tgt:
                extra += 1
        info = {
            "padsChecked": checked,
            "padsMatched": len(pairs),
            "extraTargetPads": extra,
            "tolerance": to_mm(tol, 4),
        }
        return info, derived, mismatches

    @staticmethod
    def _map_layer(tgt: Any, layer: int, mirror: bool) -> Optional[int]:
        if mirror:
            flip = getattr(tgt, "FlipLayer", None)
            if callable(flip):
                layer = int(flip(layer))
            elif layer == pcbnew.F_Cu:
                layer = pcbnew.B_Cu
            elif layer == pcbnew.B_Cu:
                layer = pcbnew.F_Cu
        if not tgt.IsLayerEnabled(layer):
            return None
        return int(layer)

    def _copy_item(
        self, tgt: Any, kind: str, item: Any, xf: Transform, mirror: bool, net_map: Dict[str, str]
    ) -> Optional[Any]:
        net = tgt.FindNet(net_map[item.GetNetname()]) if item.GetNetname() in net_map else None
        if kind in ("track", "arc"):
            lay = self._map_layer(tgt, item.GetLayer(), mirror)
            if lay is None:
                return None
            new = pcbnew.PCB_ARC(tgt) if kind == "arc" else pcbnew.PCB_TRACK(tgt)
            new.SetStart(xf.vec(item.GetStart()))
            new.SetEnd(xf.vec(item.GetEnd()))
            if kind == "arc":
                new.SetMid(xf.vec(item.GetMid()))
            new.SetWidth(item.GetWidth())
            new.SetLayer(lay)
            new.SetNet(net)
            new.SetLocked(item.IsLocked())
            return new
        if kind == "via":
            top, bottom = item.TopLayer(), item.BottomLayer()
            mt, mb = self._map_layer(tgt, top, mirror), self._map_layer(tgt, bottom, mirror)
            if mt is None or mb is None:
                return None
            new = pcbnew.PCB_VIA(tgt)
            new.SetPosition(xf.vec(item.GetPosition()))
            new.SetViaType(item.GetViaType())
            new.SetWidth(via_width(item))
            new.SetDrill(item.GetDrillValue())
            new.SetLayerPair(mt, mb)
            if hasattr(new, "SanitizeLayers"):
                new.SanitizeLayers()
            new.SetNet(net)
            new.SetLocked(item.IsLocked())
            return new
        # zones and rule areas: duplicate for fidelity (thermal settings,
        # keepout flags, priority, hatch...), re-net, then move/flip/rotate
        # with KiCad's own operations once the target board is the parent,
        # so layer flipping follows the target's copper count.
        dup = item.Duplicate(False)
        dup = dup.Cast() if hasattr(dup, "Cast") else dup
        if not isinstance(dup, pcbnew.ZONE):
            raise RuntimeError("zone duplicate did not cast to ZONE")
        dup.SetNet(net if net is not None else tgt.FindNet(0))
        if hasattr(dup, "UnFill"):
            dup.UnFill()
        tgt.Add(dup)
        cs, cd = xf.src_c, xf.dst_c
        dup.Move(vec(cd[0] - cs[0], cd[1] - cs[1]))
        centre = vec(cd[0], cd[1])
        if mirror:
            dup.Flip(centre, pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        if abs(xf.theta) > 1e-12:
            dup.Rotate(centre, pcbnew.EDA_ANGLE(xf.kicad_degrees, pcbnew.DEGREES_T))
        # cross-check KiCad's result against the fitted transform
        v_src = item.Outline().CVertex(0)
        v_dst = dup.Outline().CVertex(0)
        ex, ey = xf.apply(v_src.x, v_src.y)
        if math.hypot(v_dst.x - ex, v_dst.y - ey) > 2000:
            delete_board_item(tgt, dup)
            raise RuntimeError("zone transform disagrees with the anchor transform")
        lays = [lid for lid in dup.GetLayerSet().Seq() if not tgt.IsLayerEnabled(lid)]
        if lays:
            delete_board_item(tgt, dup)
            return None
        return dup

    @staticmethod
    def _remove_existing(tgt: Any, tgt_poly: List[Tuple[float, float]], nets: set) -> List[Any]:
        """Detach target tracks/vias of mapped nets inside the region (keeps vias in pads)."""
        pads = [p for fp in tgt.GetFootprints() for p in fp.Pads()]
        out = []
        for t in list(tgt.GetTracks()):
            if t.GetNetname() not in nets:
                continue
            if is_via(t):
                p = t.GetPosition()
                pts = [(p.x, p.y)]
                if any(pad.HitTest(p) for pad in pads if pad.GetNetCode() == t.GetNetCode()):
                    continue  # exposed-pad / via-in-pad: keep
            else:
                pts = [(t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y)]
            if all(point_in_poly(x, y, tgt_poly) for x, y in pts):
                out.append(detach_board_item(tgt, t))
        return out

    @staticmethod
    def _rollback(tgt: Any, added: List[Any], removed: List[Any]) -> None:
        for item in reversed(added):
            try:
                delete_board_item(tgt, item)
            except Exception as e:
                logger.warning(f"transplant rollback could not delete an item: {e}")
        added.clear()
        for item in removed:
            tgt.Add(item)
        removed.clear()

    def _post_report(
        self, src: Any, tgt: Any, added: List[Any], pairs: List[Tuple[Any, Any]], limit: int
    ) -> Dict[str, Any]:
        conn = tgt.GetConnectivity()
        # pour islands: copied zone fills split into more pieces than in the source
        islands = []
        if pairs:
            if any(not z.IsFilled() for z, _ in pairs):
                pcbnew.ZONE_FILLER(src).Fill(src.Zones())
            for sz, tz in pairs:
                for slid in zone_copper_layers(sz):
                    s_n = sz.GetFilledPolysList(slid).OutlineCount()
                    tl = [lid for lid in zone_copper_layers(tz)]
                    t_n = max((tz.GetFilledPolysList(lid).OutlineCount() for lid in tl), default=0)
                    if t_n > s_n or (s_n > 0 and t_n == 0):
                        pos = tz.Outline().CVertex(0)
                        islands.append(
                            {
                                "net": tz.GetNetname(),
                                "layer": layer_name(tgt, tl[0]) if tl else "",
                                "sourceIslands": s_n,
                                "targetIslands": t_n,
                                "near": _xy((pos.x, pos.y)),
                                "hint": (
                                    "fill vanished: zone narrower than min width/clearance here"
                                    if t_n == 0
                                    else "pour split into islands: different pad sizes or "
                                    "clearances than the reference"
                                ),
                            }
                        )
        orphans = []
        rule_areas = []
        for it in added:
            if isinstance(it, pcbnew.ZONE):
                if it.GetIsRuleArea():
                    flags = {
                        "tracks": bool(it.GetDoNotAllowTracks()),
                        "vias": bool(it.GetDoNotAllowVias()),
                        "pads": bool(it.GetDoNotAllowPads()),
                        "footprints": bool(it.GetDoNotAllowFootprints()),
                        "zoneFills": bool(it.GetDoNotAllowZoneFills()),
                    }
                    blocks_routing = flags["tracks"] or flags["vias"] or flags["pads"]
                    pos = it.Outline().CVertex(0)
                    rule_areas.append(
                        {
                            "name": it.GetZoneName(),
                            "layers": [layer_name(tgt, lid) for lid in it.GetLayerSet().Seq()],
                            "forbids": [k for k, v in flags.items() if v],
                            "routerObstacle": True,
                            "pourOnly": flags["zoneFills"] and not blocks_routing,
                            "near": _xy((pos.x, pos.y)),
                        }
                    )
                continue
            if is_via(it):
                tracks = list(conn.GetConnectedTracks(it))
                pads = list(conn.GetConnectedPads(it))
                cluster = list(conn.GetConnectedItems(it))
                in_zone = any(isinstance(c, pcbnew.ZONE) for c in cluster)
                if not pads and len(tracks) <= 1 and not in_zone:
                    p = it.GetPosition()
                    orphans.append(
                        {
                            "type": "via",
                            "net": it.GetNetname(),
                            "at": _xy((p.x, p.y)),
                            "reason": (
                                "connected to nothing" if not tracks else "stub (one track only)"
                            ),
                        }
                    )
                continue
            pos = pcbnew.VECTOR2I(0, 0)
            try:
                dangling = conn.TestTrackEndpointDangling(it, True, pos)
            except TypeError:
                dangling = conn.TestTrackEndpointDangling(it, True)
            if dangling:
                orphans.append(
                    {
                        "type": "arc" if is_arc(it) else "track",
                        "net": it.GetNetname(),
                        "layer": layer_name(tgt, it.GetLayer()),
                        "danglingAt": _xy((pos.x, pos.y)),
                    }
                )
        shorts = self._shorts(tgt, added)
        report: Dict[str, Any] = {
            "pourIslands": islands[:limit],
            "orphaned": orphans[:limit],
            "orphanedCount": len(orphans),
            "ruleAreas": rule_areas[:limit],
            "shorts": shorts[:limit],
        }
        if rule_areas:
            report["ruleAreaNote"] = (
                "Freerouting exports every rule area as a keepout, including pour-only ones "
                "(pourOnly=true); strip them before autorouting or route this region by hand"
            )
        return report

    @staticmethod
    def _shorts(tgt: Any, added: List[Any]) -> List[Dict[str, Any]]:
        """Copied tracks/vias that physically touch copper of another net."""
        new_ids = {it.m_Uuid.AsString() for it in added}
        index = ShapeIndex()
        for t in tgt.GetTracks():
            if t.m_Uuid.AsString() in new_ids:
                continue
            for lid in ([t.GetLayer()] if not is_via(t) else [pcbnew.F_Cu]):
                shape = t.GetEffectiveShape(lid)
                index.add(shape_box(shape), (t.GetNetCode(), t, shape, is_via(t)))
        for fp in tgt.GetFootprints():
            for p in fp.Pads():
                for lid in p.GetLayerSet().CuStack():
                    shape = p.GetEffectiveShape(lid)
                    index.add(shape_box(shape), (p.GetNetCode(), p, shape, lid))
                    break
        out = []
        for it in added:
            if isinstance(it, pcbnew.ZONE):
                continue
            lid = pcbnew.F_Cu if is_via(it) else it.GetLayer()
            shape = it.GetEffectiveShape(lid)
            for nc, other, oshape, _ in index.query(shape_box(shape)):
                if nc == it.GetNetCode():
                    continue
                if not is_via(it) and not other.IsOnLayer(lid) and not is_via(other):
                    continue
                try:
                    hit = oshape.Collide(shape, 0)
                except Exception:
                    hit = False
                if hit:
                    p = it.GetPosition() if is_via(it) else it.GetStart()
                    out.append(
                        {
                            "net": it.GetNetname(),
                            "otherNet": other.GetNetname(),
                            "type": "via" if is_via(it) else "track",
                            "at": _xy((p.x, p.y)),
                        }
                    )
                    break
        return out
