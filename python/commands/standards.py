"""Check a board against its project "standards profile" (MCP_ISSUES §2.9, C3).

The profile (``standards.yaml``/``.yml``/``.json`` next to the project, see
``utils.standards_profile``) holds design intent as data. ``check_standards``
evaluates it against the loaded board and returns the same compact,
delta-aware shape as ``run_drc`` (``utils.check_summary``, history kind
``standards``). ``run_drc`` appends the same summary when a profile exists.

Checks:

* ``fab_*``                 -- items below the fab's minimums (track width, drill,
                               annular ring, silkscreen text) and board rules
                               looser than the fab (clearance, track, via)
* ``region_intrusion``      -- copper of a net that is not in a region's
                               ``allowed_nets`` (or is in ``forbidden_nets``)
* ``region_forbidden_net``  -- ``nets``/``netclasses`` entries with ``forbid_regions``
* ``plane_split_crossing``  -- a track of a ``no_plane_split_crossing`` class whose
                               nearest plane changes net (or vanishes) under it.
                               Plane coverage comes from the zones actually on the
                               plane layer, not from the profile, so it reflects
                               the board as built.
* ``netclass_min_width``    -- tracks narrower than a profile class's ``width``
* ``netclass_layer_preference`` (info) -- length routed off a class's ``prefer`` layer
* ``gnd_pad_without_via``   -- ``checks.every_gnd_smd_pad_has_via_within``
* ``exposed_pad_without_vias`` -- ``checks.exposed_pads_have_via_array``
* ``decoupling_too_far``    -- ``checks.decoupling_within_mm``

Geometry is read from the board once into plain lists (``utils.poly2d``), so
the checks never hold SWIG polygon objects (MCP_ISSUES #5r).
"""

import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from utils import check_summary
from utils import poly2d as g
from utils.standards_profile import (
    ProfileError,
    find_profile,
    load_profile,
    net_matches,
    net_matches_any,
    normalize_profile,
)

logger = logging.getLogger("kicad_interface")

MM = 1_000_000.0
KNOWN_CHECKS = {
    "every_gnd_smd_pad_has_via_within",
    "exposed_pads_have_via_array",
    "decoupling_within_mm",
}
_GROUND_RE = re.compile(r"^(A|D|P|SH|CHASSIS)?GND\w*$|^VSS\w*$|^GND", re.IGNORECASE)
_SUPPLY_RE = re.compile(r"^\+|VDD|VCC|^VBUS$|^VBAT$|^VIN$|^VIO|^V\d|^\d+V\d*", re.IGNORECASE)
# Signal nets named after a rail (VBUS_SENSE, VDD_EN, ...) are not supplies.
_NOT_SUPPLY_RE = re.compile(
    r"SENSE|_EN$|^EN_|_EN_|FLT|FAULT|_PG$|PGOOD|DET|_DIV$|_FB$|_SW$|_LX$", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Board snapshot
# ---------------------------------------------------------------------------


def _layer_name(board: Any, layer_id: int) -> str:
    try:
        name = board.GetStandardLayerName(layer_id)
        if isinstance(name, str) and name:
            return name
    except Exception:
        pass
    return board.GetLayerName(layer_id)


def _xy(v: Any) -> Tuple[float, float]:
    return v.x / MM, v.y / MM


def _uuid(item: Any) -> Optional[str]:
    try:
        u = item.m_Uuid.AsString()
        return u if isinstance(u, str) else None
    except Exception:
        return None


def snapshot_board(board: Any) -> Dict[str, Any]:
    """Read everything the checks need into plain Python data (mm)."""
    import pcbnew

    stack = [_layer_name(board, lid) for lid in board.GetEnabledLayers().CuStack()]
    tracks, vias = [], []
    for t in board.GetTracks():
        cls = t.GetClass()
        net = t.GetNetname()
        if cls == "PCB_VIA":
            try:
                top, bot = t.TopLayer(), t.BottomLayer()
                layers = stack[
                    stack.index(_layer_name(board, top)) : stack.index(_layer_name(board, bot)) + 1
                ]
            except Exception:
                layers = list(stack)
            try:
                size = t.GetWidth(pcbnew.F_Cu) / MM
            except TypeError:
                size = t.GetWidth() / MM
            vias.append(
                {
                    "uuid": _uuid(t),
                    "net": net,
                    "at": _xy(t.GetPosition()),
                    "size": size,
                    "drill": t.GetDrillValue() / MM,
                    "layers": layers,
                    "locked": bool(t.IsLocked()),
                }
            )
            continue
        layer = _layer_name(board, t.GetLayer())
        width = t.GetWidth() / MM
        pts = [_xy(t.GetStart()), _xy(t.GetEnd())]
        if cls == "PCB_ARC":
            try:
                pts = [_xy(t.GetStart()), _xy(t.GetMid()), _xy(t.GetEnd())]
            except Exception:
                pass
        for a, b in zip(pts, pts[1:]):
            tracks.append(
                {
                    "uuid": _uuid(t),
                    "net": net,
                    "layer": layer,
                    "a": a,
                    "b": b,
                    "width": width,
                    "kind": "arc" if cls == "PCB_ARC" else "track",
                    "locked": bool(t.IsLocked()),
                }
            )

    pads, footprints = [], []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        fp_pads = []
        for p in fp.Pads():
            try:
                lset = p.GetLayerSet()
                cu = [_layer_name(board, lid) for lid in lset.CuStack()]
            except Exception:
                cu = []
            bb = p.GetBoundingBox()
            drill = 0.0
            try:
                ds = p.GetDrillSize()
                drill = min(ds.x, ds.y) / MM
            except Exception:
                pass
            try:
                size = p.GetSize(pcbnew.F_Cu)
            except TypeError:
                size = p.GetSize()
            try:
                pin_type = p.GetPinType()
            except Exception:
                pin_type = ""
            entry = {
                "uuid": _uuid(p),
                "ref": ref,
                "number": p.GetNumber(),
                "net": p.GetNetname(),
                "at": _xy(p.GetPosition()),
                "bbox": (
                    bb.GetLeft() / MM,
                    bb.GetTop() / MM,
                    bb.GetRight() / MM,
                    bb.GetBottom() / MM,
                ),
                "size": (size.x / MM, size.y / MM),
                "layers": cu,
                "smd": p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD,
                "pth": p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
                "drill": drill,
                "pinType": pin_type if isinstance(pin_type, str) else "",
            }
            pads.append(entry)
            fp_pads.append(entry)
        footprints.append({"ref": ref, "at": _xy(fp.GetPosition()), "pads": fp_pads, "fp": fp})

    zones, rule_areas = [], []
    for z in board.Zones():
        try:
            layers = [_layer_name(board, lid) for lid in z.GetLayerSet().Seq()]
        except Exception:
            layers = []
        entry = {
            "uuid": _uuid(z),
            "name": z.GetZoneName(),
            "net": z.GetNetname(),
            "layers": layers,
            "priority": z.GetAssignedPriority(),
            "polygons": g.zone_polygons(z),
        }
        if z.GetIsRuleArea():
            entry.update(
                {
                    "tracks": bool(z.GetDoNotAllowTracks()),
                    "vias": bool(z.GetDoNotAllowVias()),
                    "pads": bool(z.GetDoNotAllowPads()),
                    "pours": bool(z.GetDoNotAllowZoneFills()),
                    "footprints": bool(z.GetDoNotAllowFootprints()),
                }
            )
            rule_areas.append(entry)
        else:
            zones.append(entry)
    return {
        "stack": stack,
        "tracks": tracks,
        "vias": vias,
        "pads": pads,
        "footprints": footprints,
        "zones": zones,
        "ruleAreas": rule_areas,
    }


# ---------------------------------------------------------------------------
# Violation helpers
# ---------------------------------------------------------------------------


def _violation(
    vtype: str,
    severity: str,
    message: str,
    at: Optional[Tuple[float, float]],
    items: List[Tuple[str, Optional[str], Optional[Tuple[float, float]]]],
    key: str,
) -> Dict[str, Any]:
    loc = {"x": round(at[0], 4), "y": round(at[1], 4)} if at else {}
    return {
        "key": f"{vtype}|{key}",
        "type": vtype,
        "severity": severity,
        "message": message,
        "location": loc,
        "category": "standards",
        "items": [
            {
                "description": d,
                "uuid": u,
                "pos": {"x": round(p[0], 4), "y": round(p[1], 4)} if p else None,
            }
            for d, u, p in items
        ],
    }


def _fmt(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_fab(board: Any, snap: Dict[str, Any], fab: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not fab:
        return out

    def num(key: str) -> Optional[float]:
        try:
            return float(fab[key]) if fab.get(key) is not None else None
        except (TypeError, ValueError):
            return None

    min_track, min_drill, min_annular = num("min_track"), num("min_drill"), num("min_annular")
    min_space = num("min_space")
    eps = 1e-6
    if min_track is not None:
        for t in snap["tracks"]:
            if t["width"] < min_track - eps:
                out.append(
                    _violation(
                        "fab_track_width",
                        "error",
                        f"Track [{t['net']}] on {t['layer']} is {_fmt(t['width'])} mm wide "
                        f"(fab minimum {_fmt(min_track)} mm)",
                        t["a"],
                        [(f"Track [{t['net']}] on {t['layer']}", t["uuid"], t["a"])],
                        f"{t['uuid']}",
                    )
                )
    for v in snap["vias"]:
        if min_drill is not None and v["drill"] < min_drill - eps:
            out.append(
                _violation(
                    "fab_drill",
                    "error",
                    f"Via [{v['net']}] drill {_fmt(v['drill'])} mm (fab minimum {_fmt(min_drill)} mm)",
                    v["at"],
                    [(f"Via [{v['net']}]", v["uuid"], v["at"])],
                    f"{v['uuid']}",
                )
            )
        ring = (v["size"] - v["drill"]) / 2
        if min_annular is not None and ring < min_annular - eps:
            out.append(
                _violation(
                    "fab_annular_ring",
                    "error",
                    f"Via [{v['net']}] annular ring {_fmt(ring)} mm "
                    f"(fab minimum {_fmt(min_annular)} mm)",
                    v["at"],
                    [(f"Via [{v['net']}]", v["uuid"], v["at"])],
                    f"{v['uuid']}",
                )
            )
    for p in snap["pads"]:
        if not p["pth"] or p["drill"] <= 0:
            continue
        label = f"Pad {p['number']} [{p['net']}] of {p['ref']}"
        if min_drill is not None and p["drill"] < min_drill - eps:
            out.append(
                _violation(
                    "fab_drill",
                    "error",
                    f"{label} drill {_fmt(p['drill'])} mm (fab minimum {_fmt(min_drill)} mm)",
                    p["at"],
                    [(label, p["uuid"], p["at"])],
                    f"{p['uuid']}",
                )
            )
        ring = (min(p["size"]) - p["drill"]) / 2
        if min_annular is not None and ring < min_annular - eps:
            out.append(
                _violation(
                    "fab_annular_ring",
                    "error",
                    f"{label} annular ring {_fmt(ring)} mm (fab minimum {_fmt(min_annular)} mm)",
                    p["at"],
                    [(label, p["uuid"], p["at"])],
                    f"{p['uuid']}",
                )
            )

    layers = fab.get("layers")
    if layers is not None and int(layers) != len(snap["stack"]):
        out.append(
            _violation(
                "fab_layer_count",
                "warning",
                f"Board has {len(snap['stack'])} copper layers; the fab profile says {layers}",
                None,
                [],
                "board",
            )
        )

    # Board rules that would let DRC pass items the fab cannot make.
    try:
        ds = board.GetDesignSettings()
        looser = []
        for attr, limit, label in (
            ("m_TrackMinWidth", min_track, "minimum track width"),
            ("m_MinClearance", min_space, "minimum clearance"),
            ("m_MinThroughDrill", min_drill, "minimum through-hole drill"),
            ("m_ViasMinAnnularWidth", min_annular, "minimum annular width"),
        ):
            if limit is None or not hasattr(ds, attr):
                continue
            value = getattr(ds, attr) / MM
            if 0 < value < limit - eps or value == 0:
                looser.append(f"{label} {_fmt(value)} mm < fab {_fmt(limit)} mm")
        if looser:
            out.append(
                _violation(
                    "fab_rules_looser_than_fab",
                    "warning",
                    "Board design rules allow less than the fab can make: " + "; ".join(looser),
                    None,
                    [],
                    "board",
                )
            )
    except Exception as exc:  # pragma: no cover - SWIG differences
        logger.debug(f"design-settings comparison skipped: {exc}")

    silk_h, silk_w = num("silk_min_height"), num("silk_min_width")
    if silk_h is not None or silk_w is not None:
        out += _check_silk(board, silk_h, silk_w)
    return out


def _check_silk(board: Any, min_h: Optional[float], min_w: Optional[float]) -> List[Dict[str, Any]]:
    import pcbnew

    out = []
    silk = {pcbnew.F_SilkS, pcbnew.B_SilkS}
    texts = []
    for fp in board.GetFootprints():
        for field in list(fp.GetFields()) if hasattr(fp, "GetFields") else []:
            texts.append((fp.GetReference(), field))
        for item in fp.GraphicalItems():
            if item.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX"):
                texts.append((fp.GetReference(), item))
    for item in board.GetDrawings():
        if item.GetClass() in ("PCB_TEXT", "PCB_TEXTBOX"):
            texts.append((None, item))
    for ref, t in texts:
        try:
            if t.GetLayer() not in silk or not t.IsVisible():
                continue
            h = t.GetTextHeight() / MM
            w = t.GetTextThickness() / MM
            text = t.GetShownText(False) if hasattr(t, "GetShownText") else t.GetText()
        except Exception:
            continue
        if not text:
            continue
        problems = []
        if min_h is not None and h < min_h - 1e-6:
            problems.append(f"height {_fmt(h)} mm < {_fmt(min_h)} mm")
        if min_w is not None and w < min_w - 1e-6:
            problems.append(f"stroke {_fmt(w)} mm < {_fmt(min_w)} mm")
        if problems:
            at = _xy(t.GetPosition())
            owner = f" of {ref}" if ref else ""
            out.append(
                _violation(
                    "fab_silk_text",
                    "warning",
                    f"Silkscreen text '{text}'{owner}: " + ", ".join(problems),
                    at,
                    [(f"Text '{text}'{owner}", _uuid(t), at)],
                    f"{_uuid(t)}",
                )
            )
    return out


def _region_offenders(
    snap: Dict[str, Any], poly: g.Polygon, layers: Optional[List[str]], net_ok
) -> Dict[str, Dict[str, Any]]:
    """{net: {counts, first position, uuids}} of copper inside ``poly`` that ``net_ok`` rejects."""
    found: Dict[str, Dict[str, Any]] = {}
    box = g.bbox(poly)

    def hit(net: str, kind: str, at, uuid) -> None:
        e = found.setdefault(net, {"tracks": 0, "vias": 0, "pads": 0, "zones": 0, "items": []})
        e[kind] += 1
        if len(e["items"]) < 5:
            e["items"].append((kind, at, uuid))

    def ok(net: str) -> bool:
        return not net or net.startswith("unconnected-") or net_ok(net)

    for t in snap["tracks"]:
        if ok(t["net"]) or (layers and t["layer"] not in layers):
            continue
        if not g.bbox_overlap(g.bbox([t["a"], t["b"]]), box):
            continue
        if g.segment_enters_polygon(t["a"], t["b"], poly):
            hit(t["net"], "tracks", t["a"], t["uuid"])
    for v in snap["vias"]:
        if ok(v["net"]) or (layers and not set(layers) & set(v["layers"])):
            continue
        if g.point_strictly_inside(v["at"], poly):
            hit(v["net"], "vias", v["at"], v["uuid"])
    for p in snap["pads"]:
        if ok(p["net"]) or (layers and not set(layers) & set(p["layers"])):
            continue
        if g.point_strictly_inside(p["at"], poly):
            hit(p["net"], "pads", p["at"], p["uuid"])
    for z in snap["zones"]:
        if ok(z["net"]) or (layers and not set(layers) & set(z["layers"])):
            continue
        for outline, _holes in z["polygons"]:
            if g.polygons_overlap_strictly(outline, poly):
                inside = next((v for v in outline if g.point_strictly_inside(v, poly)), poly[0])
                hit(z["net"], "zones", inside, z["uuid"])
                break
    return found


def _region_violations(
    vtype: str, region: str, found: Dict[str, Dict[str, Any]], why: str
) -> List[Dict[str, Any]]:
    out = []
    for net, e in sorted(found.items()):
        parts = [f"{e[k]} {k}" for k in ("tracks", "vias", "pads", "zones") if e[k]]
        first = e["items"][0][1] if e["items"] else None
        out.append(
            _violation(
                vtype,
                "error",
                f"Net [{net}] has copper inside region '{region}' ({', '.join(parts)}); {why}",
                first,
                [(f"{kind[:-1].capitalize()} [{net}]", uuid, at) for kind, at, uuid in e["items"]],
                f"{region}|{net}",
            )
        )
    return out


def check_regions(snap: Dict[str, Any], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    regions = profile["regions"]
    for name, reg in regions.items():
        allowed, forbidden = reg["allowed_nets"], reg["forbidden_nets"]
        if not allowed and not forbidden:
            continue

        def net_ok(net: str, allowed=allowed, forbidden=forbidden) -> bool:
            if forbidden and net_matches_any(net, forbidden):
                return False
            return net_matches_any(net, allowed) if allowed else True

        found = _region_offenders(snap, reg["polygon"], reg["layers"], net_ok)
        why = "it is not in the region's allowed_nets" if allowed else "it is in forbidden_nets"
        out += _region_violations("region_intrusion", name, found, why)

    # nets: / netclasses: forbid_regions
    rules: List[Tuple[str, List[str], str]] = []
    for net, spec in profile["nets"].items():
        for r in spec["forbid_regions"]:
            rules.append((r, [net], f"net entry '{net}' forbids it"))
    for cls, spec in profile["netclasses"].items():
        for r in spec["forbid_regions"]:
            rules.append((r, spec["nets"], f"net class '{cls}' forbids it"))
    for region, patterns, why in rules:
        reg = regions[region]
        found = _region_offenders(
            snap,
            reg["polygon"],
            reg["layers"],
            lambda net, patterns=patterns: not net_matches_any(net, patterns),
        )
        out += _region_violations("region_forbidden_net", region, found, why)
    return out


def _plane_layers(board: Any, snap: Dict[str, Any], profile: Dict[str, Any]) -> List[str]:
    planes = [layer for layer in profile["planes"] if layer in snap["stack"]]
    if planes:
        return planes
    import pcbnew

    found = []
    for lid in board.GetEnabledLayers().CuStack():
        try:
            if board.GetLayerType(lid) == pcbnew.LT_POWER:
                found.append(_layer_name(board, lid))
        except Exception:
            pass
    return found


def _coverage(zones: List[Dict[str, Any]], pt: Tuple[float, float]) -> Optional[str]:
    for z in zones:  # sorted by priority, highest first
        for outline, holes in z["polygons"]:
            if g.point_in_shape(pt, outline, holes):
                return z["net"]
    return None


def check_netclasses(
    board: Any, snap: Dict[str, Any], profile: Dict[str, Any], step: float = 0.25
) -> Tuple[List[Dict[str, Any]], List[str]]:
    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    classes = profile["netclasses"]
    if not classes:
        return out, notes
    stack = snap["stack"]
    planes = _plane_layers(board, snap, profile)
    plane_zones = {
        layer: sorted(
            [z for z in snap["zones"] if layer in z["layers"] and z["net"]],
            key=lambda z: -z["priority"],
        )
        for layer in planes
    }

    def ref_planes(layer: str) -> List[str]:
        if layer not in stack or not planes or layer in planes:
            return []
        i = stack.index(layer)
        dist = {p: abs(stack.index(p) - i) for p in planes}
        best = min(dist.values())
        return [p for p, d in dist.items() if d == best]

    split_classes = {n: c for n, c in classes.items() if c["no_plane_split_crossing"]}
    if split_classes and not planes:
        notes.append(
            "no_plane_split_crossing: no plane layers found (declare them under 'planes' or "
            "set the layer type to power), so plane crossings were not checked"
        )

    for cls_name, cls in classes.items():
        members = [t for t in snap["tracks"] if t["net"] and net_matches_any(t["net"], cls["nets"])]
        if not members:
            continue
        # Width
        if cls["width"] is not None:
            thin: Dict[str, List[Dict[str, Any]]] = {}
            for t in members:
                if t["width"] < cls["width"] - 1e-6:
                    thin.setdefault(t["net"], []).append(t)
            for net, ts in sorted(thin.items()):
                narrowest = min(x["width"] for x in ts)
                out.append(
                    _violation(
                        "netclass_min_width",
                        "warning",
                        f"Net [{net}] (class {cls_name}) has {len(ts)} track segment(s) narrower "
                        f"than {_fmt(cls['width'])} mm (narrowest {_fmt(narrowest)} mm)",
                        ts[0]["a"],
                        [(f"Track [{net}] on {x['layer']}", x["uuid"], x["a"]) for x in ts[:5]],
                        f"{cls_name}|{net}",
                    )
                )
        # Preferred layer
        if cls["prefer"]:
            off: Dict[str, float] = {}
            first: Dict[str, Dict[str, Any]] = {}
            for t in members:
                if t["layer"] != cls["prefer"]:
                    length = math.hypot(t["b"][0] - t["a"][0], t["b"][1] - t["a"][1])
                    off[t["net"]] = off.get(t["net"], 0.0) + length
                    first.setdefault(t["net"], t)
            for net, length in sorted(off.items()):
                t = first[net]
                out.append(
                    _violation(
                        "netclass_layer_preference",
                        "info",
                        f"Net [{net}] (class {cls_name}) has {_fmt(length)} mm routed off its "
                        f"preferred layer {cls['prefer']}",
                        t["a"],
                        [(f"Track [{net}] on {t['layer']}", t["uuid"], t["a"])],
                        f"{cls_name}|{net}",
                    )
                )
        # Plane-split crossings
        if not cls["no_plane_split_crossing"] or not planes:
            continue
        crossings: Dict[Tuple[str, str], List[Tuple[str, Tuple[float, float], Optional[str]]]] = {}
        for t in members:
            for plane in ref_planes(t["layer"]):
                zones = plane_zones.get(plane) or []
                pts = g.sample_segment(t["a"], t["b"], step)
                prev = _coverage(zones, pts[0])
                for pt in pts[1:]:
                    cur = _coverage(zones, pt)
                    if cur != prev:
                        label = f"{prev or '(no plane)'} -> {cur or '(no plane)'}"
                        crossings.setdefault((t["net"], plane), []).append((label, pt, t["uuid"]))
                        prev = cur
        for (net, plane), hits in sorted(crossings.items()):
            out.append(
                _violation(
                    "plane_split_crossing",
                    "error",
                    f"Net [{net}] (class {cls_name}) crosses {len(hits)} split(s) in its "
                    f"reference plane {plane}: "
                    + "; ".join(f"{lbl} at ({_fmt(p[0])}, {_fmt(p[1])})" for lbl, p, _ in hits[:4]),
                    hits[0][1],
                    [(f"Track [{net}] over {plane}: {lbl}", u, p) for lbl, p, u in hits[:5]],
                    f"{net}|{plane}",
                )
            )
    return out, notes


class _Grid:
    """Bucket points for near-neighbour queries."""

    def __init__(self, items: List[Dict[str, Any]], key: str = "at", cell: float = 2.0):
        self.cell = cell
        self.key = key
        self.buckets: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        for it in items:
            x, y = it[key]
            self.buckets.setdefault((int(x // cell), int(y // cell)), []).append(it)

    def near(self, pt: Tuple[float, float], radius: float) -> List[Dict[str, Any]]:
        r = int(math.ceil(radius / self.cell))
        cx, cy = int(pt[0] // self.cell), int(pt[1] // self.cell)
        out = []
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                out += self.buckets.get((i, j), [])
        return out


def _box_distance(pt: Tuple[float, float], box: Tuple[float, float, float, float]) -> float:
    dx = max(box[0] - pt[0], 0.0, pt[0] - box[2])
    dy = max(box[1] - pt[1], 0.0, pt[1] - box[3])
    return math.hypot(dx, dy)


def check_gnd_vias(snap: Dict[str, Any], spec: Any) -> List[Dict[str, Any]]:
    if isinstance(spec, dict):
        distance = float(spec.get("distance", spec.get("mm", 1.0)))
        nets = [str(n) for n in (spec.get("nets") or ["GND"])]
    else:
        distance, nets = float(spec), ["GND"]
    anchors = [dict(v, kind="via") for v in snap["vias"]] + [
        dict(p, kind="pad") for p in snap["pads"] if p["pth"]
    ]
    grid = _Grid(anchors)
    out = []
    for p in snap["pads"]:
        if not p["smd"] or not p["net"] or not net_matches_any(p["net"], nets):
            continue
        best = None
        for a in grid.near(p["at"], distance + 5.0):
            if a["net"] != p["net"] or (a["kind"] == "pad" and a["uuid"] == p["uuid"]):
                continue
            d = _box_distance(a["at"], p["bbox"])
            if best is None or d < best:
                best = d
        if best is not None and best <= distance + 1e-6:
            continue
        nearest = f"nearest {_fmt(best)} mm" if best is not None else "none nearby"
        label = f"Pad {p['number']} [{p['net']}] of {p['ref']}"
        out.append(
            _violation(
                "gnd_pad_without_via",
                "warning",
                f"{label} has no {p['net']} via within {_fmt(distance)} mm ({nearest})",
                p["at"],
                [(label, p["uuid"], p["at"])],
                f"{p['uuid']}",
            )
        )
    return out


def check_exposed_pads(snap: Dict[str, Any], spec: Any) -> List[Dict[str, Any]]:
    if spec is False or spec is None:
        return []
    min_vias = int(spec.get("min_vias", 1)) if isinstance(spec, dict) else 1
    min_area = float(spec.get("min_area", 2.0)) if isinstance(spec, dict) else 2.0
    out = []
    for fp in snap["footprints"]:
        pads = fp["pads"]
        if len(pads) < 5:
            continue
        seen = set()
        for p in pads:
            area = p["size"][0] * p["size"][1]
            named_ep = str(p["number"]).upper().startswith("EP")
            centred = math.hypot(p["at"][0] - fp["at"][0], p["at"][1] - fp["at"][1]) < 0.75
            if not p["smd"] or not p["net"] or not (named_ep or (area >= min_area and centred)):
                continue
            if p["number"] in seen:  # one report per pad number (EPs come in pieces)
                continue
            seen.add(p["number"])
            # Board vias, or the footprint's own plated holes (thermal-via pads).
            anchors = snap["vias"] + [q for q in pads if q["pth"]]
            inside = [
                v
                for v in anchors
                if v["net"] == p["net"]
                and p["bbox"][0] <= v["at"][0] <= p["bbox"][2]
                and p["bbox"][1] <= v["at"][1] <= p["bbox"][3]
            ]
            if len(inside) >= min_vias:
                continue
            label = f"Pad {p['number']} [{p['net']}] of {p['ref']}"
            out.append(
                _violation(
                    "exposed_pad_without_vias",
                    "warning",
                    f"Exposed pad {label} has {len(inside)} via(s) (want at least {min_vias})",
                    p["at"],
                    [(label, p["uuid"], p["at"])],
                    f"{p['uuid']}",
                )
            )
    return out


def _is_ground(net: str) -> bool:
    return bool(_GROUND_RE.search(net.rsplit("/", 1)[-1]))


def check_decoupling(snap: Dict[str, Any], spec: Any) -> List[Dict[str, Any]]:
    if isinstance(spec, dict):
        distance = float(spec.get("distance", spec.get("mm", 3.0)))
        ic_prefixes = [str(x) for x in (spec.get("ic_prefixes") or ["U"])]
        cap_prefixes = [str(x) for x in (spec.get("cap_prefixes") or ["C"])]
        nets = [str(x) for x in (spec.get("nets") or [])]
        ignore = [str(x) for x in (spec.get("ignore_nets") or [])]
    else:
        distance, ic_prefixes, cap_prefixes, nets, ignore = float(spec), ["U"], ["C"], [], []

    def has_prefix(ref: str, prefixes: List[str]) -> bool:
        return any(re.match(rf"^{re.escape(px)}\d", ref) for px in prefixes)

    def supply(p: Dict[str, Any]) -> bool:
        net = p["net"]
        if not net or net.startswith("unconnected-") or _is_ground(net):
            return False
        if ignore and net_matches_any(net, ignore):
            return False
        if nets:
            return net_matches_any(net, nets)
        if p["pinType"] in ("power_in",):
            return True
        leaf = net.rsplit("/", 1)[-1]
        return bool(_SUPPLY_RE.search(leaf)) and not _NOT_SUPPLY_RE.search(leaf)

    cap_pads: Dict[str, List[Dict[str, Any]]] = {}
    for p in snap["pads"]:
        if has_prefix(p["ref"], cap_prefixes) and p["net"]:
            cap_pads.setdefault(p["net"], []).append(p)
    out = []
    for p in snap["pads"]:
        if not has_prefix(p["ref"], ic_prefixes) or not supply(p):
            continue
        caps = cap_pads.get(p["net"], [])
        best = None
        for c in caps:
            d = math.hypot(c["at"][0] - p["at"][0], c["at"][1] - p["at"][1])
            if best is None or d < best[0]:
                best = (d, c)
        if best is not None and best[0] <= distance + 1e-6:
            continue
        label = f"Pad {p['number']} [{p['net']}] of {p['ref']}"
        detail = (
            f"nearest {p['net']} capacitor pad is {best[1]['ref']} at {_fmt(best[0])} mm"
            if best
            else f"no capacitor on {p['net']}"
        )
        out.append(
            _violation(
                "decoupling_too_far",
                "warning",
                f"{label}: {detail} (want <= {_fmt(distance)} mm)",
                p["at"],
                [(label, p["uuid"], p["at"])]
                + (
                    [(f"{best[1]['ref']} pad {best[1]['number']}", best[1]["uuid"], best[1]["at"])]
                    if best
                    else []
                ),
                f"{p['uuid']}",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def resolve_profile(
    board_file: Optional[str], params: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(normalised profile, source) from ``profile``/``profilePath``/the project dir."""
    inline = params.get("profile")
    if isinstance(inline, dict):
        return normalize_profile(inline), "inline"
    path = params.get("profilePath")
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ProfileError(f"profile file not found: {path}")
        return load_profile(path), path
    project_dir = None
    pp = params.get("projectPath")
    if pp:
        pp = os.path.abspath(os.path.expanduser(str(pp)))
        project_dir = pp if os.path.isdir(pp) else os.path.dirname(pp)
    elif board_file:
        project_dir = os.path.dirname(os.path.abspath(board_file))
    found = find_profile(project_dir)
    if not found:
        return None, None
    return load_profile(found), found


def evaluate(board: Any, profile: Dict[str, Any], only: Optional[List[str]] = None):
    """Run every applicable check; returns (violations, checksRun, skipped, notes)."""
    snap = snapshot_board(board)
    violations: List[Dict[str, Any]] = []
    ran: List[str] = []
    skipped: List[str] = []
    notes: List[str] = []

    def want(name: str) -> bool:
        return not only or name in only

    if profile["fab"] and want("fab"):
        violations += check_fab(board, snap, profile["fab"])
        ran.append("fab")
    if (profile["regions"] or profile["nets"]) and want("regions"):
        violations += check_regions(snap, profile)
        ran.append("regions")
    if profile["netclasses"] and want("netclasses"):
        found, n = check_netclasses(board, snap, profile)
        violations += found
        notes += n
        ran.append("netclasses")
    checks = profile["checks"]
    for name, spec in checks.items():
        if not want(name) and not want("checks"):
            continue
        if spec in (None, False):
            continue
        try:
            if name == "every_gnd_smd_pad_has_via_within":
                violations += check_gnd_vias(snap, spec)
            elif name == "exposed_pads_have_via_array":
                violations += check_exposed_pads(snap, spec)
            elif name == "decoupling_within_mm":
                violations += check_decoupling(snap, spec)
            else:
                skipped.append(name)
                continue
        except (TypeError, ValueError) as exc:
            notes.append(f"check '{name}' has an invalid setting: {exc}")
            continue
        ran.append(name)
    return violations, ran, skipped, notes


def standards_for_drc(
    board: Any, board_file: str, params: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Compact standards result for ``run_drc`` (None when the project has no profile)."""
    profile, source = resolve_profile(board_file, {"projectPath": params.get("projectPath")})
    if profile is None:
        return None
    violations, ran, skipped, notes = evaluate(board, profile)
    opts = {
        k: params.get(k)
        for k in ("detail", "limit", "ignore", "nets", "refs", "region", "severity")
    }
    if opts.get("detail") == "full":
        opts["detail"] = "items"
    compact = check_summary.build_check_response("standards", board_file, violations, opts)
    return {
        "profile": source,
        "checksRun": ran,
        "skipped": skipped,
        "notes": notes,
        "counts": compact["counts"],
        "items": compact["items"],
        "itemsTruncated": compact["itemsTruncated"],
        "delta": {
            k: compact["delta"].get(k)
            for k in ("firstRun", "newCountAll", "resolvedCountAll", "new", "resolved")
        },
    }


class StandardsCommands:
    """check_standards."""

    def __init__(self, board: Optional[Any] = None):
        self.board = board

    def check_standards(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate the project's standards profile against the loaded board.

        Params: ``profilePath`` / ``profile`` (inline object) / ``projectPath``;
        ``checks`` (subset: fab, regions, netclasses, or a checks: key);
        ``detail``/``limit``/``ignore``/``nets``/``refs``/``region``/``severity``
        as for run_drc; ``record`` (default true) keeps history for the delta.
        """
        if not self.board:
            return {
                "success": False,
                "message": "No board is loaded",
                "errorDetails": "Load or create a board first",
            }
        try:
            board_file = self.board.GetFileName() or ""
        except Exception:
            board_file = ""
        opts = {
            key: params.get(key)
            for key in ("detail", "limit", "ignore", "nets", "refs", "region", "severity")
        }
        try:
            check_summary.parse_detail_params(opts)
            check_summary.parse_region(opts["region"])
            profile, source = resolve_profile(board_file, params)
        except (ValueError, ProfileError) as exc:
            return {"success": False, "message": f"Invalid standards profile or parameter: {exc}"}
        except OSError as exc:
            return {"success": False, "message": f"Cannot read the standards profile: {exc}"}
        if profile is None:
            where = os.path.dirname(board_file) if board_file else "the project directory"
            return {
                "success": False,
                "message": "No standards profile found",
                "errorDetails": (
                    f"Create standards.yaml (or standards.json) in {where}, or pass "
                    "profilePath / profile. See MCP_ISSUES §2.9 for the format."
                ),
            }
        only = params.get("checks")
        if isinstance(only, str):
            only = [only]
        violations, ran, skipped, notes = evaluate(self.board, profile, only)
        target = board_file or os.path.join(os.getcwd(), "unsaved.kicad_pcb")
        compact = check_summary.build_check_response(
            "standards", target, violations, opts, record=bool(params.get("record", True))
        )
        delta = compact["delta"]
        message = f"Found {len(violations)} standards violation(s)"
        if not delta.get("firstRun"):
            message += f" ({delta['newCountAll']} new, {delta['resolvedCountAll']} resolved)"
        return {
            "success": True,
            "message": message,
            "profile": source,
            "checksRun": ran,
            "skipped": skipped,
            "notes": notes,
            **compact,
        }
