"""Board editing primitives with dry-run, collision feedback and DRC deltas.

MCP_ISSUES §2.5 E1-E3 (and principles 1, 4, 5):

* ``add_copper``      -- batch tracks (polylines) and vias, validated first, all or nothing
* ``remove_copper``   -- remove tracks/arcs/vias by nets, region, lock state, layer, kind
                         or endpoint proximity
* ``unroute``         -- remove the routing of whole nets (``includeLocked`` for the
                         locked copper Freerouting imports)
* ``move_components`` -- move/rotate/flip several footprints and report courtyard
                         overlaps, board-edge and keepout problems and airwire lengths
* ``place_relative``  -- place a part relative to another part or one of its pads
* ``add_rule_area``   -- keepout/rule area forbidding tracks, vias, pads, pours, footprints
* ``edit_zone``       -- change a zone found by uuid, name or net+layer
* ``set_pad_zone_connection`` -- per-pad solid/thermal/none/inherit zone connection
* ``split_plane``     -- one layer, several nets, one call, priorities set

Every mutating call validates everything before touching the board, so a
failed call leaves the board unchanged; ``dryRun`` reports the would-be change
without mutating. The in-memory board is the only copy edited: the interface
saves it through its auto-save path (backups, external-change guard), so
memory and disk never drift. With ``drcDelta: true`` the board is saved
right away and ``run_drc`` reports what the edit added or resolved (a
baseline DRC runs first when the last recorded run is older than the board
file).

KiCad 10 SWIG traps handled here (MCP_ISSUES #5r): a fresh zone's
``GetLayerName()`` reports F.Cu whatever its layer, so layers are read from
``GetLayerSet()``; zone outlines are built with ``ZONE.AppendCorner`` because
``SetOutline`` stores a pointer to a Python-owned polygon that is then freed
and ``Outline().NewOutline()`` sometimes returns a bare ``SwigPyObject``;
``PAD.SetLocalThermalGapOverride`` takes ``std::optional<int>``, which SWIG
cannot build from a Python int; deleted items go through
``delete_board_item`` so Python never frees an object the board still owns.
"""

import logging
import math
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import pcbnew
from utils import check_summary
from utils import poly2d as g
from utils.board_items import delete_board_item
from utils.standards_profile import layer_name as canonical_layer, net_matches_any

logger = logging.getLogger("kicad_interface")

MM = 1_000_000

PAD_CONNECTIONS = {
    "solid": "ZONE_CONNECTION_FULL",
    "full": "ZONE_CONNECTION_FULL",
    "thermal": "ZONE_CONNECTION_THERMAL",
    "none": "ZONE_CONNECTION_NONE",
    "thermal_tht": "ZONE_CONNECTION_THT_THERMAL",
    "inherit": "ZONE_CONNECTION_INHERITED",
    "inherited": "ZONE_CONNECTION_INHERITED",
}
RULE_AREA_FORBIDS = {
    "tracks": "SetDoNotAllowTracks",
    "vias": "SetDoNotAllowVias",
    "pads": "SetDoNotAllowPads",
    "pours": "SetDoNotAllowZoneFills",
    "footprints": "SetDoNotAllowFootprints",
}
_FORBID_ALIASES = {"zones": "pours", "copper_pour": "pours", "fills": "pours", "track": "tracks"}
_KINDS = ("track", "arc", "via")


def _fail(message: str, details: Any = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "message": message}
    if details is not None:
        out["errorDetails"] = details
    out.update(extra)
    return out


def _nm(v: float) -> int:
    return int(round(float(v) * MM))


def _mm(v: int) -> float:
    return round(v / MM, 6)


def _uuid(item: Any) -> Optional[str]:
    try:
        u = item.m_Uuid.AsString()
        return u if isinstance(u, str) else None
    except Exception:
        return None


def _xy(v: Any) -> Tuple[float, float]:
    return v.x / MM, v.y / MM


class BoardEditCommands:
    """E1/E2/E3 board editing tools on the in-memory SWIG board."""

    def __init__(
        self,
        board: Optional[Any] = None,
        save_board: Optional[Callable[[], Dict[str, Any]]] = None,
        run_drc: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.board = board
        # Wired by the interface: its auto-save (backups + external-change guard)
        # and run_drc (project rules, history for deltas).
        self.save_board = save_board
        self.run_drc = run_drc

    # ================================================================ helpers

    def _no_board(self) -> Optional[Dict[str, Any]]:
        if not self.board:
            return _fail("No board is loaded", "Load or create a board first")
        return None

    def _layer_name(self, layer_id: int) -> str:
        try:
            name = self.board.GetStandardLayerName(layer_id)
            if isinstance(name, str) and name:
                return name
        except Exception:
            pass
        return self.board.GetLayerName(layer_id)

    def _copper_layer(self, name: Any) -> Tuple[Optional[int], Optional[str]]:
        """(layer id, error). Accepts 'F.Cu', 'In1.Cu', 'F', 'B', 'In1'."""
        if not isinstance(name, str) or not name:
            return None, f"layer must be a name such as 'F.Cu', got {name!r}"
        full = canonical_layer(name)
        lid = self.board.GetLayerID(full)
        if not isinstance(lid, int) or lid < 0:
            return None, f"layer '{name}' does not exist"
        if not pcbnew.IsCopperLayer(lid):
            return None, f"'{name}' is not a copper layer"
        try:
            if not self.board.GetEnabledLayers().Contains(lid):
                return None, f"copper layer '{full}' is not enabled on this board"
        except Exception:
            pass
        return lid, None

    def _copper_layers(self, names: Any) -> Tuple[List[int], Optional[str]]:
        if isinstance(names, str):
            names = [names]
        if not names:
            return [], "layers is required, e.g. ['F.Cu']"
        out: List[int] = []
        for name in names:
            if isinstance(name, str) and name in ("*.Cu", "all", "all_copper"):
                out += list(self.board.GetEnabledLayers().CuStack())
                continue
            lid, err = self._copper_layer(name)
            if err:
                return [], err
            out.append(lid)
        return sorted(set(out), key=out.index), None

    def _net(self, name: Any) -> Tuple[Any, Optional[str]]:
        """(NETINFO_ITEM or None for no net, error)."""
        if name is None or name == "":
            return None, None
        nets = self.board.GetNetInfo().NetsByName()
        if nets.has_key(str(name)):
            item = nets[str(name)]
            if not isinstance(item, pcbnew.NETINFO_ITEM):
                return (
                    None,
                    f"net '{name}' lookup returned {type(item).__name__} (broken SWIG proxy)",
                )
            return item, None
        from commands.routing import suggest_net_names

        names = [str(n) for n in nets.keys()] if hasattr(nets, "keys") else []
        hint = suggest_net_names(str(name), names)
        return None, f"net '{name}' does not exist" + (
            f"; did you mean {', '.join(repr(s) for s in hint)}?" if hint else ""
        )

    def _routing(self):
        from commands.routing import RoutingCommands

        return RoutingCommands(self.board)

    def _board_outline(self) -> List[Tuple[g.Polygon, List[g.Polygon]]]:
        poly = pcbnew.SHAPE_POLY_SET()
        try:
            ok = self.board.GetBoardPolygonOutlines(poly, False)
        except TypeError:
            ok = self.board.GetBoardPolygonOutlines(poly)
        if not ok:
            return []
        try:
            return g.swig_poly_points(poly)
        except TypeError:
            return []

    def _board_rect(self) -> Optional[g.Polygon]:
        box = self.board.GetBoardEdgesBoundingBox()
        if box.GetWidth() <= 0 or box.GetHeight() <= 0:
            return None
        return g.rect_polygon(
            box.GetLeft() / MM, box.GetTop() / MM, box.GetRight() / MM, box.GetBottom() / MM
        )

    # ------------------------------------------------------------ commit / DRC

    @staticmethod
    def _drc_params(params: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "detail": "summary",
            "limit": params.get("drcLimit", 10),
            "refillZones": params.get("refillZones", True),
        }
        if params.get("projectPath"):
            out["projectPath"] = params["projectPath"]
        return out

    def _drc_baseline(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Run DRC before the edit when the last recorded run predates the board file."""
        if not params.get("drcDelta") or params.get("dryRun") or not self.run_drc:
            return None
        try:
            board_file = self.board.GetFileName()
        except Exception:
            return None
        if not board_file or not os.path.isfile(board_file):
            return None
        last = check_summary.load_last_run("drc", board_file)
        if last and float(last.get("timestamp", 0)) >= os.path.getmtime(board_file):
            return {"baselineRun": False, "previousRunAt": check_summary._iso(last["timestamp"])}
        res = self.run_drc(self._drc_params(params))
        return {"baselineRun": True, "baselineOk": bool(res.get("success"))}

    def _commit(
        self,
        result: Dict[str, Any],
        params: Dict[str, Any],
        changed: bool,
        baseline: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Hand the change to the interface's auto-save; optionally run DRC for a delta.

        ``_skipAutoSave`` / ``_autoSaveStatus`` are private keys the interface
        pops: nothing to save for a dry run or a no-op, or "already saved".
        """
        if result.get("dryRun") or not changed:
            result["_skipAutoSave"] = True
            return result
        if not params.get("drcDelta"):
            return result
        status = self.save_board() if self.save_board else None
        if status is not None:
            result["_autoSaveStatus"] = status
            if not status.get("saved"):
                result["drc"] = {
                    "skipped": "the board was not saved, so DRC would check stale copper: "
                    + str(status.get("warning") or status.get("error") or status.get("skipped"))
                }
                return result
        if not self.run_drc:
            result["drc"] = {"skipped": "run_drc is not available"}
            return result
        drc = self.run_drc(self._drc_params(params))
        result["drc"] = self._compact_drc(drc, baseline)
        return result

    @staticmethod
    def _compact_drc(drc: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not drc.get("success"):
            return {"error": drc.get("message"), "details": drc.get("errorDetails")}
        delta = drc.get("delta") or {}
        out = {
            "total": (drc.get("summary") or {}).get("total"),
            "bySeverity": (drc.get("counts") or {}).get("by_severity"),
            "newCount": delta.get("newCountAll", 0),
            "resolvedCount": delta.get("resolvedCountAll", 0),
            "new": delta.get("new", []),
            "resolved": delta.get("resolved", []),
            "firstRun": bool(delta.get("firstRun")),
        }
        if baseline:
            out["baseline"] = baseline
        std = drc.get("standards")
        if std:
            sd = std.get("delta") or {}
            out["standards"] = {
                "total": (std.get("counts") or {}).get("total"),
                "newCount": sd.get("newCountAll"),
                "resolvedCount": sd.get("resolvedCountAll"),
                "new": sd.get("new", []),
            }
        return out

    # ------------------------------------------------------------ copper readers

    def _track_info(self, t: Any) -> Dict[str, Any]:
        cls = t.GetClass()
        info: Dict[str, Any] = {
            "uuid": _uuid(t),
            "kind": {"PCB_VIA": "via", "PCB_ARC": "arc"}.get(cls, "track"),
            "net": t.GetNetname(),
            "locked": bool(t.IsLocked()),
        }
        if cls == "PCB_VIA":
            info["at"] = list(map(lambda v: round(v, 4), _xy(t.GetPosition())))
            try:
                info["layers"] = [
                    self._layer_name(t.TopLayer()),
                    self._layer_name(t.BottomLayer()),
                ]
            except Exception:
                pass
        else:
            info["layer"] = self._layer_name(t.GetLayer())
            info["start"] = [round(v, 4) for v in _xy(t.GetStart())]
            info["end"] = [round(v, 4) for v in _xy(t.GetEnd())]
            info["width"] = _mm(t.GetWidth())
        return info

    # ================================================================ E1: add_copper

    def _parse_copper_item(self, raw: Any, locked_default: bool) -> Tuple[Optional[Dict], str]:
        """Normalise one item to {kind, ...}; returns (spec, error)."""
        if isinstance(raw, str):
            return self._parse_spec_string(raw, locked_default)
        if not isinstance(raw, dict):
            return None, "item must be an object such as {track: {...}} or {via: {...}}"
        if "track" in raw or "T" in raw:
            body, kind = raw.get("track", raw.get("T")), "track"
        elif "via" in raw or "V" in raw:
            body, kind = raw.get("via", raw.get("V")), "via"
        elif raw.get("type") in ("track", "via"):
            body, kind = raw, raw["type"]
        else:
            return None, "item needs a 'track' or 'via' key"
        if not isinstance(body, dict):
            return None, f"{kind} must be an object"
        locked = bool(body.get("locked", raw.get("locked", locked_default)))
        net, err = self._net(body.get("net"))
        if err:
            return None, err
        ds = self.board.GetDesignSettings()
        if kind == "track":
            lid, err = self._copper_layer(body.get("layer", "F.Cu"))
            if err:
                return None, err
            try:
                width = float(body["width"]) if body.get("width") is not None else None
            except (TypeError, ValueError):
                return None, f"width must be a number in mm, got {body.get('width')!r}"
            width_nm = _nm(width) if width is not None else ds.GetCurrentTrackWidth()
            if width_nm <= 0:
                return None, "width must be > 0"
            pts_raw = body.get("points")
            if not isinstance(pts_raw, (list, tuple)) or len(pts_raw) < 2:
                return None, "track needs points: [[x, y], [x, y], ...] (at least 2, mm)"
            try:
                pts = [g.parse_point(p) for p in pts_raw]
            except ValueError as exc:
                return None, str(exc)
            segs = [(a, b) for a, b in zip(pts, pts[1:]) if a != b]
            if not segs:
                return None, "track points are all identical (zero length)"
            return {
                "kind": "track",
                "net": net,
                "netName": body.get("net") or "",
                "layer": lid,
                "width": width_nm,
                "segments": segs,
                "locked": locked,
            }, ""
        # via
        try:
            at = g.parse_point(body.get("at", body.get("position")))
        except ValueError as exc:
            return None, str(exc)
        try:
            size = _nm(body["size"]) if body.get("size") is not None else ds.GetCurrentViaSize()
            drill = _nm(body["drill"]) if body.get("drill") is not None else ds.GetCurrentViaDrill()
        except (TypeError, ValueError):
            return None, "size and drill must be numbers in mm"
        if drill <= 0 or size <= drill:
            return None, f"via size ({_mm(size)} mm) must exceed its drill ({_mm(drill)} mm)"
        stack = list(self.board.GetEnabledLayers().CuStack())
        layers = body.get("layers")
        if layers:
            ids, err = self._copper_layers(layers)
            if err:
                return None, err
            if len(ids) != 2:
                return None, "via layers must name two copper layers, e.g. ['F.Cu', 'In2.Cu']"
            top, bot = sorted(ids, key=stack.index)
        else:
            top, bot = stack[0], stack[-1]
        return {
            "kind": "via",
            "net": net,
            "netName": body.get("net") or "",
            "at": at,
            "size": size,
            "drill": drill,
            "top": top,
            "bottom": bot,
            "locked": locked,
        }, ""

    def _parse_spec_string(self, text: str, locked_default: bool) -> Tuple[Optional[Dict], str]:
        """``T net layer width x1 y1 x2 y2 ...`` / ``V net x y [size drill]`` (add_items.py)."""
        p = text.split("#")[0].split()
        if not p:
            return None, "empty spec string"
        try:
            if p[0].upper() == "T" and len(p) >= 8 and (len(p) - 4) % 2 == 0:
                coords = [float(v) for v in p[4:]]
                return self._parse_copper_item(
                    {
                        "track": {
                            "net": p[1],
                            "layer": p[2],
                            "width": float(p[3]),
                            "points": [coords[i : i + 2] for i in range(0, len(coords), 2)],
                        }
                    },
                    locked_default,
                )
            if p[0].upper() == "V" and len(p) in (4, 6):
                via: Dict[str, Any] = {"net": p[1], "at": [float(p[2]), float(p[3])]}
                if len(p) == 6:
                    via.update(size=float(p[4]), drill=float(p[5]))
                return self._parse_copper_item({"via": via}, locked_default)
        except ValueError:
            return None, f"cannot read numbers in spec {text!r}"
        return None, (
            f"bad spec {text!r}: use 'T net layer width x1 y1 x2 y2 [...]' or "
            "'V net x y [size drill]' (mm)"
        )

    def add_copper(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add tracks (polylines) and vias in one all-or-nothing batch.

        ``items``: ``[{track: {net, layer, width, points: [[x, y], ...]}} |
        {via: {net, at: [x, y], size, drill, layers?}} | "T net layer width x1 y1 ..." |
        "V net x y [size drill]"]`` (mm). ``locked`` (default false) applies to
        items that don't set their own. ``dryRun`` validates only;
        ``drcDelta`` saves and reports new/resolved DRC violations.
        """
        err = self._no_board()
        if err:
            return err
        items = params.get("items")
        if not isinstance(items, list) or not items:
            return _fail("items must be a non-empty list of {track: ...} / {via: ...}")
        locked = bool(params.get("locked", False))
        specs, errors = [], []
        for i, raw in enumerate(items):
            spec, msg = self._parse_copper_item(raw, locked)
            if spec is None:
                errors.append({"index": i, "error": msg})
            else:
                specs.append(spec)
        if errors:
            return _fail(
                f"{len(errors)} of {len(items)} item(s) are invalid; nothing was added",
                errors,
                errors=errors,
            )

        def describe(spec: Dict[str, Any]) -> Dict[str, Any]:
            if spec["kind"] == "track":
                return {
                    "kind": "track",
                    "net": spec["netName"],
                    "layer": self._layer_name(spec["layer"]),
                    "width": _mm(spec["width"]),
                    "segments": len(spec["segments"]),
                    "lengthMm": round(
                        sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in spec["segments"]), 4
                    ),
                    "locked": spec["locked"],
                }
            return {
                "kind": "via",
                "net": spec["netName"],
                "at": list(spec["at"]),
                "size": _mm(spec["size"]),
                "drill": _mm(spec["drill"]),
                "layers": [self._layer_name(spec["top"]), self._layer_name(spec["bottom"])],
                "locked": spec["locked"],
            }

        summary = [describe(s) for s in specs]
        n_seg = sum(len(s["segments"]) for s in specs if s["kind"] == "track")
        n_via = sum(1 for s in specs if s["kind"] == "via")
        if params.get("dryRun"):
            return {
                "success": True,
                "dryRun": True,
                "_skipAutoSave": True,
                "message": f"Would add {n_seg} track segment(s) and {n_via} via(s)",
                "items": summary,
            }

        baseline = self._drc_baseline(params)
        created: List[Any] = []
        try:
            for spec in specs:
                if spec["kind"] == "track":
                    for a, b in spec["segments"]:
                        t = pcbnew.PCB_TRACK(self.board)
                        if not isinstance(t, pcbnew.PCB_TRACK):
                            raise TypeError(f"PCB_TRACK() returned {type(t).__name__}")
                        t.SetStart(pcbnew.VECTOR2I(_nm(a[0]), _nm(a[1])))
                        t.SetEnd(pcbnew.VECTOR2I(_nm(b[0]), _nm(b[1])))
                        t.SetWidth(spec["width"])
                        t.SetLayer(spec["layer"])
                        if spec["net"] is not None:
                            t.SetNet(spec["net"])
                        t.SetLocked(spec["locked"])
                        self.board.Add(t)
                        created.append(t)
                else:
                    v = pcbnew.PCB_VIA(self.board)
                    if not isinstance(v, pcbnew.PCB_VIA):
                        raise TypeError(f"PCB_VIA() returned {type(v).__name__}")
                    v.SetPosition(pcbnew.VECTOR2I(_nm(spec["at"][0]), _nm(spec["at"][1])))
                    stack = list(self.board.GetEnabledLayers().CuStack())
                    through = spec["top"] == stack[0] and spec["bottom"] == stack[-1]
                    if not through:
                        outer = spec["top"] == stack[0] or spec["bottom"] == stack[-1]
                        v.SetViaType(pcbnew.VIATYPE_BLIND if outer else pcbnew.VIATYPE_BURIED)
                    v.SetLayerPair(spec["top"], spec["bottom"])
                    v.SetWidth(spec["size"])
                    v.SetDrill(spec["drill"])
                    if spec["net"] is not None:
                        v.SetNet(spec["net"])
                    v.SetLocked(spec["locked"])
                    self.board.Add(v)
                    created.append(v)
        except Exception as exc:
            for obj in created:
                try:
                    delete_board_item(self.board, obj)
                except Exception:
                    logger.warning("rollback of an added item failed", exc_info=True)
            logger.error(f"add_copper failed: {exc}")
            return _fail("Failed to add copper; the board is unchanged", str(exc))

        uuids = [_uuid(o) for o in created]
        result = {
            "success": True,
            "message": f"Added {n_seg} track segment(s) and {n_via} via(s)",
            "added": {"trackSegments": n_seg, "vias": n_via},
            "items": summary,
            "uuids": uuids,
        }
        return self._commit(result, params, changed=True, baseline=baseline)

    # ================================================================ E1: remove / unroute

    def _select_copper(
        self, flt: Dict[str, Any]
    ) -> Tuple[List[Any], Dict[str, int], Optional[str]]:
        """(items to remove, skipped counts, error) for a remove_copper filter."""
        nets = flt.get("nets")
        if isinstance(nets, str):
            nets = [nets]
        region = flt.get("region")
        near = flt.get("endpointNear", flt.get("endpoint_near"))
        if not nets and region is None and near is None:
            return [], {}, "filter needs at least one of nets, region or endpointNear"
        poly = None
        if region is not None:
            try:
                poly = g.parse_polygon(region)
            except ValueError as exc:
                return [], {}, f"region: {exc}"
        mode = str(flt.get("regionMode", "inside")).lower()
        if mode not in ("inside", "touch"):
            return [], {}, "regionMode must be 'inside' or 'touch'"
        near_pt, near_r = None, 0.0
        if near is not None:
            if not isinstance(near, dict):
                return [], {}, "endpointNear must be {at: [x, y], radius} (mm)"
            try:
                near_pt = g.parse_point(near.get("at"))
                near_r = float(near.get("radius", 0.05))
            except (ValueError, TypeError) as exc:
                return [], {}, f"endpointNear: {exc}"
        kinds = flt.get("kinds") or list(_KINDS)
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = [k.rstrip("s") if isinstance(k, str) else k for k in kinds]
        bad = [k for k in kinds if k not in _KINDS]
        if bad:
            return [], {}, f"kinds must be among {list(_KINDS)}, got {bad}"
        locked = flt.get("locked", "unlocked")
        if locked is True:
            locked = "locked"
        elif locked is False:
            locked = "unlocked"
        if locked not in ("unlocked", "locked", "any"):
            return [], {}, "locked must be 'unlocked' (default), 'locked', 'any', true or false"
        layer_ids = None
        if flt.get("layer") or flt.get("layers"):
            layer_ids, err = self._copper_layers(flt.get("layers") or flt.get("layer"))
            if err:
                return [], {}, err
        if nets:
            known = {str(n) for n in self.board.GetNetInfo().NetsByName().keys()}
            unknown = [n for n in nets if not any(net_matches_any(k, [n]) for k in known)]
            if unknown:
                return [], {}, f"no net matches {unknown}"

        chosen, skipped = [], {"locked": 0}
        stack = list(self.board.GetEnabledLayers().CuStack())
        for t in self.board.GetTracks():
            cls = t.GetClass()
            kind = {"PCB_VIA": "via", "PCB_ARC": "arc"}.get(cls, "track")
            if kind not in kinds:
                continue
            if nets and not net_matches_any(t.GetNetname(), nets):
                continue
            if layer_ids is not None:
                if kind == "via":
                    try:
                        span = stack[stack.index(t.TopLayer()) : stack.index(t.BottomLayer()) + 1]
                    except ValueError:
                        span = stack
                    if not set(span) & set(layer_ids):
                        continue
                elif t.GetLayer() not in layer_ids:
                    continue
            pts = [_xy(t.GetPosition())] if kind == "via" else [_xy(t.GetStart()), _xy(t.GetEnd())]
            if poly is not None:
                if kind == "via":
                    inside = g.point_in_polygon(pts[0], poly)
                elif mode == "inside":
                    inside = all(g.point_in_polygon(p, poly) for p in pts)
                else:
                    inside = g.segment_touches_polygon(pts[0], pts[1], poly)
                if not inside:
                    continue
            if near_pt is not None and not any(
                math.hypot(p[0] - near_pt[0], p[1] - near_pt[1]) <= near_r for p in pts
            ):
                continue
            is_locked = bool(t.IsLocked())
            if (locked == "unlocked" and is_locked) or (locked == "locked" and not is_locked):
                if is_locked:
                    skipped["locked"] += 1
                continue
            chosen.append(t)
        return chosen, skipped, None

    def _remove(
        self, items: List[Any], params: Dict[str, Any], label: str, extra: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            limit = max(0, int(params.get("limit", 20)))
        except (TypeError, ValueError):
            limit = 20
        infos = [self._track_info(t) for t in items]
        by_kind: Dict[str, int] = {}
        by_net: Dict[str, int] = {}
        for i in infos:
            by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
            by_net[i["net"]] = by_net.get(i["net"], 0) + 1
        dry = bool(params.get("dryRun"))
        result: Dict[str, Any] = {
            "success": True,
            "dryRun": dry,
            "message": f"{'Would remove' if dry else 'Removed'} {len(items)} item(s){label}",
            "removed": {"count": len(items), "byKind": by_kind, "byNet": by_net},
            "items": infos[:limit],
            "itemsTruncated": len(infos) > limit,
            **extra,
        }
        if dry or not items:
            return self._commit(result, params, changed=False)
        baseline = self._drc_baseline(params)
        for t in items:
            delete_board_item(self.board, t)
        items.clear()
        try:
            self.board.SetModified()
        except Exception:
            pass
        return self._commit(result, params, changed=True, baseline=baseline)

    def remove_copper(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remove tracks/arcs/vias matching ``filter``.

        ``filter``: ``nets`` (names or globs), ``region`` ({x1,y1,x2,y2} or a
        polygon, mm) with ``regionMode`` inside (default: wholly inside) or
        touch, ``endpointNear`` ({at: [x, y], radius}), ``layer``/``layers``,
        ``kinds`` (track/arc/via), ``locked`` (unlocked (default) / locked /
        any). At least one of nets, region or endpointNear is required.
        """
        err = self._no_board()
        if err:
            return err
        flt = params.get("filter")
        if flt is None:
            flt = {
                k: params[k]
                for k in (
                    "nets",
                    "region",
                    "regionMode",
                    "endpointNear",
                    "kinds",
                    "locked",
                    "layer",
                    "layers",
                )
                if k in params
            }
        if not isinstance(flt, dict):
            return _fail("filter must be an object")
        items, skipped, error = self._select_copper(flt)
        if error:
            return _fail(f"Invalid filter: {error}")
        extra: Dict[str, Any] = {}
        if skipped.get("locked"):
            extra["skippedLocked"] = skipped["locked"]
            extra["hint"] = "locked items were kept; set filter.locked to 'any' to include them"
        return self._remove(items, params, "", extra)

    def unroute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remove all tracks and vias of ``nets`` (globs allowed).

        ``includeLocked`` (default false) also removes locked copper --
        Freerouting's SES import comes back locked. ``keepVias`` keeps vias.
        """
        err = self._no_board()
        if err:
            return err
        nets = params.get("nets") or ([params["net"]] if params.get("net") else None)
        if isinstance(nets, str):
            nets = [nets]
        if not nets:
            return _fail("nets is required (names or globs, e.g. ['/Audio/*'])")
        flt = {
            "nets": nets,
            "locked": (
                "any" if params.get("includeLocked", params.get("include_locked")) else "unlocked"
            ),
            "kinds": ["track", "arc"] if params.get("keepVias") else list(_KINDS),
        }
        items, skipped, error = self._select_copper(flt)
        if error:
            return _fail(error)
        extra: Dict[str, Any] = {"nets": nets}
        if skipped.get("locked"):
            extra["skippedLocked"] = skipped["locked"]
            extra["hint"] = (
                f"{skipped['locked']} locked item(s) kept; pass includeLocked=true to remove them "
                "(Freerouting imports its routing locked)"
            )
        return self._remove(items, params, f" from {len(nets)} net pattern(s)", extra)

    # ================================================================ E2: placement

    def _courtyard(self, fp: Any) -> Tuple[List[g.Polygon], str]:
        """Courtyard polygons in mm, or the footprint bbox when it has none."""
        layer = pcbnew.B_CrtYd if fp.GetLayer() == pcbnew.B_Cu else pcbnew.F_CrtYd
        try:
            polys = [o for o, _ in g.swig_poly_points(fp.GetCourtyard(layer))]
            if polys:
                return polys, "courtyard"
        except Exception:
            pass
        try:
            bb = fp.GetBoundingBox(False)
        except TypeError:
            bb = fp.GetBoundingBox()
        return [
            g.rect_polygon(
                bb.GetLeft() / MM, bb.GetTop() / MM, bb.GetRight() / MM, bb.GetBottom() / MM
            )
        ], "bbox"

    def _rule_areas(self) -> List[Dict[str, Any]]:
        out = []
        for z in self.board.Zones():
            if not z.GetIsRuleArea():
                continue
            if not (z.GetDoNotAllowFootprints() or z.GetDoNotAllowPads()):
                continue
            out.append(
                {
                    "uuid": _uuid(z),
                    "name": z.GetZoneName(),
                    "layers": set(z.GetLayerSet().Seq()),
                    "footprints": bool(z.GetDoNotAllowFootprints()),
                    "pads": bool(z.GetDoNotAllowPads()),
                    "polygons": [o for o, _ in g.zone_polygons(z)],
                }
            )
        return out

    def _plane_nets(self) -> set:
        return {
            z.GetNetname() for z in self.board.Zones() if not z.GetIsRuleArea() and z.GetNetname()
        }

    def _airwires(self, fp: Any, skip_nets: set, max_pads: int, pads_by_net) -> Dict[str, Any]:
        """Sum over the part's pads of the distance to the nearest same-net pad elsewhere."""
        total = 0.0
        nets = {}
        ref = fp.GetReference()
        for pad in fp.Pads():
            net = pad.GetNetname()
            if not net or net in skip_nets or net.startswith("unconnected-"):
                continue
            others = [p for p in pads_by_net.get(net, []) if p[0] != ref]
            if not others or len(others) > max_pads:
                continue
            x, y = _xy(pad.GetPosition())
            d = min(math.hypot(px - x, py - y) for _, px, py in others)
            total += d
            nets[net] = round(nets.get(net, 0.0) + d, 3)
        return {"totalMm": round(total, 3), "byNet": nets}

    def _pads_by_net(self) -> Dict[str, List[Tuple[str, float, float]]]:
        out: Dict[str, List[Tuple[str, float, float]]] = {}
        for fp in self.board.GetFootprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                net = pad.GetNetname()
                if net:
                    x, y = _xy(pad.GetPosition())
                    out.setdefault(net, []).append((ref, x, y))
        return out

    def _collisions(self, refs: List[str], margin: float) -> Dict[str, Dict[str, Any]]:
        """Per ref: overlapping footprints, outside-board, keepout hits."""
        fps = {fp.GetReference(): fp for fp in self.board.GetFootprints()}
        shapes = {}
        for ref, fp in fps.items():
            polys, basis = self._courtyard(fp)
            shapes[ref] = (polys, basis, g.bbox([p for poly in polys for p in poly]), fp.GetLayer())
        outline = self._board_outline()
        areas = self._rule_areas()
        out: Dict[str, Dict[str, Any]] = {}
        for ref in refs:
            polys, basis, box, side = shapes[ref]
            overlaps = []
            for other, (opolys, obasis, obox, oside) in shapes.items():
                if other == ref or oside != side or not g.bbox_overlap(box, obox, margin):
                    continue
                if any(
                    (
                        g.polygons_overlap_strictly(a, b)
                        if margin <= 0
                        else g.polygons_intersect(a, b, margin)
                    )
                    for a in polys
                    for b in opolys
                ):
                    overlaps.append(other)
            entry: Dict[str, Any] = {"overlaps": sorted(overlaps), "basis": basis}
            if outline:
                inside = any(
                    all(g.polygon_inside(p, o) for p in polys)
                    and not any(g.polygons_overlap_strictly(p, h) for p in polys for h in holes)
                    for o, holes in outline
                )
                entry["outsideBoard"] = not inside
            keepouts = []
            fp = fps[ref]
            for area in areas:
                if area["footprints"] and side in area["layers"]:
                    if any(
                        g.polygons_overlap_strictly(a, b) for a in polys for b in area["polygons"]
                    ):
                        keepouts.append(
                            {"uuid": area["uuid"], "name": area["name"], "rule": "footprints"}
                        )
                        continue
                if area["pads"]:
                    for pad in fp.Pads():
                        if not set(pad.GetLayerSet().CuStack()) & area["layers"]:
                            continue
                        if any(
                            g.point_in_polygon(_xy(pad.GetPosition()), poly)
                            for poly in area["polygons"]
                        ):
                            keepouts.append(
                                {
                                    "uuid": area["uuid"],
                                    "name": area["name"],
                                    "rule": "pads",
                                    "pad": pad.GetNumber(),
                                }
                            )
                            break
            entry["keepouts"] = keepouts
            out[ref] = entry
        return out

    def _parse_moves(self, moves: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Normalise list/dict moves; returns (moves, errors)."""
        if isinstance(moves, dict):
            moves = [
                dict(spec, ref=ref) if isinstance(spec, dict) else {"ref": ref, "bad": spec}
                for ref, spec in moves.items()
            ]
        if not isinstance(moves, list) or not moves:
            return [], [
                {"error": "moves must be a non-empty list of {ref, x, y, rotation?, side?}"}
            ]
        out, errors = [], []
        seen = set()
        for i, m in enumerate(moves):
            if not isinstance(m, dict):
                errors.append({"index": i, "error": "move must be an object"})
                continue
            ref = m.get("ref") or m.get("reference")
            fp = self.board.FindFootprintByReference(ref) if ref else None
            if not ref or fp is None:
                errors.append({"index": i, "ref": ref, "error": f"footprint '{ref}' not found"})
                continue
            if not isinstance(fp, pcbnew.FOOTPRINT):
                errors.append(
                    {
                        "index": i,
                        "ref": ref,
                        "error": f"lookup returned {type(fp).__name__} (broken SWIG proxy)",
                    }
                )
                continue
            if ref in seen:
                errors.append({"index": i, "ref": ref, "error": "footprint listed twice"})
                continue
            seen.add(ref)
            pos = m.get("position") if isinstance(m.get("position"), (dict, list)) else None
            try:
                if pos is not None:
                    x, y = g.parse_point(pos)
                elif "x" in m and "y" in m:
                    x, y = float(m["x"]), float(m["y"])
                elif "dx" in m or "dy" in m:
                    cx, cy = _xy(fp.GetPosition())
                    x, y = cx + float(m.get("dx", 0)), cy + float(m.get("dy", 0))
                else:
                    x, y = _xy(fp.GetPosition())
                rot = m.get("rotation", m.get("rot"))
                rot = float(rot) if rot is not None else None
            except (TypeError, ValueError) as exc:
                errors.append({"index": i, "ref": ref, "error": f"bad coordinates: {exc}"})
                continue
            side = m.get("side", m.get("layer"))
            if side is not None:
                s = str(side).lower()
                side = {
                    "front": "F",
                    "top": "F",
                    "f": "F",
                    "f.cu": "F",
                    "back": "B",
                    "bottom": "B",
                    "b": "B",
                    "b.cu": "B",
                }.get(s)
                if side is None:
                    errors.append(
                        {"index": i, "ref": ref, "error": "side must be front/back (F.Cu/B.Cu)"}
                    )
                    continue
            out.append({"ref": ref, "fp": fp, "x": x, "y": y, "rotation": rot, "side": side})
        return out, errors

    @staticmethod
    def _flip(fp: Any) -> None:
        direction = getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", None)
        if direction is not None:
            fp.Flip(fp.GetPosition(), direction)
        else:  # pragma: no cover - KiCad < 9
            fp.Flip(fp.GetPosition(), False)

    def _apply_moves(self, moves: List[Dict[str, Any]]) -> List[Tuple[Any, Any, float, int]]:
        old = []
        for m in moves:
            fp = m["fp"]
            old.append((fp, fp.GetPosition(), fp.GetOrientationDegrees(), fp.GetLayer()))
            if m["side"]:
                want = pcbnew.F_Cu if m["side"] == "F" else pcbnew.B_Cu
                if fp.GetLayer() != want:
                    self._flip(fp)
            fp.SetPosition(pcbnew.VECTOR2I(_nm(m["x"]), _nm(m["y"])))
            if m["rotation"] is not None:
                fp.SetOrientationDegrees(m["rotation"])
        return old

    def _revert_moves(self, old: List[Tuple[Any, Any, float, int]]) -> None:
        for fp, pos, rot, layer in reversed(old):
            try:
                if fp.GetLayer() != layer:
                    self._flip(fp)
                fp.SetPosition(pos)
                fp.SetOrientationDegrees(rot)
            except Exception:
                logger.warning("failed to restore a footprint position", exc_info=True)

    def move_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Move/rotate/flip footprints with collision feedback.

        ``moves``: ``[{ref, x, y, rotation?, side?}]`` (absolute mm/deg), or
        ``{dx, dy}`` relative, or the ``{REF: {...}}`` dict form. Returns per
        part: new position, ``newOverlaps``/``overlaps`` (courtyard polygons,
        same side), ``outsideBoard``, ``keepouts`` (rule areas forbidding
        footprints or pads) and airwire length before/after. ``rejectOnCollision``
        undoes the whole batch when any part gains a collision. ``dryRun``
        measures at the proposed spots and puts everything back.
        """
        err = self._no_board()
        if err:
            return err
        moves, errors = self._parse_moves(params.get("moves") or params.get("placements"))
        if errors:
            return _fail(f"{len(errors)} invalid move(s); nothing was moved", errors, errors=errors)
        return self._do_moves(moves, params)

    def _do_moves(
        self,
        moves: List[Dict[str, Any]],
        params: Dict[str, Any],
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        check = bool(params.get("checkCollisions", True))
        try:
            margin = float(params.get("margin", 0.0))
        except (TypeError, ValueError):
            return _fail("margin must be a number (mm)")
        refs = [m["ref"] for m in moves]
        skip_nets = self._plane_nets() if params.get("skipPlaneNets", True) else set()
        max_pads = int(params.get("maxPadsPerNet", 64))

        before_pads = self._pads_by_net()
        air_before = {
            m["ref"]: self._airwires(m["fp"], skip_nets, max_pads, before_pads) for m in moves
        }
        coll_before = self._collisions(refs, margin) if check else {}

        old = self._apply_moves(moves)
        try:
            after_pads = self._pads_by_net()
            air_after = {
                m["ref"]: self._airwires(m["fp"], skip_nets, max_pads, after_pads) for m in moves
            }
            coll_after = self._collisions(refs, margin) if check else {}
        except Exception as exc:
            self._revert_moves(old)
            return _fail("Collision check failed; nothing was moved", str(exc))

        parts = []
        problems = 0
        for m in moves:
            fp = m["fp"]
            entry: Dict[str, Any] = {
                "ref": m["ref"],
                "position": [round(v, 4) for v in _xy(fp.GetPosition())],
                "rotation": round(fp.GetOrientationDegrees(), 3),
                "side": "back" if fp.GetLayer() == pcbnew.B_Cu else "front",
                "airwireMm": {
                    "before": air_before[m["ref"]]["totalMm"],
                    "after": air_after[m["ref"]]["totalMm"],
                },
            }
            if check:
                a, b = coll_after[m["ref"]], coll_before[m["ref"]]
                new_ov = sorted(set(a["overlaps"]) - set(b["overlaps"]))
                entry.update(
                    overlaps=a["overlaps"],
                    newOverlaps=new_ov,
                    resolvedOverlaps=sorted(set(b["overlaps"]) - set(a["overlaps"])),
                    collisionBasis=a["basis"],
                    keepouts=a["keepouts"],
                )
                if "outsideBoard" in a:
                    entry["outsideBoard"] = a["outsideBoard"]
                new_problem = (
                    bool(new_ov)
                    or (a.get("outsideBoard") and not b.get("outsideBoard"))
                    or (len(a["keepouts"]) > len(b["keepouts"]))
                )
                entry["ok"] = not (a["overlaps"] or a.get("outsideBoard") or a["keepouts"])
                problems += 1 if new_problem else 0
            parts.append(entry)

        dry = bool(params.get("dryRun"))
        reject = bool(params.get("rejectOnCollision", False)) and problems > 0
        if dry or reject:
            self._revert_moves(old)
        result: Dict[str, Any] = {
            "success": not reject,
            "dryRun": dry,
            "message": (
                f"{'Would move' if dry else ('Rejected moving' if reject else 'Moved')} "
                f"{len(moves)} footprint(s); {problems} gained a collision"
            ),
            "parts": parts,
            "collisionsAdded": problems,
        }
        if extra:
            result.update(extra)
        if reject:
            result["errorDetails"] = "rejectOnCollision: the batch was undone"
            return result
        if dry:
            return self._commit(result, params, changed=False)
        # DRC baseline is measured on disk, which still holds the pre-move board.
        baseline = self._drc_baseline(params)
        return self._commit(result, params, changed=True, baseline=baseline)

    def place_relative(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place ``ref`` relative to ``anchorRef`` (or one of its pads).

        Target = anchor origin (or ``anchorPad`` centre) + ``offset`` [dx, dy]
        mm. With ``alignToAnchor`` the offset is in the anchor's rotated frame
        and ``rotation`` is relative to the anchor's orientation; otherwise both
        are in board coordinates. ``pad`` puts that pad of ``ref`` (instead of
        its origin) on the target -- e.g. a decoupling cap's pad 1 next to an
        IC supply pin. Collision feedback and dryRun as in move_components.
        """
        err = self._no_board()
        if err:
            return err
        ref = params.get("ref") or params.get("reference")
        anchor_ref = params.get("anchorRef") or params.get("anchor")
        if not ref or not anchor_ref:
            return _fail("ref and anchorRef are required")
        fp = self.board.FindFootprintByReference(ref)
        anchor = self.board.FindFootprintByReference(anchor_ref)
        for name, obj in ((ref, fp), (anchor_ref, anchor)):
            if obj is None:
                return _fail(f"footprint '{name}' not found")
            if not isinstance(obj, pcbnew.FOOTPRINT):
                return _fail(
                    f"footprint lookup for '{name}' returned {type(obj).__name__} (broken SWIG proxy)"
                )
        try:
            dx, dy = g.parse_point(params.get("offset", [0, 0]))
        except ValueError as exc:
            return _fail(f"offset: {exc}")
        align = bool(params.get("alignToAnchor", False))
        anchor_rot = anchor.GetOrientationDegrees()

        def find_pad(owner: Any, number: Any) -> Any:
            for p in owner.Pads():
                if str(p.GetNumber()) == str(number):
                    return p
            return None

        anchor_pad = params.get("anchorPad")
        if anchor_pad is not None:
            p = find_pad(anchor, anchor_pad)
            if p is None:
                return _fail(f"{anchor_ref} has no pad '{anchor_pad}'")
            base = _xy(p.GetPosition())
        else:
            base = _xy(anchor.GetPosition())
        if align:
            dx, dy = g.rotate((dx, dy), anchor_rot)
        target = (base[0] + dx, base[1] + dy)

        rotation = params.get("rotation")
        try:
            rotation = float(rotation) if rotation is not None else None
        except (TypeError, ValueError):
            return _fail("rotation must be a number (degrees)")
        if align and rotation is not None:
            rotation = (anchor_rot + rotation) % 360

        own_pad = params.get("pad")
        origin = target
        if own_pad is not None:
            p = find_pad(fp, own_pad)
            if p is None:
                return _fail(f"{ref} has no pad '{own_pad}'")
            # Pad offset from the origin at the final orientation.
            cur_rot = fp.GetOrientationDegrees()
            rel = (p.GetPosition().x - fp.GetPosition().x) / MM, (
                p.GetPosition().y - fp.GetPosition().y
            ) / MM
            final_rot = rotation if rotation is not None else cur_rot
            local = g.rotate(rel, -cur_rot)
            rel_final = g.rotate(local, final_rot)
            origin = (target[0] - rel_final[0], target[1] - rel_final[1])

        move = {
            "ref": ref,
            "fp": fp,
            "x": origin[0],
            "y": origin[1],
            "rotation": rotation,
            "side": None,
        }
        side = params.get("side")
        if side is not None:
            parsed, errors = self._parse_moves(
                [{"ref": ref, "side": side, "x": origin[0], "y": origin[1]}]
            )
            if errors:
                return _fail(errors[0]["error"])
            move["side"] = parsed[0]["side"]
            if own_pad is not None and move["side"] != (
                "B" if fp.GetLayer() == pcbnew.B_Cu else "F"
            ):
                return _fail(
                    "pad alignment with a side change is not supported; flip first, then align"
                )
        return self._do_moves(
            [move],
            params,
            extra={
                "target": [round(target[0], 4), round(target[1], 4)],
                "anchor": {
                    "ref": anchor_ref,
                    "pad": anchor_pad,
                    "at": [round(base[0], 4), round(base[1], 4)],
                },
            },
        )

    # ================================================================ E3: zones

    def _zone_summary(self, z: Any) -> Dict[str, Any]:
        try:
            layers = [self._layer_name(lid) for lid in z.GetLayerSet().Seq()]
        except Exception:
            layers = []
        out = {
            "uuid": _uuid(z),
            "name": z.GetZoneName(),
            "net": z.GetNetname(),
            "layers": layers,
            "priority": z.GetAssignedPriority(),
            "ruleArea": bool(z.GetIsRuleArea()),
        }
        if out["ruleArea"]:
            out["forbid"] = sorted(
                k for k, setter in RULE_AREA_FORBIDS.items() if getattr(z, "Get" + setter[3:])()
            )
        try:
            out["areaMm2"] = round(
                sum(g.area(o) - sum(g.area(h) for h in hs) for o, hs in g.zone_polygons(z)), 3
            )
        except Exception:
            pass
        return out

    def _forbid_flags(self, forbid: Any) -> Tuple[Dict[str, bool], Optional[str]]:
        if isinstance(forbid, str):
            forbid = [forbid]
        if not isinstance(forbid, (list, tuple)) or not forbid:
            return {}, f"forbid must list some of {sorted(RULE_AREA_FORBIDS)}"
        flags = {k: False for k in RULE_AREA_FORBIDS}
        for item in forbid:
            key = _FORBID_ALIASES.get(str(item).lower(), str(item).lower())
            if key not in RULE_AREA_FORBIDS:
                return {}, f"unknown forbid '{item}'; use {sorted(RULE_AREA_FORBIDS)}"
            flags[key] = True
        return flags, None

    @staticmethod
    def _set_layers(zone: Any, layer_ids: List[int]) -> None:
        if len(layer_ids) == 1:
            zone.SetLayer(layer_ids[0])
        else:
            lset = pcbnew.LSET()
            for lid in layer_ids:
                lset.AddLayer(lid)
            zone.SetLayerSet(lset)

    def _polygon_nm(self, value: Any) -> Tuple[List[Tuple[int, int]], Optional[str]]:
        if isinstance(value, str) and value.lower() in ("board", "outline", "board_outline"):
            outline = self._board_outline()
            pts = outline[0][0] if len(outline) == 1 else self._board_rect()
            if not pts:
                return [], "polygon 'board' needs a board outline (Edge.Cuts)"
        else:
            try:
                pts = g.parse_polygon(value)
            except ValueError as exc:
                return [], str(exc)
        return [(_nm(x), _nm(y)) for x, y in pts], None

    def add_rule_area(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add a rule area (keepout) on ``layers`` forbidding the listed items.

        ``polygon``: [[x, y], ...] mm, a {x1,y1,x2,y2} box or "board".
        ``forbid``: tracks, vias, pads, pours, footprints. ``name`` optional.
        """
        err = self._no_board()
        if err:
            return err
        layer_ids, error = self._copper_layers(params.get("layers") or params.get("layer"))
        if error:
            return _fail(f"Invalid layers: {error}")
        flags, error = self._forbid_flags(params.get("forbid"))
        if error:
            return _fail(error)
        pts, error = self._polygon_nm(params.get("polygon") or params.get("points"))
        if error:
            return _fail(f"Invalid polygon: {error}")
        summary = {
            "layers": [self._layer_name(lid) for lid in layer_ids],
            "forbid": sorted(k for k, v in flags.items() if v),
            "name": params.get("name") or "",
            "pointCount": len(pts),
        }
        if params.get("dryRun"):
            return {
                "success": True,
                "dryRun": True,
                "_skipAutoSave": True,
                "message": "Would add rule area",
                "ruleArea": summary,
            }
        baseline = self._drc_baseline(params)
        zone = pcbnew.ZONE(self.board)
        if not isinstance(zone, pcbnew.ZONE):
            return _fail(f"ZONE() returned {type(zone).__name__} (broken SWIG proxy)")
        zone.SetIsRuleArea(True)
        self._set_layers(zone, layer_ids)
        for key, setter in RULE_AREA_FORBIDS.items():
            getattr(zone, setter)(flags[key])
        if params.get("name"):
            zone.SetZoneName(str(params["name"]))
        self._routing()._set_zone_outline(zone, pts)
        self.board.Add(zone)
        result = {
            "success": True,
            "message": f"Added rule area on {', '.join(summary['layers'])} forbidding {', '.join(summary['forbid'])}",
            "ruleArea": self._zone_summary(zone),
        }
        return self._commit(result, params, changed=True, baseline=baseline)

    def _find_zones(self, params: Dict[str, Any]) -> Tuple[List[Any], Optional[Dict[str, Any]]]:
        uuid = params.get("zone") or params.get("uuid")
        name = params.get("zoneName")
        net = params.get("net")
        layer = params.get("layer")
        if not uuid and not name and net is None:
            return [], _fail("Identify the zone with zone (uuid), zoneName, or net (+ layer)")
        lid = None
        if layer:
            lid, error = self._copper_layer(layer)
            if error:
                return [], _fail(error)
        found = []
        for z in self.board.Zones():
            if not isinstance(z, pcbnew.ZONE):
                return [], _fail(f"board zone list returned {type(z).__name__} (broken SWIG proxy)")
            if uuid and _uuid(z) != uuid:
                continue
            if name and z.GetZoneName() != name:
                continue
            if net is not None and z.GetNetname() != net:
                continue
            if lid is not None and lid not in set(z.GetLayerSet().Seq()):
                continue
            found.append(z)
        if not found:
            sample = [self._zone_summary(z) for z in list(self.board.Zones())[:25]]
            return [], _fail(
                "No zone matches",
                "zones on the board (first 25) are listed in 'zones'",
                zones=sample,
            )
        if len(found) > 1 and not params.get("all"):
            return [], _fail(
                f"{len(found)} zones match; narrow with layer/zone uuid or pass all=true",
                zones=[self._zone_summary(z) for z in found],
            )
        return found, None

    def edit_zone(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Change an existing zone or rule area.

        Find it by ``zone`` (uuid), ``zoneName``, or ``net`` (+ ``layer``);
        ``all`` edits every match. Changes: ``newNet``, ``layers``,
        ``polygon``, ``priority``, ``clearance``, ``minWidth``,
        ``padConnection``, ``thermalGap``, ``thermalSpokeWidth``,
        ``islandRemoval``, ``minIslandArea``, ``fillType``, ``rename``,
        and for rule areas ``forbid``. Fills are cleared when copper geometry
        changes; run refill_zones.
        """
        err = self._no_board()
        if err:
            return err
        zones, error = self._find_zones(params)
        if error:
            return error
        from commands.routing import ZONE_ISLAND_MODES, ZONE_PAD_CONNECTIONS

        # Validate every change before touching anything.
        new_net, new_layers, pts, flags = None, None, None, None
        if params.get("newNet") is not None:
            new_net, error = self._net(params["newNet"])
            if error:
                return _fail(error)
        if params.get("layers"):
            new_layers, error = self._copper_layers(params["layers"])
            if error:
                return _fail(error)
        if params.get("polygon") is not None:
            pts, error = self._polygon_nm(params["polygon"])
            if error:
                return _fail(f"Invalid polygon: {error}")
        if params.get("forbid") is not None:
            if not all(z.GetIsRuleArea() for z in zones):
                return _fail("forbid applies to rule areas only")
            flags, error = self._forbid_flags(params["forbid"])
            if error:
                return _fail(error)
        conn = params.get("padConnection")
        if conn is not None and str(conn).lower() not in ZONE_PAD_CONNECTIONS:
            return _fail(f"padConnection must be one of {sorted(ZONE_PAD_CONNECTIONS)}")
        island = params.get("islandRemoval")
        if island is not None and str(island).lower() not in ZONE_ISLAND_MODES:
            return _fail(f"islandRemoval must be one of {sorted(ZONE_ISLAND_MODES)}")
        fill = params.get("fillType")
        if fill is not None and fill not in ("solid", "hatched"):
            return _fail("fillType must be solid or hatched")
        for key in (
            "priority",
            "clearance",
            "minWidth",
            "thermalGap",
            "thermalSpokeWidth",
            "minIslandArea",
        ):
            if params.get(key) is not None:
                try:
                    float(params[key])
                except (TypeError, ValueError):
                    return _fail(f"{key} must be a number")
        option_keys = (
            "priority",
            "clearance",
            "minWidth",
            "padConnection",
            "thermalGap",
            "thermalSpokeWidth",
            "islandRemoval",
            "minIslandArea",
            "fillType",
        )
        changes = [k for k in option_keys if params.get(k) is not None]
        changes += [
            k
            for k in ("newNet", "layers", "polygon", "forbid", "rename")
            if params.get(k) is not None
        ]
        if not changes:
            return _fail(
                "Nothing to change",
                f"pass one of {list(option_keys) + ['newNet', 'layers', 'polygon', 'forbid', 'rename']}",
            )

        before = [self._zone_summary(z) for z in zones]
        if params.get("dryRun"):
            return {
                "success": True,
                "dryRun": True,
                "_skipAutoSave": True,
                "message": f"Would change {', '.join(changes)} on {len(zones)} zone(s)",
                "zones": before,
            }
        baseline = self._drc_baseline(params)
        routing = self._routing()
        for z in zones:
            hatched = z.GetFillMode() == pcbnew.ZONE_FILL_MODE_HATCH_PATTERN
            opts = {k: params[k] for k in option_keys if params.get(k) is not None}
            if params.get("rename") is not None:
                opts["name"] = params["rename"]
            if not z.GetIsRuleArea():
                _, error = routing._configure_zone(
                    z,
                    opts,
                    {
                        "priority": z.GetAssignedPriority(),
                        "fillType": "hatched" if hatched else "solid",
                    },
                )
                if error:  # pragma: no cover - validated above
                    return error
            else:
                if "priority" in opts:
                    z.SetAssignedPriority(int(float(opts["priority"])))
                if "name" in opts:
                    z.SetZoneName(str(opts["name"]))
            if new_net is not None:
                z.SetNet(new_net)
            if new_layers is not None:
                self._set_layers(z, new_layers)
            if pts is not None:
                z.RemoveAllContours()
                routing._set_zone_outline(z, pts)
            if flags is not None:
                for key, setter in RULE_AREA_FORBIDS.items():
                    getattr(z, setter)(flags[key])
            if any(
                k in changes
                for k in ("newNet", "layers", "polygon", "clearance", "minWidth", "priority")
            ):
                try:
                    z.UnFill()
                except Exception:
                    pass
        after = [self._zone_summary(z) for z in zones]
        result = {
            "success": True,
            "message": f"Changed {', '.join(changes)} on {len(zones)} zone(s); run refill_zones to refill",
            "zones": after,
            "before": before,
        }
        return self._commit(result, params, changed=True, baseline=baseline)

    def set_pad_zone_connection(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set per-pad zone connection overrides.

        ``pads``: ["U1:5", {ref, pad}], and/or ``refs`` (every pad of those
        parts), optionally narrowed by ``nets`` (globs). ``connection``:
        solid | thermal | none | inherit | thermal_tht. ``thermalSpokeWidth``
        (mm) and ``thermalGap`` (mm) set local overrides where SWIG allows.
        """
        err = self._no_board()
        if err:
            return err
        conn = str(params.get("connection", "")).lower()
        if conn not in PAD_CONNECTIONS:
            return _fail(
                f"connection must be one of solid, thermal, none, inherit, thermal_tht; got {conn!r}"
            )
        const = getattr(pcbnew, PAD_CONNECTIONS[conn])
        names = {
            getattr(pcbnew, v): k
            for k, v in PAD_CONNECTIONS.items()
            if k in ("solid", "thermal", "none", "thermal_tht", "inherit")
        }
        targets: List[Tuple[str, Any]] = []
        errors = []
        nets = params.get("nets")
        if isinstance(nets, str):
            nets = [nets]
        for spec in params.get("pads") or []:
            if isinstance(spec, str) and ":" in spec:
                ref, num = spec.rsplit(":", 1)
            elif isinstance(spec, dict):
                ref, num = spec.get("ref"), spec.get("pad")
            else:
                errors.append(f"bad pad spec {spec!r}; use 'REF:PAD' or {{ref, pad}}")
                continue
            fp = self.board.FindFootprintByReference(str(ref))
            if fp is None:
                errors.append(f"footprint '{ref}' not found")
                continue
            hits = [p for p in fp.Pads() if str(p.GetNumber()) == str(num)]
            if not hits:
                errors.append(f"{ref} has no pad '{num}'")
            targets += [(str(ref), p) for p in hits]
        for ref in params.get("refs") or []:
            fp = self.board.FindFootprintByReference(str(ref))
            if fp is None:
                errors.append(f"footprint '{ref}' not found")
                continue
            targets += [(str(ref), p) for p in fp.Pads()]
        if errors:
            return _fail(f"{len(errors)} problem(s); nothing was changed", errors, errors=errors)
        if nets:
            targets = [(r, p) for r, p in targets if net_matches_any(p.GetNetname(), nets)]
        if not targets:
            return _fail("No pads selected", "pass pads ['U1:5'] and/or refs ['U1'] (nets narrows)")
        try:
            spoke = (
                _nm(params["thermalSpokeWidth"])
                if params.get("thermalSpokeWidth") is not None
                else None
            )
            gap = _nm(params["thermalGap"]) if params.get("thermalGap") is not None else None
        except (TypeError, ValueError):
            return _fail("thermalSpokeWidth and thermalGap must be numbers (mm)")

        changed, unchanged = [], 0
        seen = set()
        for ref, p in targets:
            key = (ref, _uuid(p))
            if key in seen:
                continue
            seen.add(key)
            cur = p.GetLocalZoneConnection()
            if cur == const and spoke is None and gap is None:
                unchanged += 1
                continue
            changed.append((ref, p, cur))
        summary = [
            {
                "ref": r,
                "pad": p.GetNumber(),
                "net": p.GetNetname(),
                "from": names.get(c, str(c)),
                "to": conn,
            }
            for r, p, c in changed
        ]
        if params.get("dryRun") or not changed:
            res = {
                "success": True,
                "dryRun": bool(params.get("dryRun")),
                "message": f"{'Would change' if params.get('dryRun') else 'Changed'} {len(changed)} pad(s); {unchanged} already {conn}",
                "pads": summary,
            }
            return self._commit(res, params, changed=False)
        baseline = self._drc_baseline(params)
        warnings = []
        for _ref, p, _cur in changed:
            p.SetLocalZoneConnection(const)
            if spoke is not None:
                p.SetLocalThermalSpokeWidthOverride(spoke)
            if gap is not None:
                try:
                    p.SetLocalThermalGapOverride(gap)
                except TypeError:
                    # std::optional<int> is not constructible from Python on 10.0.x.
                    if hasattr(p, "SetThermalGap"):
                        p.SetThermalGap(gap)
                    elif not warnings:
                        warnings.append(
                            "thermalGap override is not settable through this KiCad's SWIG"
                        )
        result = {
            "success": True,
            "message": f"Set {conn} zone connection on {len(changed)} pad(s); {unchanged} already {conn}",
            "pads": summary,
            "warnings": warnings,
        }
        return self._commit(result, params, changed=True, baseline=baseline)

    def split_plane(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Build a multi-net plane on one layer in one call.

        ``regions``: ``{net: polygon}`` or ``[{net, polygon, priority?}]``;
        earlier regions get higher priority so overlaps resolve in list order
        (from ``basePriority``, default 1). A polygon may be "board" for the
        board outline's box. ``replaceExisting`` first removes the copper zones
        already on that layer. Zone options (clearance, minWidth, padConnection,
        thermalGap, thermalSpokeWidth, islandRemoval, fillType) apply to all.
        ``layerType: power`` marks the layer as a plane.
        """
        err = self._no_board()
        if err:
            return err
        lid, error = self._copper_layer(params.get("layer"))
        if error:
            return _fail(f"Invalid layer: {error}")
        regions = params.get("regions")
        if isinstance(regions, dict):
            regions = [{"net": k, "polygon": v} for k, v in regions.items()]
        if not isinstance(regions, list) or not regions:
            return _fail("regions must be {net: polygon} or [{net, polygon, priority?}]")
        try:
            base = int(params.get("basePriority", 1))
        except (TypeError, ValueError):
            return _fail("basePriority must be an integer")
        from commands.routing import ZONE_ISLAND_MODES, ZONE_PAD_CONNECTIONS

        conn = params.get("padConnection")
        if conn is not None and str(conn).lower() not in ZONE_PAD_CONNECTIONS:
            return _fail(f"padConnection must be one of {sorted(ZONE_PAD_CONNECTIONS)}")
        island = params.get("islandRemoval")
        if island is not None and str(island).lower() not in ZONE_ISLAND_MODES:
            return _fail(f"islandRemoval must be one of {sorted(ZONE_ISLAND_MODES)}")
        layer_type = params.get("layerType")
        lt_const = None
        if layer_type is not None:
            lt_const = {
                "power": "LT_POWER",
                "signal": "LT_SIGNAL",
                "mixed": "LT_MIXED",
                "jumper": "LT_JUMPER",
            }.get(str(layer_type).lower())
            if lt_const is None:
                return _fail("layerType must be power, signal, mixed or jumper")

        plan, errors = [], []
        n = len(regions)
        for i, r in enumerate(regions):
            if not isinstance(r, dict):
                errors.append({"index": i, "error": "region must be {net, polygon}"})
                continue
            net, error = self._net(r.get("net"))
            if error or net is None:
                errors.append({"index": i, "error": error or "net is required"})
                continue
            pts, error = self._polygon_nm(r.get("polygon"))
            if error:
                errors.append({"index": i, "net": r.get("net"), "error": error})
                continue
            try:
                prio = int(r["priority"]) if r.get("priority") is not None else base + (n - 1 - i)
            except (TypeError, ValueError):
                errors.append({"index": i, "error": "priority must be an integer"})
                continue
            plan.append({"net": net, "netName": str(r.get("net")), "points": pts, "priority": prio})
        if errors:
            return _fail(
                f"{len(errors)} invalid region(s); nothing was changed", errors, errors=errors
            )

        warnings = []
        polys_mm = [[(x / MM, y / MM) for x, y in p["points"]] for p in plan]
        for i in range(len(plan)):
            for j in range(i + 1, len(plan)):
                if g.polygons_overlap_strictly(polys_mm[i], polys_mm[j]):
                    hi, lo = (
                        (plan[i], plan[j])
                        if plan[i]["priority"] >= plan[j]["priority"]
                        else (plan[j], plan[i])
                    )
                    if plan[i]["priority"] == plan[j]["priority"]:
                        warnings.append(
                            f"regions {plan[i]['netName']} and {plan[j]['netName']} overlap with equal "
                            "priority; KiCad will fill the overlap unpredictably"
                        )
                    else:
                        warnings.append(
                            f"{hi['netName']} overlaps {lo['netName']}; {hi['netName']} wins (higher priority)"
                        )
        existing = [
            z
            for z in self.board.Zones()
            if not z.GetIsRuleArea() and lid in set(z.GetLayerSet().Seq())
        ]
        replace = bool(params.get("replaceExisting", False))
        if existing and not replace:
            warnings.append(
                f"{len(existing)} zone(s) already on {self._layer_name(lid)} were kept; "
                "pass replaceExisting=true to rebuild the layer"
            )
        layer = self._layer_name(lid)
        planned = [
            {
                "net": p["netName"],
                "priority": p["priority"],
                "areaMm2": round(g.area(polys_mm[k]), 3),
            }
            for k, p in enumerate(plan)
        ]
        if params.get("dryRun"):
            return {
                "success": True,
                "dryRun": True,
                "_skipAutoSave": True,
                "message": f"Would build {len(plan)} zone(s) on {layer}"
                + (f" replacing {len(existing)}" if existing and replace else ""),
                "zones": planned,
                "wouldRemove": [self._zone_summary(z) for z in existing] if replace else [],
                "warnings": warnings,
            }

        routing = self._routing()
        built = []
        defaults = {
            "minWidth": 0.25,
            "clearance": params.get("clearance"),
            "padConnection": None,
            "fillType": "solid",
        }
        try:
            for p in plan:
                z = pcbnew.ZONE(self.board)
                if not isinstance(z, pcbnew.ZONE):
                    raise TypeError(f"ZONE() returned {type(z).__name__}")
                z.SetLayer(lid)
                z.SetNet(p["net"])
                opts = {
                    k: params[k]
                    for k in (
                        "clearance",
                        "minWidth",
                        "padConnection",
                        "thermalGap",
                        "thermalSpokeWidth",
                        "islandRemoval",
                        "minIslandArea",
                        "fillType",
                    )
                    if params.get(k) is not None
                }
                opts["priority"] = p["priority"]
                _, error = routing._configure_zone(z, opts, defaults)
                if error:
                    raise ValueError(error.get("errorDetails") or error.get("message"))
                if params.get("namePrefix"):
                    z.SetZoneName(f"{params['namePrefix']}{p['netName']}")
                routing._set_zone_outline(z, p["points"])
                built.append(z)
        except Exception as exc:
            return _fail("Could not build the plane zones; the board is unchanged", str(exc))

        baseline = self._drc_baseline(params)
        removed = []
        if replace:
            for z in existing:
                removed.append(self._zone_summary(z))
                delete_board_item(self.board, z)
        for z in built:
            self.board.Add(z)
        if lt_const:
            try:
                self.board.SetLayerType(lid, getattr(pcbnew, lt_const))
            except Exception as exc:
                warnings.append(f"could not set the layer type: {exc}")
        result = {
            "success": True,
            "message": f"Built {len(built)} zone(s) on {layer}"
            + (f", removed {len(removed)}" if removed else "")
            + "; run refill_zones to fill them",
            "layer": layer,
            "zones": [self._zone_summary(z) for z in built],
            "removed": removed,
            "warnings": warnings,
        }
        return self._commit(result, params, changed=True, baseline=baseline)
