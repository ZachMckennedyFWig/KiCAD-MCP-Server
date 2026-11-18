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
from typing import Any, Dict, List, Tuple

import sexpdata
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
from commands.schematic_connectivity import LABEL_SCOPES, resolve_label_scope
from commands.schematic_text_utils import (
    _extract_property_position,
    _find_facing_label,
    _find_placed_symbol_block,
    _find_project_root,
    _move_property_in_block,
    _straight_ahead,
)
from commands.wire_manager import REPLACE_TOLERANCE_MM, WireManager
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


def _wire_end_key(pt) -> Tuple[int, int]:
    """A wire end in KiCad internal units (1 nm), as WireManager matches wire ends."""
    return round(float(pt[0]) * 10000), round(float(pt[1]) * 10000)


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
        scope ("local" / "global" / "auto") overrides labelType when given;
        "auto" picks a global label for a net whose name already appears on
        another sheet of the hierarchy (or as a global label on this one) and a
        local label otherwise, per net.
        Facing-label auto-wiring only applies to local labels; global labels join
        their net by name, so one is placed per pin with no inter-label wiring.
        A facing label must lie straight out in front of the pin, on its axis;
        the straight wire to it is still checked against the sheet and skipped
        (a label is placed instead) when it would land on or cross something.

        stubLength (mm) draws a wire out of each pin and puts the label at its
        end. Without a stub, avoidFieldOverlap (default true) moves a label on
        a two-pin part out along the pin, with a stub wire, when its text would
        overlap a visible Reference/Value field.

        Every pin is resolved first while the file is unchanged, then all
        removals (replace=True), labels and wires go into the sheet in one
        read-modify-write, so a failed write leaves the sheet as it was.
        """
        logger.info("Batch connect: placing net labels on multiple pins")
        try:
            schematic_path = params.get("schematicPath")
            connections = params.get("connections")
            replace = bool(params.get("replace", False))
            avoid_fields = bool(params.get("avoidFieldOverlap", True))
            # A label straight on the pin end sits over the pin number and name;
            # stubLength draws a short wire out of the pin first and puts the
            # label at its end, the way schematics are normally drawn.
            stub = float(params.get("stubLength") or 0.0)
            label_type = params.get("labelType") or params.get("label_type") or "label"
            if label_type not in ("label", "global_label"):
                return {
                    "success": False,
                    "message": (
                        f"invalid labelType '{label_type}'; expected 'label' or 'global_label'"
                    ),
                }
            scope = params.get("scope")
            if scope is not None and scope not in ("local", "global", "auto"):
                return {
                    "success": False,
                    "message": f"invalid scope '{scope}'; expected 'local', 'global' or 'auto'",
                }
            if scope in LABEL_SCOPES:
                label_type = LABEL_SCOPES[scope]

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
            # Planned edits, applied in one read-modify-write after the loop.
            # Resolving every pin first, while the file is unchanged, keeps the
            # PinLocator cache valid; writing per pin reparsed the whole sheet
            # two or three times per label (about 2.4 s per label on an
            # 11,000-line sheet), so a dozen labels ran past the bridge timeout.
            new_labels: List[tuple] = []  # (net, position, label_type, orientation)
            new_wires: List[tuple] = []  # (start, end)
            to_clear: List[tuple] = []  # (net, position) for replace=True

            # Sheet geometry for the auto-wire conflict check and the field
            # offset, read once (the file does not change until the write).
            geom_cache: Dict[str, Any] = {}

            def _geom() -> Dict[str, Any]:
                if "g" not in geom_cache:
                    geom_cache["g"] = _sheet_geometry(sch_path)
                    if geom_drop:
                        self._drop_from_geometry(geom_cache["g"], *geom_drop)
                return geom_cache["g"]

            def _facing_planned(net_name, position, orientation, proximity_mm=14.0):
                """A label planned earlier in this batch that faces *position*.

                Mirrors _find_facing_label for labels not yet in the file, so a
                pair of facing pins in one batch is still joined by a wire. The
                same _straight_ahead gate applies: a label off the pin's axis
                would get a diagonal wire.
                """
                facing = (int(round(orientation)) % 360 + 180) % 360
                px, py = float(position[0]), float(position[1])
                for net, pos, kind, angle in new_labels:
                    if kind != "label" or net != net_name or angle % 360 != facing:
                        continue
                    if ((pos[0] - px) ** 2 + (pos[1] - py) ** 2) ** 0.5 > proximity_mm:
                        continue
                    if _straight_ahead(position, orientation, pos):
                        return [pos[0], pos[1]]
                return None

            # With replace=True the old labels at this batch's pins (and stub ends)
            # are removed in the same write. _find_facing_label still reads the
            # unchanged file, so it could pick such a label as the wiring target
            # for another pin, and the write then renames it: re-netting R1.2 from
            # X to Y while R2.1 asks for X wired R2.1 onto Y. Collect every
            # position this batch clears first, and skip facing labels there.
            cleared_by_batch: List[Tuple[float, float]] = []
            # replace=True also removes the pin's old connection (#5f): every
            # label on the pin, of all three kinds, and a branch-free wire run
            # from the pin to a label -- an earlier run's stub of any length
            # (with its label), or an auto-wire to another pin's label of a
            # different net (that label stays). See _plan_replace_at.
            remove_wires: List[tuple] = []
            remove_labels: List[tuple] = []  # (kind, text, [x, y])
            replaced_at: Dict[tuple, List[Dict[str, Any]]] = {}
            kept_wires: List[tuple] = []  # (ref, pin, net, far end) of wires left alone
            geom_drop: List[Any] = []
            if replace:
                batch_pins: List[tuple] = []  # (ref, pin, net, [x, y])
                for c_ref, c_pins in connections.items():
                    if not isinstance(c_pins, dict):
                        continue
                    for c_pin, c_net in c_pins.items():
                        try:
                            c_pos = locator.get_pin_location(sch_path, c_ref, str(c_pin))
                            if not c_pos:
                                c_all = locator.get_all_symbol_pins(sch_path, c_ref) or {}
                                if len(c_all) != 1:
                                    continue
                                c_pin = next(iter(c_all))
                                c_pos = c_all[c_pin]
                            cx, cy = float(c_pos[0]), float(c_pos[1])
                            cleared_by_batch.append((cx, cy))
                            batch_pins.append((c_ref, str(c_pin), str(c_net), [cx, cy]))
                            if stub > 0:
                                c_angle = locator.get_pin_angle(sch_path, c_ref, str(c_pin)) or 0
                                c_orient = int(round(c_angle / 90) * 90) % 360
                                dx, dy = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[
                                    c_orient
                                ]
                                cleared_by_batch.append((cx + dx * stub, cy + dy * stub))
                        except Exception:
                            continue
                if batch_pins:
                    with open(sch_path, "r", encoding="utf-8") as sf:
                        sch_now = sexpdata.loads(sf.read())
                    index = self._replace_index(sch_now)
                    label_keys = set()
                    wire_keys = set()
                    for _c_ref, _c_pin, c_net, c_pos in batch_pins:
                        plan = self._plan_replace_at(index, c_pos, new_net=c_net)
                        for kind, text, pos in plan["labels"]:
                            k = (kind, text, round(pos[0], 4), round(pos[1], 4))
                            if k not in label_keys:
                                label_keys.add(k)
                                remove_labels.append((kind, text, [pos[0], pos[1]]))
                                cleared_by_batch.append((pos[0], pos[1]))
                        for a, b in plan["wires"]:
                            k = frozenset((_wire_end_key(a), _wire_end_key(b)))
                            if k not in wire_keys:
                                wire_keys.add(k)
                                remove_wires.append(([a[0], a[1]], [b[0], b[1]]))
                        if plan["replaced"]:
                            pk = (round(c_pos[0], 2), round(c_pos[1], 2))
                            replaced_at.setdefault(pk, []).extend(plan["replaced"])
                    # A direct pin-to-label wire the plan keeps (it branches or
                    # something touches it) leaves the pin on that label's net
                    # too: warn after the write when the names differ.
                    for w in WireManager.pin_label_wires(sch_now, [p[3] for p in batch_pins]):
                        k = frozenset((_wire_end_key(w["pin"]), _wire_end_key(w["far"])))
                        if k in wire_keys:
                            continue
                        owner = next(p for p in batch_pins if p[3] == w["pin"])
                        kept_wires.append((owner[0], owner[1], owner[2], w["far"]))
                    geom_drop = [remove_labels, remove_wires, index]

            def _cleared_here(pos) -> bool:
                # Same per-axis test and tolerance as the clear in
                # add_labels_and_wires, so the two always agree.
                px, py = float(pos[0]), float(pos[1])
                return any(
                    abs(px - cx) < REPLACE_TOLERANCE_MM and abs(py - cy) < REPLACE_TOLERANCE_MM
                    for cx, cy in cleared_by_batch
                )

            # scope=auto: one label kind per net, decided from the other sheets
            # before this call adds anything.
            auto_scope: Dict[str, Dict[str, Any]] = {}
            if scope == "auto":
                auto_scope = resolve_label_scope(
                    sch_path,
                    [
                        str(net)
                        for pin_map in connections.values()
                        if isinstance(pin_map, dict)
                        for net in pin_map.values()
                    ],
                )

            for ref, pin_map in connections.items():
                if not isinstance(pin_map, dict):
                    failed.append({"ref": ref, "reason": "pin_map must be a dict {pin: netName}"})
                    continue
                for pin_id, net_name in pin_map.items():
                    try:
                        pin_label_type = (
                            auto_scope[str(net_name)]["labelType"]
                            if str(net_name) in auto_scope
                            else label_type
                        )
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
                        entry: Dict[str, Any] = {
                            "ref": ref,
                            "pin": str(pin_id),
                            "net": net_name,
                            "position": {"x": position[0], "y": position[1]},
                        }
                        if placed_positions.get(pos_key) == net_name:
                            entry["note"] = (
                                "deduped: label already placed at this coordinate for this net"
                            )
                            placed.append(entry)
                            continue
                        if replaced_at.get(pos_key):
                            entry["replaced"] = replaced_at[pos_key]
                        if scope == "auto":
                            entry["labelType"] = pin_label_type

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

                        dx, dy = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}[orientation]
                        stub_end = [
                            round(float(position[0]) + dx * stub, 4),
                            round(float(position[1]) + dy * stub, 4),
                        ]
                        if replace:
                            # Every pin of the batch, whichever branch it takes
                            # below: a pin wired to another pin's label used to
                            # keep its own old label, joining the two nets. This
                            # is also what cleared_by_batch assumed.
                            to_clear.append((net_name, list(position)))
                            if stub > 0:
                                # An earlier stub run left this net's label at
                                # the stub end; without this a re-run stacks a
                                # second label there.
                                to_clear.append((net_name, list(stub_end)))

                        # Facing-label auto-wiring is only meaningful for local labels;
                        # global labels join their net by name (one placed per pin).
                        existing_pos = None
                        if pin_label_type == "label":
                            existing_pos = _find_facing_label(
                                sch_path, net_name, position, orientation
                            )
                            if existing_pos and _cleared_here(existing_pos):
                                existing_pos = None  # replaced by this batch: not a target
                            existing_pos = existing_pos or _facing_planned(
                                net_name, position, orientation
                            )
                        route = None
                        route_note = None
                        if existing_pos:
                            # On the pin's axis, so this is one straight segment;
                            # still keep it off pins, wires and symbol bodies.
                            route = _orthogonal_route(position, orientation, existing_pos)
                            if route is None:
                                route_note = "facing label not reachable orthogonally"
                            else:
                                conflict = _route_conflict(_geom(), route)
                                if conflict:
                                    route_note = f"auto-wire skipped: {conflict}"
                                    route = None
                        if route:
                            for a, b in zip(route, route[1:]):
                                new_wires.append((list(a), list(b)))
                            entry["note"] = (
                                f"wired to existing label at ({existing_pos[0]},{existing_pos[1]})"
                            )
                            entry["wire"] = [{"x": p[0], "y": p[1]} for p in route]
                        else:
                            label_at = list(position)
                            offset_note = None
                            if stub > 0:
                                # An explicit stub wins over the field offset.
                                label_at = stub_end
                            elif avoid_fields:
                                label_at, offset_note = self._offset_label_from_fields(
                                    sch_path,
                                    locator,
                                    ref,
                                    position,
                                    orientation,
                                    net_name,
                                    geom=_geom,
                                )
                            if label_at != list(position):
                                new_wires.append((list(position), label_at))
                                entry["label_position"] = {"x": label_at[0], "y": label_at[1]}
                            notes = [n for n in (route_note, offset_note) if n]
                            if notes:
                                entry["note"] = "; ".join(notes)
                            new_labels.append((net_name, label_at, pin_label_type, orientation))
                        placed.append(entry)
                        placed_positions[pos_key] = net_name
                    except Exception as pin_err:
                        failed.append({"ref": ref, "pin": str(pin_id), "reason": str(pin_err)})

            # Old labels and wires (replace=True) are removed in the same write
            # that adds the new ones: a failed write then leaves the sheet as it
            # was, instead of with the old labels gone and nothing in their place.
            write_kwargs: Dict[str, Any] = {
                "clear": to_clear,
                "tolerance": REPLACE_TOLERANCE_MM,
                "remove_wires": remove_wires,
            }
            if remove_labels:
                write_kwargs["remove_labels"] = remove_labels
            if not WireManager.add_labels_and_wires(
                sch_path, new_labels, new_wires, **write_kwargs
            ):
                failed.extend(dict(e, reason="writing the schematic failed") for e in placed)
                placed = []

            result: Dict[str, Any] = {
                "success": len(failed) == 0,
                "message": f"Placed {len(placed)} label(s), {len(failed)} failed",
                "placed": placed,
                "failed": failed,
            }
            warnings = self._kept_wire_warnings(sch_path, kept_wires) if placed else []
            warnings += self._pwr_flag_warnings(sch_path, locator, placed)
            used_nets = {str(row.get("net")) for row in placed}
            for net in sorted(used_nets & set(auto_scope)):
                local_only = auto_scope[net]["localOnly"]
                if local_only:
                    warnings.append(
                        f"scope=auto placed a global label for '{net}', but "
                        + ", ".join(Path(f).name for f in local_only)
                        + " use(s) only local/hierarchical labels of that name, which a "
                        "global label does not join. Convert those to global labels "
                        "(batch_connect replace=true with scope='global') to merge the nets."
                    )
            if warnings:
                result["warnings"] = warnings
            return result

        except Exception as e:
            logger.error(f"Error in batch_connect: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return {"success": False, "message": str(e)}

    @staticmethod
    def _replace_index(data: list) -> Dict[str, Any]:
        """Wires, labels, junctions and pin positions of a parsed sheet, for _plan_replace_at."""
        from sexpdata import Symbol

        label_tags = {Symbol("label"), Symbol("global_label"), Symbol("hierarchical_label")}

        def at_of(item):
            at = next(
                (
                    p
                    for p in item[1:]
                    if isinstance(p, list) and len(p) >= 3 and p[0] == Symbol("at")
                ),
                None,
            )
            return (float(at[1]), float(at[2])) if at is not None else None

        wires = []  # (index, (a, b, width, type))
        labels = []  # (index, kind, text, (x, y))
        junctions = []
        for idx, item in enumerate(data):
            if not (isinstance(item, list) and item):
                continue
            parsed = WireManager._parse_wire(item)
            if parsed is not None:
                wires.append((idx, parsed))
            elif item[0] in label_tags:
                pos = at_of(item)
                if pos is not None:
                    labels.append((idx, str(item[0]), str(item[1]) if len(item) > 1 else "", pos))
            elif item[0] == Symbol("junction"):
                pos = at_of(item)
                if pos is not None:
                    junctions.append(pos)
        return {
            "wires": wires,
            "labels": labels,
            "junctions": junctions,
            "pins": WireManager._collect_pin_positions(data),
        }

    @staticmethod
    def _plan_replace_at(index, position, new_net=None, tolerance=REPLACE_TOLERANCE_MM):
        """What replace=True removes for the pin at *position* (nothing is written here).

        * Every label on the pin, of all three kinds -- ``label``,
          ``global_label`` and ``hierarchical_label`` (#5f: only local labels
          used to go, so an old global label stayed on the pin and merged two
          nets).
        * Wire chains leaving the pin that end at a label: a chain is a run of
          wire segments with nothing else attached anywhere along it (no
          branch, junction, pin or label before its far end). When the far end
          is the pin's own label (not on a pin, no other wire there) -- an
          earlier stub of any length, or a bare stub -- the chain and that
          label go. When it reaches another pin's label -- the wire
          batch_connect draws to a facing label -- or a label another wire
          also uses, only the chain goes (the label stays), unless that label
          already names *new_net*, in which case the wire is still right and
          stays.

        *index* is _replace_index of the sheet as it is before the batch.
        Returns {"labels": [(kind, text, (x, y))], "wires": [((x1, y1), (x2, y2))],
        "replaced": [...]} where "replaced" lists the removed labels as
        {type, text, position} followed by the removed wire chains as
        {type: "wire", to: label text, points: [...]}.
        """
        wires = index["wires"]
        labels = index["labels"]
        junctions = index["junctions"]
        pin_positions = index["pins"]
        px, py = float(position[0]), float(position[1])
        out_labels: List[tuple] = []
        out_wires: List[tuple] = []
        removed: List[Dict[str, Any]] = []
        removed_wires: List[Dict[str, Any]] = []

        def near(x, y, tx, ty):
            return abs(x - tx) < tolerance and abs(y - ty) < tolerance

        def on_interior(pt, a, b, eps=1e-3):
            """pt strictly inside segment a-b (not at either end)."""
            if near(pt[0], pt[1], *a) or near(pt[0], pt[1], *b):
                return False
            cross = (b[0] - a[0]) * (pt[1] - a[1]) - (b[1] - a[1]) * (pt[0] - a[0])
            if abs(cross) > eps * max(1.0, abs(b[0] - a[0]) + abs(b[1] - a[1])):
                return False
            return (
                min(a[0], b[0]) - eps <= pt[0] <= max(a[0], b[0]) + eps
                and min(a[1], b[1]) - eps <= pt[1] <= max(a[1], b[1]) + eps
            )

        def labels_near(pt):
            return [lab for lab in labels if near(lab[3][0], lab[3][1], *pt)]

        def chain_from(first):
            """Follow a branch-free wire run from the pin through *first*.

            Returns (wire indices, far end, labels at the far end, is_own, points)
            or None when the run branches, touches anything on the way, or ends
            somewhere that is not a label.
            """
            i, (a, b, _w, _t) = first
            cur = b if near(a[0], a[1], px, py) else a
            chain = [i]
            points = [(px, py), cur]
            while True:
                if any(near(cur[0], cur[1], jx, jy) for jx, jy in junctions):
                    return None
                at_pin = any(near(cur[0], cur[1], qx, qy) for qx, qy in pin_positions)
                here = labels_near(cur)
                attached = [
                    (j, seg)
                    for j, seg in wires
                    if j not in chain
                    and (near(seg[0][0], seg[0][1], *cur) or near(seg[1][0], seg[1][1], *cur))
                ]
                if here or at_pin:
                    if not here:
                        return None  # a bare pin-to-pin wire is not ours to judge
                    own = not at_pin and not attached
                    return chain, cur, here, own, points
                if len(attached) != 1:
                    return None  # dead end or branch
                j, (c, d, _w2, _t2) = attached[0]
                chain.append(j)
                cur = d if near(c[0], c[1], *cur) else c
                points.append(cur)

        dropped_labels = set()
        dropped_wires = set()

        def drop_label(idx, kind, text, pos):
            if idx in dropped_labels:
                return
            dropped_labels.add(idx)
            out_labels.append((kind, text, pos))
            removed.append({"type": kind, "text": text, "position": {"x": pos[0], "y": pos[1]}})

        # 1. Labels on the pin itself.
        for idx, kind, text, pos in labels:
            if near(pos[0], pos[1], px, py):
                drop_label(idx, kind, text, pos)

        # 2. Wire chains from the pin to a label.
        for first in wires:
            _i, (a, b, _w, _t) = first
            if not (near(a[0], a[1], px, py) or near(b[0], b[1], px, py)):
                continue
            if first[0] in dropped_wires:
                continue
            found = chain_from(first)
            if found is None:
                continue
            chain, _end, end_labels, own, points = found
            # Nothing else may touch the run between its two ends.
            segs = [next(seg for j, seg in wires if j == k) for k in chain]
            others = [seg for j, seg in wires if j not in chain]
            touch_pts = (
                [lab[3] for lab in labels]
                + list(pin_positions)
                + junctions
                + [p for s in others for p in (s[0], s[1])]
            )
            if any(on_interior(pt, s[0], s[1]) for s in segs for pt in touch_pts):
                continue
            if any(on_interior(pt, s[0], s[1]) for pt in points for s in others):
                continue
            if own:
                for idx, kind, text, pos in end_labels:
                    drop_label(idx, kind, text, pos)
            elif new_net is not None and any(text == new_net for _i2, _k, text, _p in end_labels):
                continue  # already wired to the requested net: keep it
            for k, seg in zip(chain, segs):
                if k not in dropped_wires:
                    dropped_wires.add(k)
                    out_wires.append((seg[0], seg[1]))
            if not own:
                removed_wires.append(
                    {
                        "type": "wire",
                        "to": end_labels[0][2],
                        "points": [{"x": x, "y": y} for x, y in points],
                    }
                )
        return {"labels": out_labels, "wires": out_wires, "replaced": removed + removed_wires}

    @staticmethod
    def _drop_from_geometry(geom, remove_labels, remove_wires, index) -> None:
        """Take what replace=True removes out of a _sheet_geometry read before the write.

        Otherwise an old label or stub the batch deletes would still block the
        auto-wire or the field offset that takes its place.
        """

        def key(pt):
            return (round(float(pt[0]), 3), round(float(pt[1]), 3))

        gone = {frozenset((key(a), key(b))) for a, b in remove_wires}
        geom["wires"] = [w for w in geom["wires"] if frozenset((key(w[0]), key(w[1]))) not in gone]
        still = {key(p) for w in geom["wires"] for p in w}
        still |= {key(p) for p in index["pins"]}
        removed_label_keys = {(kind, text, key(pos)) for kind, text, pos in remove_labels}
        still |= {
            key(pos)
            for _i, kind, text, pos in index["labels"]
            if (kind, text, key(pos)) not in removed_label_keys
        }
        candidates = {key(pos) for _k, _t, pos in remove_labels}
        candidates |= {key(p) for w in remove_wires for p in w}
        geom["points"] -= candidates - still

    @staticmethod
    def _offset_label_from_fields(
        sch_path, locator, ref, position, orientation, net_name, geom=None
    ):
        """Where to put a label on a two-pin part's pin so it clears Ref/Value text.

        The label normally sits on the pin. For two-pin parts (passives), when
        its text box would overlap a visible Reference/Value field of any
        symbol, it is pushed outward along the pin direction by 2.54 or
        5.08 mm (the caller draws the stub wire) — provided the new spot does
        not land on an existing connection. *geom* is a _sheet_geometry dict
        or a callable returning one (read from *sch_path* when omitted).
        Returns ([x, y], note-or-None).
        """
        pin = [float(position[0]), float(position[1])]
        try:
            all_pins = locator.get_all_symbol_pins(sch_path, ref) or {}
            if len(all_pins) != 2:
                return pin, None
            if callable(geom):
                geom = geom()
            if geom is None:
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
    def _kept_wire_warnings(sch_path, kept_wires) -> List[str]:
        """Warn when replace=True left a pin wired to a label of another net.

        replace keeps a wire from the pin to a label when the run is not a
        plain stub (it branches, or something else touches it). If the label
        there after the write names a different net, the pin is on both nets;
        say so instead of leaving it silent.
        """
        if not kept_wires:
            return []
        out: List[str] = []
        try:
            with open(sch_path, "r", encoding="utf-8") as f:
                data = sexpdata.loads(f.read())
            names: Dict[tuple, List[str]] = {}
            kinds = ("label", "global_label", "hierarchical_label")
            for item in data:
                if not (isinstance(item, list) and len(item) > 1 and str(item[0]) in kinds):
                    continue
                at = next((p for p in item[1:] if isinstance(p, list) and str(p[0]) == "at"), None)
                if at is not None:
                    key = (round(float(at[1]) * 10000), round(float(at[2]) * 10000))
                    names.setdefault(key, []).append(str(item[1]))
            for ref, pin, net, far in kept_wires:
                key = (round(far[0] * 10000), round(far[1] * 10000))
                others = sorted({n for n in names.get(key, []) if n != net})
                if others:
                    out.append(
                        f"{ref}.{pin}: replace kept the wire from this pin to "
                        f"({far[0]}, {far[1]}), where label {', '.join(others)} stays, "
                        f"so the pin is on {net} and {', '.join(others)}. Delete that wire "
                        "if the pin should be on only one net."
                    )
        except Exception as e:
            logger.warning(f"kept-wire check failed: {e}")
        return out

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
                connect_params = {
                    "schematicPath": schematic_path,
                    "connections": nets_per_ref,
                    "labelType": params.get("labelType") or params.get("label_type") or "label",
                    "stubLength": params.get("stubLength") or 0,
                }
                for key in ("scope", "avoidFieldOverlap"):
                    if params.get(key) is not None:
                        connect_params[key] = params.get(key)
                connect_result = self.batch_connect(connect_params)

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
