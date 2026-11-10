"""Netlist -> board synchronisation helpers for ``sync_schematic_to_board`` (B1).

The schematic side is read from the KiCad XML netlist ``kicad-cli sch export
netlist --format kicadxml`` writes, which is what KiCad's own "Update PCB from
Schematic" (F8) works from. These helpers parse it and compare/apply the parts
F8 owns on each footprint: Value, Datasheet/Description and custom fields, the
DNP and exclude-from-BOM flags, the schematic link (path) and every pad's net.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

# Fields a footprint carries that the sync must not copy as plain fields:
# Reference/Value have their own setters, Footprint is the lib id itself.
_NOT_COPIED = {"Reference", "Value", "Footprint"}


def escape_generated_net_name(name: str) -> str:
    """KiCad's board name for a netlist net.

    The netlist writes generated single-pin nets as ``unconnected-(U15-GPIO/MFP5-Pad32)``
    while the board stores ``unconnected-(U15-GPIO{slash}MFP5-Pad32)``: a ``/``
    from the pin name is escaped so it is not read as a sheet path.
    """
    if name.startswith("unconnected-("):
        return name.replace("/", "{slash}")
    return name


def parse_kicad_netlist(xml_text: str) -> Dict[str, Any]:
    """Components and pin->net map from a KiCad XML netlist.

    Returns ``{"components": [...], "pin_net": {(ref, pin): net}}``. Each
    component: reference, value, footprint, uuid (symbol UUID), path (the
    footprint's schematic link ``/sheet-uuids/symbol-uuid``), fields (every
    ``<field>`` except Reference/Value/Footprint), dnp, exclude_from_bom,
    exclude_from_board.
    """
    root = ET.fromstring(xml_text)
    components: List[Dict[str, Any]] = []
    for comp in root.findall("./components/comp"):
        # <tstamps> carries the schematic symbol UUID -- verified against
        # kicad-cli 10.0: a bare UUID with no sheet path (instances of a reused
        # sheet share it). Legacy exports could comma-join several; keep the first.
        tstamps = (comp.findtext("tstamps") or "").replace(",", " ").split()
        uuid = tstamps[0] if tstamps else ""
        sheetpath = comp.find("sheetpath")
        sheet_stamps = sheetpath.get("tstamps", "/") if sheetpath is not None else "/"
        if not sheet_stamps.endswith("/"):
            sheet_stamps += "/"
        fields: Dict[str, str] = {}
        for field in comp.findall("./fields/field"):
            name = field.get("name")
            if name and name not in _NOT_COPIED:
                fields[name] = field.text or ""
        props = {p.get("name") for p in comp.findall("property")}
        components.append(
            {
                "reference": comp.get("ref", ""),
                "value": comp.findtext("value", ""),
                "footprint": comp.findtext("footprint", ""),
                "uuid": uuid,
                "path": (sheet_stamps + uuid) if uuid else "",
                "fields": fields,
                "dnp": "dnp" in props,
                "exclude_from_bom": "exclude_from_bom" in props,
                "exclude_from_board": "exclude_from_board" in props,
            }
        )
    pin_net: Dict[Tuple[str, str], str] = {}
    for net in root.findall("./nets/net"):
        name = escape_generated_net_name(net.get("name", ""))
        for node in net.findall("node"):
            pin_net[(node.get("ref", ""), node.get("pin", ""))] = name
    return {"components": components, "pin_net": pin_net}


def _blank(text: Optional[str]) -> str:
    """Normalise an empty field: KiCad writes "~" for an unset Datasheet."""
    text = "" if text is None else str(text)
    return "" if text == "~" else text


def _field_text(fp: Any, name: str) -> Optional[str]:
    try:
        if not fp.HasField(name):
            return None
        return str(fp.GetFieldText(name))
    except Exception:
        return None


def sync_footprint_fields(fp: Any, comp: Dict[str, Any], apply: bool) -> List[Dict[str, Any]]:
    """Compare (and with *apply*, update) what F8 owns on one footprint.

    Covers Value, every netlist field except Reference/Value/Footprint
    (Datasheet, Description and custom fields such as LCSC), DNP,
    exclude-from-BOM and the schematic link. A field the footprint lacks is
    created hidden on the Fab layer of its side, like F8. Fields only the
    footprint has are left alone. Returns the changes as
    ``[{"what", "from", "to"}]``.
    """
    import pcbnew

    changes: List[Dict[str, Any]] = []

    value = comp.get("value")
    if value is not None:
        old = str(fp.GetValue())
        if old != value:
            changes.append({"what": "Value", "from": old, "to": value})
            if apply:
                fp.SetValue(value)

    for name, text in (comp.get("fields") or {}).items():
        old_text = _field_text(fp, name)
        if old_text is None and _blank(text) == "":
            continue  # do not create empty fields
        if old_text is not None and _blank(old_text) == _blank(text):
            continue
        changes.append({"what": name, "from": old_text, "to": text})
        if apply:
            fp.SetField(name, text)
            if old_text is None:
                try:
                    field = fp.GetField(name)
                    field.SetVisible(False)
                    back = fp.GetLayer() == pcbnew.B_Cu
                    field.SetLayer(pcbnew.B_Fab if back else pcbnew.F_Fab)
                except Exception as e:
                    logger.debug(f"could not hide new field {name}: {e}")

    for what, getter, setter, key in (
        ("DNP", "IsDNP", "SetDNP", "dnp"),
        ("ExcludeFromBOM", "IsExcludedFromBOM", "SetExcludedFromBOM", "exclude_from_bom"),
    ):
        want = bool(comp.get(key, False))
        try:
            have = bool(getattr(fp, getter)())
        except Exception:
            continue
        if have != want:
            changes.append({"what": what, "from": have, "to": want})
            if apply:
                getattr(fp, setter)(want)

    want_path = comp.get("path") or ""
    if want_path:
        try:
            have_path = str(fp.GetPath().AsString())
        except Exception:
            have_path = None
        if have_path is not None and have_path != want_path:
            changes.append({"what": "SchematicLink", "from": have_path, "to": want_path})
            if apply:
                fp.SetPath(pcbnew.KIID_PATH(want_path))
    return changes
