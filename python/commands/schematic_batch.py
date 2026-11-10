"""
Batch schematic authoring commands.

Tools that place / edit / connect many things in one call to avoid per-item round-trips:
  - batch_add_components:            add many components at once
  - batch_edit_schematic_components: edit many components (value/footprint/reference/...)
  - replace_schematic_component:     swap a symbol, preserving position and field values
  - batch_add_no_connects:           add no-connect (X) flags to many pins
  - batch_connect:                   place net labels on many pins (with facing-label wiring)
  - batch_add_and_connect:           place components and wire their nets in one call

The command class is constructed with a reference to the KiCADInterface so it can reuse
existing single-item handlers (add/edit/get_schematic_component) and the footprint library.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List

from commands.dynamic_symbol_loader import DynamicSymbolLoader
from commands.pin_locator import PinLocator
from commands.schematic_autowire import (
    _DIRS,
    _boxes_overlap,
    _label_bbox,
    _orthogonal_route,
    _route_conflict,
    _sheet_geometry,
)
from commands.schematic_text_utils import (
    _extract_property_position,
    _find_facing_label,
    _find_placed_symbol_block,
    _find_project_root,
    _move_property_in_block,
)
from commands.wire_manager import WireManager
from utils.file_io import FileTransaction, read_text_preserve_newline, write_text_atomic
from utils.symbol_instances import add_missing_instances, instance_report

logger = logging.getLogger("kicad_interface")

_GRID = 1.27  # 50-mil KiCad schematic grid (mm)


def _symbol_has_pins(locator, schematic_file, lib_id) -> bool:
    """Whether the embedded definition of *lib_id* has any pins (False if unknown)."""
    try:
        return bool(locator.get_symbol_pins(schematic_file, lib_id))
    except Exception:
        return False


def _summarize_errors(errors: List[Dict[str, Any]], limit: int = 5) -> str:
    """One-line digest of per-component failures, for callers that only show the message."""
    parts = [
        f"{e.get('reference', '?')} ({e.get('symbol', '?')}): {e.get('error', 'unknown error')}"
        for e in errors[:limit]
    ]
    if len(errors) > limit:
        parts.append(f"... and {len(errors) - limit} more")
    return "; ".join(parts)


def _snap(val: float) -> float:
    """Round a coordinate to the nearest 50-mil (1.27mm) schematic grid point."""
    return round(round(val / _GRID) * _GRID, 4)


def _field_positions_for_pins(cx, cy, all_pins):
    """Compute (Reference, Value) field positions from pin axis. Returns list of (name,x,y,angle)."""
    off = 2.54
    if len(all_pins) == 2:
        coords = list(all_pins.values())
        p1x = float(coords[0][0]) if isinstance(coords[0], (list, tuple)) else float(coords[0]["x"])
        p1y = float(coords[0][1]) if isinstance(coords[0], (list, tuple)) else float(coords[0]["y"])
        p2x = float(coords[1][0]) if isinstance(coords[1], (list, tuple)) else float(coords[1]["x"])
        p2y = float(coords[1][1]) if isinstance(coords[1], (list, tuple)) else float(coords[1]["y"])
        if abs(p1y - p2y) > abs(p1x - p2x):  # vertical pin axis -> labels left/right
            return [("Reference", round(cx - off, 4), cy, 0), ("Value", round(cx + off, 4), cy, 0)]
        return [("Reference", cx, round(cy - off, 4), 0), ("Value", cx, round(cy + off, 4), 0)]
    if all_pins:  # multi-pin -> above topmost / below bottommost
        pin_ys = [
            float(c[1]) if isinstance(c, (list, tuple)) else float(c["y"])
            for c in all_pins.values()
        ]
        return [
            ("Reference", cx, round(min(pin_ys) - off, 4), 0),
            ("Value", cx, round(max(pin_ys) + off, 4), 0),
        ]
    return [("Reference", cx, round(cy - off, 4), 0), ("Value", cx, round(cy + off, 4), 0)]


def _bbox_from_pins(all_pins, cx, cy):
    """body_bbox from pin spread (±1.27mm), or center ±2.54mm fallback."""
    try:
        if all_pins:
            xs = [c[0] if isinstance(c, (list, tuple)) else c["x"] for c in all_pins.values()]
            ys = [c[1] if isinstance(c, (list, tuple)) else c["y"] for c in all_pins.values()]
            return {
                "x_min": min(xs) - _GRID,
                "y_min": min(ys) - _GRID,
                "x_max": max(xs) + _GRID,
                "y_max": max(ys) + _GRID,
            }
    except Exception:
        pass
    return {"x_min": cx - 2.54, "y_min": cy - 2.54, "x_max": cx + 2.54, "y_max": cy + 2.54}


class SchematicBatchCommands:
    """Handlers for batch schematic authoring. Holds a back-reference to KiCADInterface."""

    def __init__(self, iface):
        self.iface = iface

    def batch_add_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add multiple components to a schematic in a single call."""
        logger.info("Batch adding components to schematic")
        try:
            schematic_path = params.get("schematicPath")
            components = params.get("components", [])
            origin_x = params.get("origin_x", 0) or 0
            origin_y = params.get("origin_y", 0) or 0
            auto_position_fields = params.get("auto_position_fields", True)

            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if not components:
                return {
                    "success": False,
                    "message": "components list is required and must be non-empty",
                }

            schematic_file = Path(schematic_path)
            project_path = schematic_file.parent
            loader = DynamicSymbolLoader(project_path=project_path)
            locator = PinLocator()

            results: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []

            for comp in components:
                symbol = comp.get("symbol", "")
                if ":" not in symbol:
                    errors.append(
                        {
                            "symbol": symbol,
                            "reference": comp.get("reference", "?"),
                            "error": "symbol must be 'Library:SymbolName'",
                        }
                    )
                    continue

                library, sym_name = symbol.split(":", 1)
                reference = comp.get("reference", "X?")
                value = comp.get("value", sym_name)
                footprint = comp.get("footprint", "")
                pos = comp.get("position", {})
                x = (pos.get("x", 0) if isinstance(pos, dict) else 0) + origin_x
                y = (pos.get("y", 0) if isinstance(pos, dict) else 0) + origin_y
                rotation = comp.get("rotation", 0)
                include_pins = comp.get("includePins", False)
                # Multi-unit parts place one unit per entry, all sharing the
                # reference. Without this the batch could only ever place unit 1,
                # so an FPGA split into five units had to go in one call per unit
                # — exactly the round-trips the batch exists to avoid.
                unit = int(comp.get("unit", 1) or 1)

                # Everything written for this component — lib_symbols entry,
                # instance, field moves — is undone if any step fails, so a
                # reported failure never leaves an orphan instance behind (#5c).
                txn = FileTransaction(schematic_file)
                try:
                    loader.add_component(
                        schematic_file,
                        library,
                        sym_name,
                        reference=reference,
                        value=value,
                        footprint=footprint,
                        x=x,
                        y=y,
                        unit=unit,
                        angle=rotation,
                        project_path=project_path,
                    )

                    entry: Dict[str, Any] = {
                        "reference": reference,
                        "symbol": symbol,
                        "unit": unit,
                        "snapped_position": {"x": _snap(x), "y": _snap(y)},
                    }
                    entry.update(instance_report(loader.placed_instances))

                    if footprint and self.iface.footprint_library.find_footprint(footprint) is None:
                        entry["footprint_warning"] = (
                            f"Footprint '{footprint}' was not found in any registered footprint library "
                            "(validation only — the string WAS written to the Footprint field). "
                            "Use search_footprints to find a valid footprint string."
                        )

                    locator._schematic_cache.pop(str(schematic_file), None)
                    all_pins = locator.get_all_symbol_pins(schematic_file, reference) or {}
                    if not all_pins and _symbol_has_pins(locator, schematic_file, symbol):
                        raise ValueError(
                            f"{reference} was written but its pins could not be located "
                            "(the sheet may not parse); placement rolled back"
                        )

                    block_text = None
                    raw_content, newline = read_text_preserve_newline(schematic_file)
                    block_text, block_start, block_end = _find_placed_symbol_block(
                        raw_content, reference, unit
                    )
                    if auto_position_fields and block_text:
                        cx, cy = _snap(x), _snap(y)
                        new_block = block_text
                        for prop, px, py, pa in _field_positions_for_pins(cx, cy, all_pins):
                            new_block, _ = _move_property_in_block(
                                new_block, prop, px, py, pa, True
                            )
                        if new_block != block_text:
                            raw_content = (
                                raw_content[:block_start] + new_block + raw_content[block_end + 1 :]
                            )
                            write_text_atomic(schematic_file, raw_content, newline)
                            block_text = new_block

                    if block_text:
                        ref_pos = _extract_property_position(block_text, "Reference")
                        val_pos = _extract_property_position(block_text, "Value")
                        if ref_pos:
                            entry["ref_position"] = ref_pos
                        if val_pos:
                            entry["value_position"] = val_pos

                    if include_pins:
                        pins_def = locator.get_symbol_pins(schematic_file, symbol) or {}
                        pins = {
                            pin_num: {
                                "x": coords[0],
                                "y": coords[1],
                                "name": pins_def.get(str(pin_num), {}).get("name", str(pin_num)),
                            }
                            for pin_num, coords in all_pins.items()
                        }
                        entry["pins"] = pins
                        if not pins:
                            entry["pins_error"] = (
                                f"Pin extraction returned no data for {reference} ({symbol}). "
                                "Use get_schematic_pin_locations as a follow-up if needed."
                            )

                    entry["body_bbox"] = _bbox_from_pins(all_pins, _snap(x), _snap(y))
                    results.append(entry)

                except Exception as e:
                    logger.error(f"Error adding {reference} ({symbol}): {e}")
                    try:
                        txn.rollback()
                    except OSError as rb_err:  # pragma: no cover - disk-level failure
                        logger.error(f"Rollback of {schematic_file} failed: {rb_err}")
                    errors.append(
                        {"symbol": symbol, "reference": reference, "error": str(e) or repr(e)}
                    )

            # Placement already writes an instance entry for every use of this
            # sheet (#428). This fills in any symbol already on the sheet that
            # lacks one, and only for this sheet: running the fixer over every
            # parent that names it rewalked each parent's whole subtree.
            # Best-effort.
            try:
                add_missing_instances([schematic_file])
            except Exception as e:
                logger.warning(f"Instance entries for {schematic_file} not checked: {e}")

            placement_bbox = None
            bbs = [r["body_bbox"] for r in results if "body_bbox" in r]
            if bbs:
                placement_bbox = {
                    "x_min": min(b["x_min"] for b in bbs),
                    "y_min": min(b["y_min"] for b in bbs),
                    "x_max": max(b["x_max"] for b in bbs),
                    "y_max": max(b["y_max"] for b in bbs),
                }

            return {
                "success": len(errors) == 0,
                "added": results,
                "errors": errors,
                "added_count": len(results),
                "error_count": len(errors),
                "placement_bbox": placement_bbox,
            }

        except Exception as e:
            logger.error(f"Error in batch_add_components: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def batch_edit_schematic_components(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Edit multiple components in one call via the single-component edit handler."""
        logger.info("Batch editing schematic components")
        schematic_path = params.get("schematicPath")
        components = params.get("components")

        if not schematic_path:
            return {"success": False, "message": "schematicPath is required"}
        if not components or not isinstance(components, dict):
            return {
                "success": False,
                "message": "components must be a dict {reference: {footprint?, value?, newReference?}}",
            }

        updated: Dict[str, Any] = {}
        errors: List[Dict[str, Any]] = []
        for reference, props in components.items():
            sub_params = {"schematicPath": schematic_path, "reference": reference, **props}
            result = self.iface._handle_edit_schematic_component(sub_params)
            if result.get("success"):
                updated[reference] = result.get("updated", {})
            else:
                errors.append(
                    {"reference": reference, "error": result.get("message", "Unknown error")}
                )

        return {
            "success": len(errors) == 0,
            "updated_count": len(updated),
            "error_count": len(errors),
            "updated": updated,
            "errors": errors,
        }

    def replace_schematic_component(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Replace a placed symbol with a different symbol, preserving position and fields."""
        logger.info("Replacing schematic component")
        try:
            schematic_path = params.get("schematicPath")
            reference = params.get("reference")
            new_symbol = params.get("newSymbol")
            new_rotation = params.get("newRotation")

            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if not reference:
                return {"success": False, "message": "reference is required"}
            if not new_symbol:
                return {
                    "success": False,
                    "message": "newSymbol is required (e.g. 'Device:D_Zener')",
                }
            if ":" not in new_symbol:
                return {"success": False, "message": "newSymbol must be in 'Library:Symbol' format"}
            new_library, new_type = new_symbol.split(":", 1)

            sch_file = Path(schematic_path)
            if not sch_file.exists():
                return {"success": False, "message": f"Schematic not found: {schematic_path}"}

            comp_info = self.iface._handle_get_schematic_component(
                {"schematicPath": schematic_path, "reference": reference}
            )
            if not comp_info.get("success"):
                return {
                    "success": False,
                    "message": f"Cannot find component '{reference}': {comp_info.get('message', '')}",
                }

            old_pos = comp_info.get("position", {})
            old_x = old_pos.get("x", 0)
            old_y = old_pos.get("y", 0)
            old_rotation = old_pos.get("angle", 0)
            old_fields = comp_info.get("fields", {})
            use_rotation = new_rotation if new_rotation is not None else old_rotation

            content, newline = read_text_preserve_newline(sch_file)
            block_text, block_start, block_end = _find_placed_symbol_block(content, reference)
            if block_text is None:
                return {
                    "success": False,
                    "message": f"Component '{reference}' not found in schematic file",
                }

            derived_project_path = _find_project_root(sch_file.parent)
            loader = DynamicSymbolLoader(project_path=derived_project_path)

            def _field_value(name, default):
                fd = old_fields.get(name)
                return (
                    fd.get("value", default)
                    if isinstance(fd, dict)
                    else (fd if fd is not None else default)
                )

            old_value = _field_value("Value", new_type)
            old_footprint = _field_value("Footprint", "")

            # Removing the old instance and placing the new one is one change:
            # if the new symbol cannot be placed, the old one comes back.
            with FileTransaction(sch_file):
                trim_start = block_start
                while trim_start > 0 and content[trim_start - 1] in (" ", "\t"):
                    trim_start -= 1
                if trim_start > 0 and content[trim_start - 1] == "\n":
                    trim_start -= 1
                content = content[:trim_start] + content[block_end + 1 :]
                write_text_atomic(sch_file, content, newline)

                loader.add_component(
                    sch_file,
                    new_library,
                    new_type,
                    reference=reference,
                    value=old_value,
                    footprint=old_footprint,
                    x=old_x,
                    y=old_y,
                    angle=use_rotation,
                    project_path=derived_project_path,
                )

            fields_to_restore = {}
            for fname, fdata in old_fields.items():
                if fname in ("Reference", "Value", "Footprint"):
                    continue
                fields_to_restore[fname] = (
                    fdata.get("value", "") if isinstance(fdata, dict) else str(fdata)
                )

            if fields_to_restore:
                # Restore the remaining fields through the block-scoped component
                # editor. A plain regex substitution over the whole file (the
                # previous implementation) replaced the FIRST matching
                # (property "<name>" ...) in the file, which for common field
                # names like "Description" lives inside lib_symbols — corrupting
                # an unrelated library symbol definition instead of this
                # component instance.
                restore_result = self.iface._handle_edit_schematic_component(
                    {
                        "schematicPath": schematic_path,
                        "reference": reference,
                        "properties": {
                            fname: {"value": fval} for fname, fval in fields_to_restore.items()
                        },
                    }
                )
                if not restore_result.get("success"):
                    logger.warning(
                        "Could not restore fields %s on %s after replace: %s",
                        sorted(fields_to_restore),
                        reference,
                        restore_result.get("message"),
                    )

            pin_locator = PinLocator()
            pins_raw = pin_locator.get_all_symbol_pins(sch_file, reference) or {}
            pins_def = pin_locator.get_symbol_pins(sch_file, f"{new_library}:{new_type}") or {}
            pins = {
                pin_num: {
                    "x": coords[0],
                    "y": coords[1],
                    "name": pins_def.get(str(pin_num), {}).get("name", str(pin_num)),
                }
                for pin_num, coords in pins_raw.items()
            }

            return {
                "success": True,
                "reference": reference,
                "newSymbol": new_symbol,
                "position": {"x": old_x, "y": old_y, "rotation": use_rotation},
                "pins": pins,
                "message": f"Replaced {reference} with {new_symbol} at ({old_x}, {old_y})",
            }

        except Exception as e:
            logger.error(f"Error replacing schematic component: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def batch_add_no_connects(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Add no-connect flags to multiple pins in a single call."""
        logger.info("Batch adding no-connect flags")
        try:
            schematic_path = params.get("schematicPath")
            pins = params.get("pins", [])
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if not pins:
                return {"success": False, "message": "pins list is required and must be non-empty"}

            sch_path = Path(schematic_path)
            locator = PinLocator()
            placed: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []

            for entry in pins:
                comp_ref = entry.get("componentRef")
                pin_name = entry.get("pinName")
                if not comp_ref or pin_name is None:
                    failed.append(
                        {"entry": entry, "reason": "componentRef and pinName are required"}
                    )
                    continue
                try:
                    pin_loc = locator.get_pin_location(sch_path, comp_ref, str(pin_name))
                    if not pin_loc:
                        all_pins = locator.get_all_symbol_pins(sch_path, comp_ref) or {}
                        if len(all_pins) == 1:
                            pin_loc = next(iter(all_pins.values()))
                        else:
                            avail = sorted(all_pins.keys()) if all_pins else []
                            failed.append(
                                {
                                    "componentRef": comp_ref,
                                    "pinName": str(pin_name),
                                    "reason": f"Pin not found; available: {avail}",
                                }
                            )
                            continue
                    if WireManager.add_no_connect(sch_path, pin_loc):
                        placed.append(
                            {
                                "componentRef": comp_ref,
                                "pinName": str(pin_name),
                                "position": {"x": pin_loc[0], "y": pin_loc[1]},
                            }
                        )
                    else:
                        failed.append(
                            {
                                "componentRef": comp_ref,
                                "pinName": str(pin_name),
                                "reason": "add_no_connect returned False",
                            }
                        )
                except Exception as pin_err:
                    failed.append(
                        {"componentRef": comp_ref, "pinName": str(pin_name), "reason": str(pin_err)}
                    )

            return {
                "success": len(failed) == 0,
                "message": f"Placed {len(placed)} no-connect marker(s), {len(failed)} failed",
                "placed": placed,
                "failed": failed,
            }
        except Exception as e:
            logger.error(f"Error in batch_add_no_connects: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def batch_connect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place net labels on multiple pins in a single call, with facing-label wiring.

        labelType (default "label") selects the label kind:
          - "label":        sheet-local net label (connects only within this sheet)
          - "global_label": connects across all sheets in the design by name
        Facing-label auto-wiring only applies to local labels; global labels join
        their net by name, so one is placed per pin with no inter-label wiring.
        """
        logger.info("Batch connect: placing net labels on multiple pins")
        try:
            schematic_path = params.get("schematicPath")
            connections = params.get("connections")
            replace = bool(params.get("replace", False))
            avoid_fields = bool(params.get("avoidFieldOverlap", True))
            label_type = params.get("labelType") or params.get("label_type") or "label"
            if label_type not in ("label", "global_label"):
                return {
                    "success": False,
                    "message": (
                        f"invalid labelType '{label_type}'; expected 'label' or 'global_label'"
                    ),
                }

            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}
            if not connections or not isinstance(connections, dict):
                return {
                    "success": False,
                    "message": "connections must be a dict {ref: {pin: netName}}",
                }

            locator = PinLocator()
            sch_path = Path(schematic_path)

            try:
                from commands.schematic import SchematicLoadError, SchematicManager

                SchematicManager.load_schematic(str(sch_path))
            except SchematicLoadError as parse_err:
                response = parse_err.to_response()
                response["message"] += " All pin operations aborted."
                return response

            placed: List[Dict[str, Any]] = []
            failed: List[Dict[str, Any]] = []
            placed_positions: Dict[tuple, str] = {}

            for ref, pin_map in connections.items():
                if not isinstance(pin_map, dict):
                    failed.append({"ref": ref, "reason": "pin_map must be a dict {pin: netName}"})
                    continue
                for pin_id, net_name in pin_map.items():
                    try:
                        resolved_pin = str(pin_id)
                        position = locator.get_pin_location(sch_path, ref, resolved_pin)
                        if not position:
                            all_pins = locator.get_all_symbol_pins(sch_path, ref) or {}
                            if len(all_pins) == 1:
                                resolved_pin = next(iter(all_pins))
                                position = all_pins[resolved_pin]
                            else:
                                avail = sorted(all_pins.keys()) if all_pins else []
                                failed.append(
                                    {
                                        "ref": ref,
                                        "pin": resolved_pin,
                                        "reason": f"pin not found; available: {avail}",
                                    }
                                )
                                continue

                        pos_key = (round(float(position[0]), 2), round(float(position[1]), 2))
                        if placed_positions.get(pos_key) == net_name:
                            placed.append(
                                {
                                    "ref": ref,
                                    "pin": str(pin_id),
                                    "net": net_name,
                                    "position": {"x": position[0], "y": position[1]},
                                    "note": "deduped: label already placed at this coordinate for this net",
                                }
                            )
                            continue

                        # Orient the label along the pin's OUTWARD direction, the
                        # way eeschema does: the label is anchored at the pin
                        # endpoint and its text runs away from the body. KiCad
                        # pairs angle 0/90 with `justify left` (text grows right/
                        # up) and 180/270 with `justify right` (text grows left/
                        # down) — WireManager.add_label follows the same pairing.
                        # Turning the label back on itself (the old 0->180 map)
                        # laid every right-hand pin's net name across the symbol
                        # body, and since `justify` is not separately settable it
                        # could not be corrected without hand-editing the file.
                        raw_angle = locator.get_pin_angle(sch_path, ref, resolved_pin) or 0
                        orientation = int(round(raw_angle / 90) * 90) % 360

                        # replace=true clears EVERY label kind on the pin (local,
                        # global, hierarchical) — and a label at the end of a
                        # bare stub from it — before anything is placed (#5f).
                        # Clearing only local labels left an old global label
                        # behind, merging two nets.
                        removed_labels: List[Dict[str, Any]] = []
                        if replace:
                            removed_labels = self._delete_labels_at(sch_path, position)

                        # Facing-label auto-wiring is only meaningful for local labels;
                        # global labels join their net by name (one placed per pin).
                        existing_pos = (
                            _find_facing_label(sch_path, net_name, position, orientation)
                            if label_type == "label"
                            else None
                        )
                        route = None
                        route_note = None
                        if existing_pos:
                            route = _orthogonal_route(position, orientation, existing_pos)
                            if route is None:
                                route_note = "facing label not reachable orthogonally"
                            else:
                                conflict = _route_conflict(_sheet_geometry(sch_path), route)
                                if conflict:
                                    route_note = f"auto-wire skipped: {conflict}"
                                    route = None
                        if route:
                            if len(route) == 2:
                                ok = WireManager.add_wire(sch_path, route[0], route[1])
                            else:
                                ok = WireManager.add_polyline_wire(sch_path, route)
                            if ok:
                                row = {
                                    "ref": ref,
                                    "pin": str(pin_id),
                                    "net": net_name,
                                    "position": {"x": position[0], "y": position[1]},
                                    "note": f"wired to existing label at ({existing_pos[0]},{existing_pos[1]})",
                                    "wire": [{"x": p[0], "y": p[1]} for p in route],
                                }
                                if removed_labels:
                                    row["replaced"] = removed_labels
                                placed.append(row)
                                placed_positions[pos_key] = net_name
                            else:
                                failed.append(
                                    {
                                        "ref": ref,
                                        "pin": str(pin_id),
                                        "net": net_name,
                                        "reason": "wire-to-existing-label failed",
                                    }
                                )
                            continue

                        label_pos = list(position)
                        offset_note = None
                        if avoid_fields:
                            label_pos, offset_note = self._offset_label_from_fields(
                                sch_path, locator, ref, position, orientation, net_name
                            )

                        if label_pos != list(position) and not WireManager.add_wire(
                            sch_path, list(position), label_pos
                        ):
                            label_pos, offset_note = list(position), None

                        if WireManager.add_label(
                            sch_path,
                            net_name,
                            label_pos,
                            label_type=label_type,
                            orientation=orientation,
                        ):
                            row = {
                                "ref": ref,
                                "pin": str(pin_id),
                                "net": net_name,
                                "position": {"x": position[0], "y": position[1]},
                            }
                            if label_pos != list(position):
                                row["label_position"] = {"x": label_pos[0], "y": label_pos[1]}
                            notes = [n for n in (route_note, offset_note) if n]
                            if notes:
                                row["note"] = "; ".join(notes)
                            if removed_labels:
                                row["replaced"] = removed_labels
                            placed.append(row)
                            placed_positions[pos_key] = net_name
                        else:
                            failed.append(
                                {
                                    "ref": ref,
                                    "pin": str(pin_id),
                                    "net": net_name,
                                    "reason": "add_label failed",
                                }
                            )
                    except Exception as pin_err:
                        failed.append({"ref": ref, "pin": str(pin_id), "reason": str(pin_err)})

            result: Dict[str, Any] = {
                "success": len(failed) == 0,
                "message": f"Placed {len(placed)} label(s), {len(failed)} failed",
                "placed": placed,
                "failed": failed,
            }
            warnings = self._pwr_flag_warnings(sch_path, locator, placed)
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            logger.error(f"Error in batch_connect: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    @staticmethod
    def _delete_labels_at(sch_path, position, tolerance=0.5):
        """Remove every label on the pin at *position* (batch_connect replace=True).

        Covers all three label kinds — ``label``, ``global_label`` and
        ``hierarchical_label`` (#5f: only local labels used to go, so an old
        global label stayed on the pin and merged two nets). A label at the far
        end of a bare stub wire from the pin (one wire, nothing else attached
        at its far end) is removed together with that stub. Returns the
        removed labels as [{type, text, position}].
        """
        removed: List[Dict[str, Any]] = []
        try:
            import sexpdata
            from sexpdata import Symbol
            from utils.sexpr_format import dumps as kicad_dumps

            label_tags = {Symbol("label"), Symbol("global_label"), Symbol("hierarchical_label")}
            data = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
            px, py = float(position[0]), float(position[1])

            def near(x, y, tx, ty):
                return abs(x - tx) < tolerance and abs(y - ty) < tolerance

            def label_at(item):
                at = next(
                    (
                        p
                        for p in item[1:]
                        if isinstance(p, list) and len(p) >= 3 and p[0] == Symbol("at")
                    ),
                    None,
                )
                return (float(at[1]), float(at[2])) if at is not None else None

            # Far ends of bare stubs leaving the pin.
            wires = [
                (i, WireManager._parse_wire(item))
                for i, item in enumerate(data)
                if WireManager._parse_wire(item) is not None
            ]
            stub_ends: Dict[tuple, int] = {}
            for i, (a, b, _w, _t) in wires:
                if near(a[0], a[1], px, py):
                    far = b
                elif near(b[0], b[1], px, py):
                    far = a
                else:
                    continue
                others = sum(
                    1
                    for j, (c, d, _w2, _t2) in wires
                    if j != i and (near(c[0], c[1], *far) or near(d[0], d[1], *far))
                )
                if others == 0:
                    stub_ends[(round(far[0], 4), round(far[1], 4))] = i

            pin_positions = WireManager._collect_pin_positions(data)
            drop = set()
            for idx, item in enumerate(data):
                if not (isinstance(item, list) and item and item[0] in label_tags):
                    continue
                pos = label_at(item)
                if pos is None:
                    continue
                hit = near(pos[0], pos[1], px, py)
                if not hit:
                    stub = next(
                        (
                            wi
                            for (sx, sy), wi in stub_ends.items()
                            if near(pos[0], pos[1], sx, sy)
                            and not any(near(pos[0], pos[1], qx, qy) for qx, qy in pin_positions)
                        ),
                        None,
                    )
                    if stub is None:
                        continue
                    drop.add(stub)
                drop.add(idx)
                removed.append(
                    {
                        "type": str(item[0]),
                        "text": str(item[1]) if len(item) > 1 else "",
                        "position": {"x": pos[0], "y": pos[1]},
                    }
                )
            if drop:
                new_data = [item for i, item in enumerate(data) if i not in drop]
                WireManager.sync_junctions(new_data)
                Path(sch_path).write_text(kicad_dumps(new_data), encoding="utf-8")
        except Exception as e:
            logger.warning(f"replace cleanup failed: {e}")
        return removed

    @staticmethod
    def _offset_label_from_fields(sch_path, locator, ref, position, orientation, net_name):
        """Where to put a label on a two-pin part's pin so it clears Ref/Value text.

        The label normally sits on the pin. For two-pin parts (passives), when
        its text box would overlap a visible Reference/Value field of any
        symbol, it is pushed outward along the pin direction by 2.54 or
        5.08 mm (the caller draws the stub wire) — provided the new spot does
        not land on an existing connection. Returns ([x, y], note-or-None).
        """
        pin = [float(position[0]), float(position[1])]
        try:
            all_pins = locator.get_all_symbol_pins(sch_path, ref) or {}
            if len(all_pins) != 2:
                return pin, None
            geom = _sheet_geometry(sch_path)
            if not geom["fields"]:
                return pin, None

            def clear(x, y):
                box = _label_bbox(x, y, orientation, net_name)
                return not any(_boxes_overlap(box, f, margin=0.1) for f in geom["fields"])

            if clear(*pin):
                return pin, None
            d = _DIRS.get(int(orientation) % 360)
            if d is None:
                return pin, None
            for step in (2.54, 5.08):
                cand = [round(pin[0] + d[0] * step, 4), round(pin[1] + d[1] * step, 4)]
                if not clear(*cand):
                    continue
                if _route_conflict(geom, [pin, cand]) is not None:
                    continue
                if (round(cand[0], 3), round(cand[1], 3)) in geom["points"]:
                    continue
                return cand, f"label moved {step} mm out along the pin to clear a field"
        except Exception as e:
            logger.debug(f"label field-offset check skipped for {ref}: {e}")
        return pin, None

    @staticmethod
    def _pwr_flag_warnings(sch_path, locator, placed):
        """Warn about nets touched by this call that ERC will report as undriven.

        Only nets holding a ``power_in`` pin and no ``power_out`` pin (a
        PWR_FLAG or a regulator output), evaluated over the whole sheet
        hierarchy rather than the items of this call (#6). Plain signal nets
        whose names merely look like power (DRV_PWR_EN, VIN_SENSE) are never
        reported, nor are nets flagged on another sheet or in an earlier call.
        """
        from commands.schematic_connectivity import power_driver_warnings

        return power_driver_warnings(Path(sch_path), placed)

    def batch_add_and_connect(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Place multiple components and connect their pins in a single call."""
        logger.info("Batch add-and-connect: placing components and wiring nets")
        try:
            schematic_path = params.get("schematicPath")
            if not schematic_path:
                return {"success": False, "message": "schematicPath is required"}

            raw_components = params.get("components", [])
            if not raw_components:
                return {
                    "success": False,
                    "message": "components list is required and must be non-empty",
                }

            components_for_add = []
            nets_per_ref: Dict[str, Any] = {}
            for comp in raw_components:
                components_for_add.append({k: v for k, v in comp.items() if k != "nets"})
                if comp.get("nets"):
                    nets_per_ref[comp.get("reference", "")] = comp["nets"]

            add_params = {k: v for k, v in params.items() if k != "components"}
            add_params["components"] = components_for_add
            add_result = self.batch_add_components(add_params)

            connect_result: Dict[str, Any] = {
                "placed": [],
                "failed": [],
                "message": "no nets specified",
            }
            if nets_per_ref:
                connect_result = self.batch_connect(
                    {
                        "schematicPath": schematic_path,
                        "connections": nets_per_ref,
                        "labelType": params.get("labelType") or params.get("label_type") or "label",
                    }
                )

            add_errors = list(add_result.get("errors", []))
            if not add_errors and add_result.get("success") is False and add_result.get("message"):
                # batch_add_components failed as a whole (bad params, unreadable
                # sheet): surface its reason instead of a bare "0 placed".
                add_errors.append({"symbol": "*", "reference": "*", "error": add_result["message"]})
            conn_placed = connect_result.get("placed", [])
            conn_failed = connect_result.get("failed", [])

            # The MCP front-end shows only `message` when success is false, so
            # the per-component reasons must be in it, not just in `errors` (#3).
            message = (
                f"Placed {add_result.get('added_count', 0)} component(s)"
                f"{' (' + str(len(add_errors)) + ' failed)' if add_errors else ''}, "
                f"connected {len(conn_placed)} pin(s)"
                f"{' (' + str(len(conn_failed)) + ' failed)' if conn_failed else ''}"
            )
            if add_errors:
                message += f". Add failures: {_summarize_errors(add_errors)}"
            if conn_failed and add_result.get("added_count", 0) == 0:
                message += ". Connect failures: " + "; ".join(
                    f"{f.get('ref', '?')}/{f.get('pin', '?')}: {f.get('reason', '?')}"
                    for f in conn_failed[:5]
                )

            return {
                "success": add_result.get("added_count", 0) > 0,
                "message": message,
                "added_count": add_result.get("added_count", 0),
                "added": add_result.get("added", []),
                "connected_count": len(conn_placed),
                "connected": conn_placed,
                "placement_bbox": add_result.get("placement_bbox"),
                "errors": add_errors,
                "failed_connections": conn_failed,
                "warnings": connect_result.get("warnings", []),
            }

        except Exception as e:
            logger.error(f"Error in batch_add_and_connect: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    def update_symbol_from_library(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh lib_symbols cache entries from the symbol library."""
        from commands.update_symbol_from_library import update_symbol_from_library

        try:
            return update_symbol_from_library(params)
        except Exception as e:
            logger.error(f"Error in update_symbol_from_library: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}
