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

What the real RP2350B transplant (Raspberry Pi's minimal design onto a board
using KiCad's stock QFN-80 footprint) needed beyond a plain copy:

* The two footprints differ: the reference QFN has its exposed pad split into
  an SMD pad plus nine plated through-hole "via" pads, and its pins sit
  0.05 mm further in. A source pad whose centre falls inside the copper of a
  same-numbered target pad is a match (reported under ``offsetPads``), and a
  plated hole with no hole on the target side becomes a board via
  (``include: pad_vias``).
* Only the power nets are copied (``nets``); pad mismatches on nets that are
  not copied are reported but do not block the copy.
* Anchor pins outvote passives when the net map is derived, and a two-pad
  part whose nets are swapped is reported as reversed (the R21 case) instead
  of as a net merge.
* The reference uses 0.25 mm via drills; copied vias are grown to the target
  board's minimum via size / drill (``fitVias``).
* Copied tracks that ended in a reference pour that was not copied (it
  extends outside the region) are reported with that reason, and copied
  copper too close to existing copper is reported (``clearance``) and can be
  dropped (``skipConflicting``).
* Re-running is idempotent: items already present on the target are not
  duplicated, and ``replaceExisting`` also replaces zones and rule areas.

The transform is fitted to the anchor pads (least squares, 2D Kabsch), so it
does not depend on how KiCad reports footprint orientation for flipped parts.
"""

import logging
import math
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pcbnew

from commands.finishing_geom import (
    ParamError,
    ShapeIndex,
    box_of,
    grow,
    is_arc,
    is_via,
    layer_name,
    netclass_clearance_fn,
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

INCLUDE_ALL = ("tracks", "vias", "zones", "rule_areas", "pad_vias")

# A vote from an anchor pin outweighs any number of passives: the anchor's
# pads are what the transform was fitted to, so their nets are authoritative.
ANCHOR_WEIGHT = 1000


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


# --------------------------------------------------------------------------- #
# Net-map logic (pure Python, unit-tested without pcbnew)
# --------------------------------------------------------------------------- #


def derive_net_map(
    votes: Iterable[Tuple[str, str, int]], given: Dict[str, str]
) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    """Derive source net -> target net from weighted pad votes.

    ``votes`` are ``(source net, target net, weight)``. The heaviest target net
    wins; on a tie a target net no other source net has claimed yet wins (so
    a reversed passive does not fold two nets together), then the same name,
    then any named net. Nets in ``given`` are not derived. Returns
    ``(derived, ambiguous)`` where ``ambiguous`` lists the ties.
    """
    tally: Dict[str, Dict[str, int]] = {}
    for sn, tn, w in votes:
        if not sn or sn in given:
            continue
        tally.setdefault(sn, {})
        tally[sn][tn] = tally[sn].get(tn, 0) + int(w)
    claimed: Set[str] = {str(v) for v in given.values()}
    derived: Dict[str, str] = {}
    ambiguous: List[Dict[str, Any]] = []
    order = sorted(tally, key=lambda sn: (-max(tally[sn].values()), sn))
    for sn in order:
        ranked = sorted(
            tally[sn].items(),
            key=lambda kv: (-kv[1], kv[0] in claimed, kv[0] != sn, kv[0] == "", kv[0]),
        )
        best, weight = ranked[0]
        if len(ranked) > 1 and ranked[1][1] == weight:
            ambiguous.append(
                {
                    "sourceNet": sn,
                    "candidates": sorted(k for k, v in tally[sn].items() if v == weight),
                    "chosen": best or None,
                }
            )
        if best:
            derived[sn] = best
            claimed.add(best)
    return derived, ambiguous


def find_reversed_parts(
    pairs: Sequence[Dict[str, Any]], net_map: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Two-pad target parts whose pad nets are swapped relative to the source.

    ``pairs`` entries: ``srcRef, srcNet, tgtRef, tgtNet, tgtPadCount``.
    """
    by_tgt: Dict[str, List[Dict[str, Any]]] = {}
    for p in pairs:
        by_tgt.setdefault(p["tgtRef"], []).append(p)
    out = []
    for ref, ps in sorted(by_tgt.items()):
        if len(ps) != 2 or ps[0].get("tgtPadCount") != 2:
            continue
        a, b = ps
        ea, eb = net_map.get(a["srcNet"]), net_map.get(b["srcNet"])
        if not ea or not eb or ea == eb:
            continue
        if a["tgtNet"] == eb and b["tgtNet"] == ea:
            out.append(
                {
                    "targetRef": ref,
                    "sourceRef": a["srcRef"],
                    "hint": (
                        f"{ref} is reversed relative to {a['srcRef']} in the reference "
                        "(its two pads carry each other's nets): rotate it 180 deg, or "
                        "swap the pins in the schematic"
                    ),
                }
            )
    return out


def select_copy_nets(net_map: Dict[str, str], wanted: Optional[Sequence[str]]) -> Set[str]:
    """Source nets to copy: all mapped ones, or those named (by source or target name)."""
    if not wanted:
        return set(net_map)
    names = {str(n) for n in wanted}
    return {sn for sn, tn in net_map.items() if sn in names or tn in names}


def classify_mismatches(mismatches: List[Dict[str, Any]], copy_nets: Set[str]) -> int:
    """Mark each mismatch ``blocking`` (it touches a net being copied); returns the count."""
    count = 0
    for m in mismatches:
        if m["type"] in ("anchorPadMissing", "anchorGeometry"):
            blocking = True
        elif m["type"] == "netMerge":
            blocking = any(sn in copy_nets for sn in m.get("sourceNets", []))
        else:
            blocking = m.get("sourceNet") in copy_nets
        m["blocking"] = blocking
        count += int(blocking)
    return count


def fit_via_size(
    width: int, drill: int, min_size: int, min_drill: int, min_ring: int
) -> Tuple[int, int]:
    """Grow a through via to the board's minimum drill, diameter and annular ring."""
    d = max(int(drill), int(min_drill))
    w = max(int(width), int(min_size), d + 2 * int(min_ring))
    return w, d


def _um(v: float) -> int:
    return int(round(v / 1000.0))


def _pad_label(pad: Any) -> str:
    fp = pad.GetParentFootprint()
    return f"{fp.GetReference() if fp is not None else '?'}.{pad.GetNumber()}"


def _pad_ref(pad: Any) -> str:
    fp = pad.GetParentFootprint()
    return fp.GetReference() if fp is not None else "?"


def _xy(p: Any) -> Dict[str, float]:
    return {"x": to_mm(p[0], 3), "y": to_mm(p[1], 3)}


def _electrical(pad: Any) -> bool:
    """Numbered or netted pads; skips paste apertures and similar."""
    return bool(str(pad.GetNumber()) or pad.GetNetname())


def _has_hole(pad: Any) -> bool:
    try:
        d = pad.GetDrillSize()
        return d.x > 0 and d.y > 0
    except Exception:
        return False


def _pad_size(pad: Any) -> int:
    try:
        s = pad.GetSize(pcbnew.F_Cu)
    except TypeError:
        s = pad.GetSize()
    return int(min(s.x, s.y))


def _zone_vertices(zone: Any) -> List[Tuple[int, int]]:
    outline = zone.Outline()
    pts: List[Tuple[int, int]] = []
    for i in range(outline.OutlineCount()):
        ch = outline.Outline(i)
        pts += [(ch.CPoint(k).x, ch.CPoint(k).y) for k in range(ch.PointCount())]
    return pts


def geom_key(item: Any) -> Tuple[Any, ...]:
    """Identity of a copper item by net + geometry (1 um grid), for duplicate detection."""
    if isinstance(item, pcbnew.ZONE):
        rule = bool(item.GetIsRuleArea())
        verts = sorted({(_um(x), _um(y)) for x, y in _zone_vertices(item)})
        return (
            "zone",
            rule,
            "" if rule else item.GetNetname(),
            tuple(sorted(int(lid) for lid in item.GetLayerSet().Seq())),
            tuple(verts),
        )
    if is_via(item):
        p = item.GetPosition()
        return ("via", item.GetNetname(), _um(p.x), _um(p.y))
    a, b = item.GetStart(), item.GetEnd()
    ends = tuple(sorted([(_um(a.x), _um(a.y)), (_um(b.x), _um(b.y))]))
    mid = (_um(item.GetMid().x), _um(item.GetMid().y)) if is_arc(item) else None
    return ("track", item.GetNetname(), int(item.GetLayer()), ends, mid, _um(item.GetWidth()))


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
        fit_vias = bool(params.get("fitVias", True))
        skip_conflicting = bool(params.get("skipConflicting", False))
        limit = int(num(params, "limit", 50))
        margin = to_nm(num(params, "regionMargin", 3.0))
        frame = str(params.get("regionFrame", "source"))
        if frame not in ("source", "target"):
            raise ParamError("Invalid regionFrame", "use 'source' or 'target'")
        given_map = params.get("netMap") or {}
        if not isinstance(given_map, dict):
            raise ParamError("'netMap' must be an object", '{"sourceNet": "targetNet"}')
        given_map = {str(k): str(v) for k, v in given_map.items()}
        wanted = params.get("nets")
        if wanted is not None and (
            not isinstance(wanted, list) or not all(isinstance(n, str) for n in wanted)
        ):
            raise ParamError("'nets' must be a list of net names", '["+3V3", "GND"]')

        src = pcbnew.LoadBoard(src_path)
        if src is None or not hasattr(src, "GetFootprints"):
            raise ParamError(f"Could not load source board {src_path}")
        src_fp = src.FindFootprintByReference(str(src_ref))
        tgt_fp = tgt.FindFootprintByReference(str(tgt_ref))
        if src_fp is None or not isinstance(src_fp, pcbnew.FOOTPRINT):
            raise ParamError(f"Anchor '{src_ref}' not found on the source board")
        if tgt_fp is None or not isinstance(tgt_fp, pcbnew.FOOTPRINT):
            raise ParamError(f"Anchor '{tgt_ref}' not found on the loaded board")
        if wanted:
            unknown = [
                n
                for n in wanted
                if src.FindNet(n) is None and tgt.FindNet(n) is None and n not in given_map
            ]
            if unknown:
                raise ParamError(
                    "'nets' names nets that exist on neither board", ", ".join(unknown)
                )

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

        def any_in_region(points: Sequence[Tuple[float, float]]) -> bool:
            return any(point_in_poly(x, y, src_poly) for x, y in points)

        # ---- pad correspondence + net map ------------------------------------
        check = self._verify_pads(src, tgt, src_fp, xform, src_poly, tgt_poly, tol, given_map)
        net_map: Dict[str, str] = dict(check["derived"])
        net_map.update(given_map)
        missing_targets = sorted({v for v in net_map.values() if tgt.FindNet(v) is None})
        if missing_targets:
            raise ParamError(
                "netMap names nets that are not on the loaded board", ", ".join(missing_targets)
            )
        copy_nets = select_copy_nets(net_map, wanted)
        mismatches = anchor_issues + check["mismatches"]
        blocking = classify_mismatches(mismatches, copy_nets)
        # list blocking mismatches first so `limit` never hides them
        mismatches.sort(key=lambda m: not m["blocking"])

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
                check["info"],
                mismatchCount=len(mismatches),
                blockingCount=blocking,
                mismatches=mismatches[:limit],
                offsetPads=check["offsets"][:limit],
                offsetPadCount=len(check["offsets"]),
                reversedParts=check["reversed"],
                ambiguousNets=check["ambiguous"][:limit],
            ),
            "netMap": {
                "count": len(net_map),
                "derived": sum(1 for k in check["derived"] if k not in given_map),
                "given": len(given_map),
                "copied": sorted(copy_nets),
                "renamed": {k: v for k, v in sorted(net_map.items()) if k != v},
            },
        }
        if wanted:
            result["netMap"]["notCopied"] = sorted(set(net_map) - copy_nets)
        if bool(params.get("fullNetMap", False)):
            result["netMap"]["all"] = dict(sorted(net_map.items()))
        if verify and blocking and not force:
            result["success"] = False
            result["message"] = (
                f"{blocking} pad mismatch(es) on copied nets between source and target; nothing "
                "was copied. Fix the placement/nets, pass an explicit netMap, restrict `nets`, "
                "or force=true"
            )
            return result

        # ---- gather source items ---------------------------------------------
        skipped: Dict[str, Any] = {
            "unmappedNet": {},
            "notSelected": 0,
            "crossingRegion": 0,
            "partialZones": [],
            "layerMissing": 0,
            "alreadyPresent": 0,
        }
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
                if any_in_region(pts) and t.GetNetname() in copy_nets:
                    skipped["crossingRegion"] += 1
                continue
            net = t.GetNetname()
            if net not in net_map:
                skipped["unmappedNet"][net] = skipped["unmappedNet"].get(net, 0) + 1
                continue
            if net not in copy_nets:
                skipped["notSelected"] += 1
                continue
            items.append((kind, t))
        copied_src_zones: Set[str] = set()
        for z in src.Zones():
            rule = bool(z.GetIsRuleArea())
            if ("rule_areas" if rule else "zones") not in include:
                continue
            pts = _zone_vertices(z)
            if not pts:
                continue
            net = z.GetNetname()
            if not in_region(pts):
                if any_in_region(pts) and (rule or net in copy_nets):
                    skipped["partialZones"].append(
                        {
                            "ruleArea": rule,
                            "net": net,
                            "name": z.GetZoneName(),
                            "layers": [layer_name(src, lid) for lid in z.GetLayerSet().Seq()],
                        }
                    )
                continue
            if not rule and net not in net_map:
                skipped["unmappedNet"][net] = skipped["unmappedNet"].get(net, 0) + 1
                continue
            if not rule and net not in copy_nets:
                skipped["notSelected"] += 1
                continue
            items.append(("rule_area" if rule else "zone", z))
            copied_src_zones.add(z.m_Uuid.AsString())
        pad_vias = (
            [(sp, tp, at) for sp, tp, at in check["padVias"] if sp.GetNetname() in copy_nets]
            if "pad_vias" in include
            else []
        )

        # ---- apply (always transactional) -----------------------------------
        tgt.BuildConnectivity()
        unconnected_before = int(tgt.GetConnectivity().GetUnconnectedCount(False))
        nets_before = {t.m_Uuid.AsString(): t.GetNet() for t in tgt.GetTracks()}
        added: List[Any] = []
        removed: List[Any] = []
        origin: Dict[str, Any] = {}  # new item uuid -> source item
        copied = {"tracks": 0, "arcs": 0, "vias": 0, "zones": 0, "ruleAreas": 0, "padVias": 0}
        pairs: List[Tuple[Any, Any]] = []  # (source zone, target zone)
        resized: Dict[str, int] = {}
        resized_ids: Dict[str, str] = {}
        intended: Dict[str, Any] = {}  # copied track/via uuid -> NETINFO_ITEM
        fills = snapshot_fills(tgt)
        ds = tgt.GetDesignSettings()
        via_rules = (
            int(getattr(ds, "m_ViasMinSize", 0) or 0),
            int(getattr(ds, "m_MinThroughDrill", 0) or 0),
            int(getattr(ds, "m_ViasMinAnnularWidth", 0) or 0),
        )

        def undo() -> None:
            self._rollback(tgt, added, removed)
            restore_fills(fills)
            tgt.BuildConnectivity()

        def rebuild() -> None:
            # BuildConnectivity() propagates pad nets through connected
            # copper: a copied track touching one pad of another net, or an
            # existing track crossed by copied copper, takes the other net,
            # which hides exactly the short this tool must report (and would
            # be saved). Put every track's and via's net back.
            tgt.BuildConnectivity()
            for t in tgt.GetTracks():
                uid = t.m_Uuid.AsString()
                want = intended.get(uid) or nets_before.get(uid)
                if want is not None and t.GetNetCode() != want.GetNetCode():
                    t.SetNet(want)

        def fit(via: Any) -> None:
            if not fit_vias or via.GetViaType() != pcbnew.VIATYPE_THROUGH:
                return
            w0, d0 = via_width(via), int(via.GetDrillValue())
            w, d = fit_via_size(w0, d0, *via_rules)
            if (w, d) != (w0, d0):
                via.SetWidth(w)
                via.SetDrill(d)
                key = f"{to_mm(w0, 3)}/{to_mm(d0, 3)} -> {to_mm(w, 3)}/{to_mm(d, 3)}"
                resized[key] = resized.get(key, 0) + 1
                resized_ids[via.m_Uuid.AsString()] = key

        try:
            if replace:
                removed = self._remove_existing(
                    tgt, tgt_poly, {net_map[n] for n in copy_nets}, include
                )
            existing = {geom_key(t) for t in tgt.GetTracks()}
            existing |= {geom_key(z) for z in tgt.Zones()}
            for kind, item in items:
                made = self._copy_item(tgt, kind, item, xform, mirror, net_map)
                if made is None:
                    skipped["layerMissing"] += 1
                    continue
                key = geom_key(made)
                if key in existing:
                    skipped["alreadyPresent"] += 1
                    if kind in ("zone", "rule_area"):  # already on the board
                        delete_board_item(tgt, made)
                    continue
                existing.add(key)
                if kind == "via":
                    fit(made)
                if kind not in ("zone", "rule_area"):  # zones are added by _copy_item
                    tgt.Add(made)
                added.append(made)
                origin[made.m_Uuid.AsString()] = item
                if kind not in ("zone", "rule_area"):
                    intended[made.m_Uuid.AsString()] = made.GetNet()
                key_name = {"track": "tracks", "arc": "arcs", "via": "vias", "zone": "zones"}
                copied[key_name.get(kind, "ruleAreas")] += 1
                if kind == "zone":
                    pairs.append((item, made))
            made_vias = self._add_pad_vias(tgt, pad_vias, fit, skipped)
            for v in made_vias:
                added.append(v)
                intended[v.m_Uuid.AsString()] = v.GetNet()
            copied["padVias"] = len(made_vias)
            if refill and (pairs or added):
                pcbnew.ZONE_FILLER(tgt).Fill(tgt.Zones())
            rebuild()
            matched_ids = check["matchedTargetPads"]
            report = self._post_report(
                src, tgt, xform, added, pairs, origin, copied_src_zones, matched_ids, limit, refill
            )
            dropped: List[Dict[str, Any]] = []
            if skip_conflicting and report["_conflictItems"]:
                bad_ids = report["_conflictItems"]
                for it in [a for a in added if a.m_Uuid.AsString() in bad_ids]:
                    p = it.GetPosition() if is_via(it) else it.GetStart()
                    dropped.append(
                        {
                            "type": "via" if is_via(it) else "track",
                            "net": it.GetNetname(),
                            "at": _xy((p.x, p.y)),
                        }
                    )
                    rkey = resized_ids.pop(it.m_Uuid.AsString(), None)
                    if rkey is not None:
                        resized[rkey] -= 1
                    added.remove(it)
                    delete_board_item(tgt, it)
                    kind_key = "vias" if is_via(it) else ("arcs" if is_arc(it) else "tracks")
                    copied[kind_key] = max(0, copied[kind_key] - 1)
                if refill:
                    pcbnew.ZONE_FILLER(tgt).Fill(tgt.Zones())
                rebuild()
                report = self._post_report(
                    src,
                    tgt,
                    xform,
                    added,
                    pairs,
                    origin,
                    copied_src_zones,
                    matched_ids,
                    limit,
                    refill,
                )
            report.pop("_conflictItems", None)
            if dropped:
                report["droppedConflicting"] = dropped[:limit]
                report["droppedConflictingCount"] = len(dropped)
            resized = {k: v for k, v in resized.items() if v > 0}
            if resized:
                report["viasResized"] = resized
            report["connectivity"] = {
                "unconnectedBefore": unconnected_before,
                "unconnectedAfter": int(tgt.GetConnectivity().GetUnconnectedCount(False)),
            }
        except Exception:
            undo()
            raise

        result.update(
            copied=copied,
            skipped=dict(skipped, partialZones=skipped["partialZones"][:limit]),
            removedExisting=len(removed),
            report=report,
        )
        shorts = report.get("shorts", [])
        if shorts and not force:
            undo()
            result["success"] = False
            result["rolledBack"] = True
            result["message"] = (
                f"Copied copper would short {report.get('shortCount', len(shorts))} time(s) "
                "against existing copper of other nets; the copy was rolled back "
                "(skipConflicting=true drops those items, force=true keeps them)"
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
        def by_number(fp: Any) -> Dict[str, List[Any]]:
            out: Dict[str, List[Any]] = {}
            for p in fp.Pads():
                if not str(p.GetNumber()):
                    continue  # paste apertures etc. carry no number
                out.setdefault(str(p.GetNumber()), []).append(p)
            return out

        def xy(p: Any) -> Tuple[int, int]:
            pos = p.GetPosition()
            return (pos.x, pos.y)

        sp, tp = by_number(src_fp), by_number(tgt_fp)
        issues: List[Dict[str, Any]] = []
        pairs: List[Tuple[Any, Any]] = []
        for number, plist in sp.items():
            other = tp.get(number)
            if not other:
                issues.append(
                    {
                        "type": "anchorPadMissing",
                        "sourcePad": f"{src_fp.GetReference()}.{number}",
                        "detail": "target anchor has no pad with this number",
                    }
                )
                continue
            if len(other) != len(plist):
                # e.g. an exposed pad split into an SMD pad plus via pads on one
                # side only: left out of the fit, verified by containment later
                continue
            # Several pads can share a number (exposed-pad arrays): pair them
            # by order of position, which the rigid fit tolerates.
            pairs += list(zip(sorted(plist, key=xy), sorted(other, key=xy)))
        distinct = {xy(a) for a, _ in pairs}
        if len(distinct) >= 2:
            xform, resid = fit_transform(
                [xy(a) for a, _ in pairs], [xy(b) for _, b in pairs], mirror
            )
            # Pads sharing a number may pair in the wrong order after rotation;
            # refit with only the unique numbers when that happens.
            if resid > tol:
                uniq = [
                    (plist[0], tp[number][0])
                    for number, plist in sp.items()
                    if len(plist) == 1 and len(tp.get(number) or []) == 1
                ]
                if len({xy(a) for a, _ in uniq}) >= 2:
                    pairs = uniq
                    xform, resid = fit_transform(
                        [xy(a) for a, _ in pairs], [xy(b) for _, b in pairs], mirror
                    )
        else:
            sp0, tp0 = src_fp.GetPosition(), tgt_fp.GetPosition()
            d = tgt_fp.GetOrientationDegrees() - src_fp.GetOrientationDegrees()
            xform = Transform(-math.radians(d), mirror, (sp0.x, sp0.y), (tp0.x, tp0.y))
            resid = 0.0
            pairs = []
        if resid > tol:
            # Footprints from different libraries differ by a few hundredths
            # (the RP2350B reference QFN has its pins 0.05 mm further in than
            # KiCad's): fine as long as every source pad centre still lands on
            # its target pad's copper.
            off = []
            for a, b in pairs:
                qx, qy = xform.apply(*xy(a))
                if math.hypot(qx - xy(b)[0], qy - xy(b)[1]) > tol and not b.HitTest(vec(qx, qy)):
                    off.append(_pad_label(a))
            if off:
                issues.append(
                    {
                        "type": "anchorGeometry",
                        "detail": (
                            f"anchor pads do not fit a rigid transform (max residual "
                            f"{to_mm(resid, 4)} mm; off their target pad: {', '.join(off[:8])}): "
                            "different footprints?"
                        ),
                    }
                )
        return xform, resid, issues

    def _verify_pads(
        self,
        src: Any,
        tgt: Any,
        src_anchor: Any,
        xform: Transform,
        src_poly: List[Tuple[float, float]],
        tgt_poly: List[Tuple[float, float]],
        tol: int,
        given_map: Dict[str, str],
    ) -> Dict[str, Any]:
        centres = ShapeIndex(cell_nm=to_nm(1.0))
        extents = ShapeIndex(cell_nm=to_nm(1.0))
        tgt_pads = []
        for fp in tgt.GetFootprints():
            for p in fp.Pads():
                if not _electrical(p):
                    continue
                pos = p.GetPosition()
                centres.add((pos.x, pos.y, pos.x, pos.y), (pos.x, pos.y, p))
                try:
                    extents.add(box_of(p.GetBoundingBox()), p)
                except Exception:
                    pass
                tgt_pads.append(p)
        anchor_id = src_anchor.m_Uuid.AsString()
        pairs: List[Tuple[Any, Any, Tuple[float, float]]] = []
        mismatches: List[Dict[str, Any]] = []
        offsets: List[Dict[str, Any]] = []
        checked = 0
        matched_tgt: Set[str] = set()
        for fp in src.GetFootprints():
            for p in fp.Pads():
                pos = p.GetPosition()
                if not point_in_poly(pos.x, pos.y, src_poly):
                    continue
                if not _electrical(p):
                    continue  # paste apertures / mechanical pads: not electrical
                checked += 1
                qx, qy = xform.apply(pos.x, pos.y)
                q = vec(qx, qy)
                # Prefer a pad with the same number when several share a spot
                # (exposed pads carry unnumbered aperture pads on top).
                best, best_key = None, None
                for x, y, cand in centres.query(grow((int(qx), int(qy), int(qx), int(qy)), tol)):
                    d = math.hypot(x - qx, y - qy)
                    if d > tol:
                        continue
                    key = (str(cand.GetNumber()) != str(p.GetNumber()), not cand.GetNetname(), d)
                    if best_key is None or key < best_key:
                        best, best_key = cand, key
                if best is None:
                    # Not centred within tolerance: look for a target pad whose
                    # copper covers the transformed centre.
                    for cand in extents.query((int(qx), int(qy), int(qx), int(qy))):
                        if not cand.HitTest(q):
                            continue
                        cp = cand.GetPosition()
                        d = math.hypot(cp.x - qx, cp.y - qy)
                        key = (
                            str(cand.GetNumber()) != str(p.GetNumber()),
                            not cand.GetNetname(),
                            d,
                        )
                        if best_key is None or key < best_key:
                            best, best_key = cand, key
                    if best is not None:
                        entry = {
                            "sourcePad": _pad_label(p),
                            "targetPad": _pad_label(best),
                            "offset": to_mm(best_key[2], 4),
                            "at": _xy((qx, qy)),
                        }
                        if self._pad_inside(p, best, xform):
                            # The whole source pad sits on the target pad's
                            # copper: an exposed pad split differently (the
                            # reference's via pads inside KiCad's single EP).
                            offsets.append(entry)
                        else:
                            # Overlapping but displaced: the part is placed
                            # off the reference position (or the footprints
                            # differ). Paired for the net checks, but it is
                            # a mismatch unless `tolerance` covers it.
                            entry.update(type="padOffset", sourceNet=p.GetNetname())
                            entry["hint"] = (
                                "target pad is displaced from the reference position; move "
                                "the part, or raise `tolerance` if the offset is intended"
                            )
                            mismatches.append(entry)
                if best is None:
                    entry = {
                        "type": "missingTargetPad",
                        "sourcePad": _pad_label(p),
                        "sourceNet": p.GetNetname(),
                        "expectedAt": _xy((qx, qy)),
                    }
                    near = None
                    for x, y, cand in centres.query(
                        grow((int(qx), int(qy), int(qx), int(qy)), to_nm(1.0))
                    ):
                        d = math.hypot(x - qx, y - qy)
                        if near is None or d < near[0]:
                            near = (d, cand)
                    if near is not None:
                        entry["nearestTargetPad"] = _pad_label(near[1])
                        entry["nearestDistance"] = to_mm(near[0], 4)
                    mismatches.append(entry)
                    continue
                matched_tgt.add(best.m_Uuid.AsString())
                pairs.append((p, best, (qx, qy)))

        def numbered(fp: Any) -> int:
            return sum(1 for pad in fp.Pads() if str(pad.GetNumber()))

        def describe(exclude: Set[str]) -> List[Dict[str, Any]]:
            return [
                {
                    "srcRef": _pad_ref(sp),
                    "srcNet": sp.GetNetname(),
                    "tgtRef": _pad_ref(tp),
                    "tgtNet": tp.GetNetname(),
                    "tgtPadCount": numbered(tp.GetParentFootprint()),
                }
                for sp, tp, _ in pairs
                if _pad_ref(tp) not in exclude and tp.GetParentFootprint() is not None
            ]

        def votes(exclude: Set[str]) -> List[Tuple[str, str, int]]:
            out = []
            for sp, tp, _ in pairs:
                if _pad_ref(tp) in exclude:
                    continue
                parent = sp.GetParentFootprint()
                anchor = parent is not None and parent.m_Uuid.AsString() == anchor_id
                out.append((sp.GetNetname(), tp.GetNetname(), ANCHOR_WEIGHT if anchor else 1))
            return out

        derived, ambiguous = derive_net_map(votes(set()), given_map)
        full = dict(derived, **given_map)
        reversed_parts = find_reversed_parts(describe(set()), full)
        if reversed_parts:
            # re-derive without the reversed parts' votes, then re-check
            skip = {r["targetRef"] for r in reversed_parts}
            derived, ambiguous = derive_net_map(votes(skip), given_map)
            full = dict(derived, **given_map)
            reversed_parts = find_reversed_parts(describe(set()), full)
        hints = {r["targetRef"]: r["hint"] for r in reversed_parts}

        for sp, tp, at in pairs:
            sn, tn = sp.GetNetname(), tp.GetNetname()
            if not sn:
                continue
            expected = full.get(sn)
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
                entry = {
                    "type": "netMismatch",
                    "sourcePad": _pad_label(sp),
                    "targetPad": _pad_label(tp),
                    "sourceNet": sn,
                    "expectedNet": expected,
                    "targetNet": tn,
                    "at": _xy(at),
                }
                if _pad_ref(tp) in hints:
                    entry["hint"] = hints[_pad_ref(tp)]
                mismatches.append(entry)
        # two source nets folded onto one target net is a likely mistake
        inverse: Dict[str, List[str]] = {}
        for sn, tn in full.items():
            inverse.setdefault(tn, []).append(sn)
        for tn, sns in inverse.items():
            if len(set(sns)) > 1:
                mismatches.append(
                    {
                        "type": "netMerge",
                        "targetNet": tn,
                        "sourceNets": sorted(set(sns)),
                        "detail": "several source nets map to one target net",
                    }
                )
        # plated holes in the source (e.g. via pads in an exposed pad) whose
        # target pad has no hole: candidates for board vias
        pad_vias = [(sp, tp, at) for sp, tp, at in pairs if _has_hole(sp) and not _has_hole(tp)]
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
        return {
            "info": info,
            "derived": derived,
            "mismatches": mismatches,
            "offsets": offsets,
            "reversed": reversed_parts,
            "ambiguous": ambiguous,
            "padVias": pad_vias,
            "matchedTargetPads": matched_tgt,
        }

    @staticmethod
    def _pad_inside(src_pad: Any, tgt_pad: Any, xform: Transform) -> bool:
        """True when the source pad's bounding box, transformed, lies on the target pad."""
        try:
            x0, y0, x1, y1 = box_of(src_pad.GetBoundingBox())
        except Exception:
            return False
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            if not tgt_pad.HitTest(vec(*xform.apply(x, y))):
                return False
        return True

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
    def _add_pad_vias(
        tgt: Any,
        pad_vias: List[Tuple[Any, Any, Tuple[float, float]]],
        fit: Any,
        skipped: Dict[str, Any],
    ) -> List[Any]:
        """Board vias where the source pad is a plated hole and the target pad is not."""
        index = ShapeIndex(cell_nm=to_nm(1.0))
        for t in tgt.GetTracks():
            if is_via(t):
                p = t.GetPosition()
                index.add((p.x, p.y, p.x, p.y), t)
        made = []
        for sp, tp, (qx, qy) in pad_vias:
            radius = max(_pad_size(sp) // 2, 1)
            box = grow((int(qx), int(qy), int(qx), int(qy)), radius)
            if any(
                v.GetNetCode() == tp.GetNetCode()
                and math.hypot(v.GetPosition().x - qx, v.GetPosition().y - qy) < radius
                for v in index.query(box)
            ):
                skipped["alreadyPresent"] += 1
                continue
            v = pcbnew.PCB_VIA(tgt)
            v.SetPosition(vec(qx, qy))
            v.SetViaType(pcbnew.VIATYPE_THROUGH)
            v.SetWidth(_pad_size(sp))
            v.SetDrill(int(min(sp.GetDrillSize().x, sp.GetDrillSize().y)))
            v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            v.SetNet(tp.GetNet())
            fit(v)
            tgt.Add(v)
            index.add((int(qx), int(qy), int(qx), int(qy)), v)
            made.append(v)
        return made

    @staticmethod
    def _remove_existing(
        tgt: Any, tgt_poly: List[Tuple[float, float]], nets: set, include: Sequence[str]
    ) -> List[Any]:
        """Detach target copper of the copied nets inside the region.

        Tracks and vias (``tracks``/``vias``; vias in a same-net pad, such as
        exposed-pad vias, are kept), zones of those nets (``zones``) and rule
        areas (``rule_areas``) lying wholly inside the region.
        """
        pads = [p for fp in tgt.GetFootprints() for p in fp.Pads()]
        out = []
        for t in list(tgt.GetTracks()):
            if t.GetNetname() not in nets:
                continue
            if is_via(t):
                if "vias" not in include:
                    continue
                p = t.GetPosition()
                pts = [(p.x, p.y)]
                if any(pad.HitTest(p) for pad in pads if pad.GetNetCode() == t.GetNetCode()):
                    continue  # exposed-pad / via-in-pad: keep
            else:
                if "tracks" not in include:
                    continue
                pts = [(t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y)]
            if all(point_in_poly(x, y, tgt_poly) for x, y in pts):
                out.append(detach_board_item(tgt, t))
        for z in list(tgt.Zones()):
            rule = bool(z.GetIsRuleArea())
            if ("rule_areas" if rule else "zones") not in include:
                continue
            if not rule and z.GetNetname() not in nets:
                continue
            pts = _zone_vertices(z)
            if pts and all(point_in_poly(x, y, tgt_poly) for x, y in pts):
                out.append(detach_board_item(tgt, z))
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
        self,
        src: Any,
        tgt: Any,
        xform: Transform,
        added: List[Any],
        pairs: List[Tuple[Any, Any]],
        origin: Dict[str, Any],
        copied_src_zones: Set[str],
        matched_pads: Set[str],
        limit: int,
        refilled: bool,
    ) -> Dict[str, Any]:
        conn = tgt.GetConnectivity()
        # pour islands: copied zone fills split into more pieces than in the source
        islands = []
        if pairs and refilled:
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
        # source pours that were not copied: copied copper that ended in one
        # dangles on the target (explained in the orphan report)
        lost_pours = [
            z
            for z in src.Zones()
            if not z.GetIsRuleArea() and z.m_Uuid.AsString() not in copied_src_zones
        ]

        def lost_pour_at(src_item: Any, x: float, y: float) -> Optional[str]:
            if src_item is None:
                return None
            sx, sy = xform.inverse(x, y)
            p = vec(sx, sy)
            for z in lost_pours:
                if z.GetNetCode() != src_item.GetNetCode():
                    continue
                layers = zone_copper_layers(z)
                if not any(src_item.IsOnLayer(lid) for lid in layers):
                    continue
                if z.Outline().Contains(p):
                    names = "/".join(layer_name(src, lid) for lid in layers)
                    return (
                        f"ended in the reference's {z.GetNetname()} pour on {names}, which was "
                        "not copied (it extends outside the region): connect it to a pour, "
                        "track or via of that net"
                    )
            return None

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
            src_item = origin.get(it.m_Uuid.AsString())
            if is_via(it):
                tracks = list(conn.GetConnectedTracks(it))
                pads = list(conn.GetConnectedPads(it))
                cluster = list(conn.GetConnectedItems(it))
                in_zone = any(isinstance(c, pcbnew.ZONE) for c in cluster)
                if not pads and len(tracks) <= 1 and not in_zone:
                    p = it.GetPosition()
                    entry = {
                        "type": "via",
                        "net": it.GetNetname(),
                        "at": _xy((p.x, p.y)),
                        "reason": (
                            "connected to nothing" if not tracks else "stub (one track only)"
                        ),
                    }
                    why = lost_pour_at(src_item, p.x, p.y)
                    if why:
                        entry["reason"] += "; " + why
                    orphans.append(entry)
                continue
            pos = pcbnew.VECTOR2I(0, 0)
            try:
                dangling = conn.TestTrackEndpointDangling(it, True, pos)
            except TypeError:
                dangling = conn.TestTrackEndpointDangling(it, True)
            if dangling:
                entry = {
                    "type": "arc" if is_arc(it) else "track",
                    "net": it.GetNetname(),
                    "layer": layer_name(tgt, it.GetLayer()),
                    "danglingAt": _xy((pos.x, pos.y)),
                }
                why = lost_pour_at(src_item, pos.x, pos.y)
                if why:
                    entry["reason"] = why
                orphans.append(entry)
        shorts, clearance, inherited, conflict_ids = self._conflicts(tgt, added, matched_pads)
        report: Dict[str, Any] = {
            "pourIslands": islands[:limit] if refilled else "not checked (refill=false)",
            "orphaned": orphans[:limit],
            "orphanedCount": len(orphans),
            "ruleAreas": rule_areas[:limit],
            "shorts": shorts[:limit],
            "shortCount": len(shorts),
            "clearance": clearance[:limit],
            "clearanceCount": len(clearance),
            "clearanceToMatchedPads": inherited[:limit],
            "clearanceToMatchedPadsCount": len(inherited),
            "_conflictItems": conflict_ids,
        }
        if clearance or inherited:
            report["clearanceNote"] = (
                "Net-class clearances only (custom DRC rules are not evaluated). 'clearance' is "
                "copied copper too close to unrelated existing copper; 'clearanceToMatchedPads' "
                "is the reference's own spacing to the parts it was designed around, made "
                "tighter or kept by the target's footprints/rules"
            )
        if rule_areas:
            report["ruleAreaNote"] = (
                "Freerouting exports every rule area as a keepout, including pour-only ones "
                "(pourOnly=true); strip them before autorouting or route this region by hand"
            )
        return report

    @staticmethod
    def _conflicts(
        tgt: Any, added: List[Any], matched_pads: Set[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], Set[str]]:
        """Copied tracks/vias that touch (short) or come within net-class clearance of
        copper of another net. Returns (shorts, clearance, clearance to matched pads,
        uuids of copied items that short or violate clearance to unrelated copper).
        """
        new_ids = {it.m_Uuid.AsString() for it in added}
        clr = netclass_clearance_fn(tgt)
        coppers = set(int(lid) for lid in tgt.GetEnabledLayers().CuStack())
        index = ShapeIndex()
        reach = 0
        for t in tgt.GetTracks():
            if t.m_Uuid.AsString() in new_ids:
                continue
            layers = (
                [int(lid) for lid in t.GetLayerSet().CuStack()] if is_via(t) else [t.GetLayer()]
            )
            shape = t.GetEffectiveShape(layers[0])
            index.add(shape_box(shape), (t.GetNetCode(), t, shape, set(layers), False))
        for fp in tgt.GetFootprints():
            for p in fp.Pads():
                layers = [int(lid) for lid in p.GetLayerSet().CuStack() if int(lid) in coppers]
                if not layers:
                    continue
                shape = p.GetEffectiveShape(layers[0])
                matched = p.m_Uuid.AsString() in matched_pads
                index.add(shape_box(shape), (p.GetNetCode(), p, shape, set(layers), matched))
        for it in added:
            reach = max(reach, clr(it.GetNetname()))
        reach += to_nm(0.5)  # room for a wider clearance on the other item's net class
        shorts: List[Dict[str, Any]] = []
        clearance: List[Dict[str, Any]] = []
        inherited: List[Dict[str, Any]] = []
        bad: Set[str] = set()
        for it in added:
            if isinstance(it, pcbnew.ZONE):
                continue
            via = is_via(it)
            own_layers = (
                {int(lid) for lid in it.GetLayerSet().CuStack()} if via else {int(it.GetLayer())}
            )
            shape = it.GetEffectiveShape(next(iter(own_layers)))
            p = it.GetPosition() if via else it.GetStart()
            kind = "via" if via else "track"
            short_hit = None
            near_hit = None
            for nc, other, oshape, olayers, matched in index.query(grow(shape_box(shape), reach)):
                if nc == it.GetNetCode() or not (own_layers & olayers):
                    continue
                need = max(clr(it.GetNetname()), clr(other.GetNetname()))
                try:
                    if oshape.Collide(shape, 0):
                        short_hit = other
                        break
                    if near_hit is None and oshape.Collide(shape, max(need - 1, 0)):
                        near_hit = (other, need, matched)
                except Exception as e:
                    logger.debug(f"transplant collide failed: {e}")
            if short_hit is not None:
                shorts.append(
                    {
                        "net": it.GetNetname(),
                        "otherNet": short_hit.GetNetname(),
                        "type": kind,
                        "at": _xy((p.x, p.y)),
                    }
                )
                bad.add(it.m_Uuid.AsString())
                continue
            if near_hit is not None:
                other, need, matched = near_hit
                entry = {
                    "net": it.GetNetname(),
                    "otherNet": other.GetNetname(),
                    "other": (
                        _pad_label(other)
                        if isinstance(other, pcbnew.PAD)
                        else ("via" if is_via(other) else "track")
                    ),
                    "type": kind,
                    "required": to_mm(need, 4),
                    "at": _xy((p.x, p.y)),
                }
                if matched:
                    inherited.append(entry)
                else:
                    clearance.append(entry)
                    bad.add(it.m_Uuid.AsString())
        return shorts, clearance, inherited, bad
