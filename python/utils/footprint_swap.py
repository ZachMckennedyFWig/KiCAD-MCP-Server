"""Load library footprints safely and swap a placed footprint for another one.

Shared by ``replace_component``, ``edit_component`` (footprint change) and
``sync_schematic_to_board`` (footprint changed in the schematic), so all three
keep the same things across a swap: reference, value, position, rotation,
board side, the schematic link (``Path``), attributes, lock state, custom
fields, and every pad's net (matched by pad number). This mirrors KiCad's own
``PCB_EDIT_FRAME::ExchangeFootprint()``.

KiCad 10 SWIG traps handled here (see MCP_ISSUES #5r):

* ``pcbnew.FootprintLoad`` can be missing or return a bare ``SwigPyObject``
  after the SWIG type registry is damaged. ``PCB_IO_KICAD_SEXPR().FootprintLoad``
  is tried as a fallback, and every result is checked with
  ``isinstance(x, pcbnew.FOOTPRINT)`` so a broken proxy fails with a clear
  message instead of an ``AttributeError`` deep inside the swap.
* ``FOOTPRINT.GetFPID()`` can return an unusable ``SwigPyObject``;
  ``GetFPIDAsString()`` is used to read the lib id instead.
* ``FOOTPRINT.Flip`` takes a ``FLIP_DIRECTION`` in KiCad 10 and a bool in
  KiCad 8/9. The swap flips first and sets the orientation afterwards, so the
  flip axis does not change the result.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

# Fields every footprint has; their values are carried over explicitly.
_MANDATORY_FIELDS = {"Reference", "Value", "Footprint", "Datasheet", "Description"}


class FootprintLoadError(Exception):
    """A library footprint could not be loaded as a real ``pcbnew.FOOTPRINT``."""


def _is_footprint(pcbnew_mod: Any, obj: Any) -> bool:
    footprint_type = getattr(pcbnew_mod, "FOOTPRINT", None)
    if isinstance(footprint_type, type):
        return isinstance(obj, footprint_type)
    # Stubbed pcbnew (unit tests): fall back to duck typing.
    return obj is not None and hasattr(obj, "Pads") and hasattr(obj, "SetReference")


def load_library_footprint(library_path: str, fp_name: str) -> Any:
    """Load ``fp_name`` from the ``.pretty`` directory ``library_path``.

    Returns a ``pcbnew.FOOTPRINT``. Raises ``FootprintLoadError`` with the
    reason when no loader yields a real footprint.
    """
    import pcbnew

    attempts: List[str] = []
    loaders = []
    if hasattr(pcbnew, "FootprintLoad"):
        loaders.append(
            ("pcbnew.FootprintLoad", lambda: pcbnew.FootprintLoad(library_path, fp_name))
        )
    if hasattr(pcbnew, "PCB_IO_KICAD_SEXPR"):
        loaders.append(
            (
                "PCB_IO_KICAD_SEXPR().FootprintLoad",
                lambda: pcbnew.PCB_IO_KICAD_SEXPR().FootprintLoad(library_path, fp_name),
            )
        )
    for label, loader in loaders:
        try:
            fp = loader()
        except Exception as exc:  # try the next loader
            attempts.append(f"{label} raised {exc}")
            continue
        if fp is None:
            attempts.append(f"{label} returned nothing")
            continue
        if not _is_footprint(pcbnew, fp):
            cast = getattr(pcbnew, "Cast_to_FOOTPRINT", None)
            if cast is not None:
                try:
                    fp = cast(fp)
                except Exception:
                    pass
        if _is_footprint(pcbnew, fp):
            return fp
        attempts.append(
            f"{label} returned {type(fp).__name__}, not a pcbnew.FOOTPRINT "
            "(SWIG proxy without FOOTPRINT methods; restart the MCP server if this persists)"
        )
    raise FootprintLoadError(
        f"could not load footprint '{fp_name}' from {library_path}: "
        + ("; ".join(attempts) if attempts else "no footprint loader available in pcbnew")
    )


def footprint_lib_id(fp: Any) -> str:
    """``Library:Name`` of a placed footprint ("" when unreadable)."""
    try:
        value = fp.GetFPIDAsString()
        if isinstance(value, str):
            return value
    except Exception:
        pass
    try:
        fpid = fp.GetFPID()
        lib = fpid.GetLibNickname().GetUTF8() if hasattr(fpid, "GetLibNickname") else ""
        name = fpid.GetLibItemName().GetUTF8() if hasattr(fpid, "GetLibItemName") else ""
        return f"{lib}:{name}" if lib else name
    except Exception:
        return ""


def _net_name(net: Any) -> str:
    try:
        return str(net.GetNetname())
    except Exception:
        return str(net)


def _copy_custom_fields(old_fp: Any, new_fp: Any) -> List[str]:
    """Copy user fields (LCSC, MPN, ...) from ``old_fp``; keep their visibility."""
    copied: List[str] = []
    try:
        fields = list(old_fp.GetFields())
    except Exception:
        return copied
    for field in fields:
        try:
            name = field.GetName()
            if name in _MANDATORY_FIELDS:
                continue
            mandatory = getattr(field, "IsMandatory", None)
            if callable(mandatory) and mandatory():
                continue
            new_fp.SetField(name, field.GetText())
            new_field = new_fp.GetField(name)
            if new_field is not None and hasattr(new_field, "SetVisible"):
                new_field.SetVisible(bool(field.IsVisible()))
            copied.append(name)
        except Exception as exc:
            logger.debug(f"swap_footprint: field copy skipped: {exc}")
    return copied


def swap_footprint(
    board: Any,
    old_fp: Any,
    new_fp: Any,
    lib_name: str,
    fp_name: str,
    value: Optional[str] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """Replace ``old_fp`` on ``board`` with ``new_fp`` (a freshly loaded library footprint).

    Keeps reference, value (unless ``value`` is given), position, rotation,
    side, schematic path, attributes, lock state, custom fields and pad nets
    by pad number. ``old_fp`` is deleted from the board. Returns
    ``(new_fp, report)``.
    """
    import pcbnew

    from utils.board_items import delete_board_item

    if not _is_footprint(pcbnew, new_fp):
        raise FootprintLoadError(
            f"replacement for {lib_name}:{fp_name} is {type(new_fp).__name__}, "
            "not a pcbnew.FOOTPRINT"
        )

    reference = old_fp.GetReference()
    old_id = footprint_lib_id(old_fp)
    position = old_fp.GetPosition()
    orientation = old_fp.GetOrientation()
    was_flipped = bool(old_fp.IsFlipped())

    new_fp.SetReference(reference)
    new_fp.SetValue(value if value else old_fp.GetValue())
    new_fp.SetFPID(pcbnew.LIB_ID(lib_name, fp_name))
    for getter, setter in (
        ("GetPath", "SetPath"),
        ("GetAttributes", "SetAttributes"),
        ("IsLocked", "SetLocked"),
        ("IsDNP", "SetDNP"),
        ("GetSheetname", "SetSheetname"),
        ("GetSheetfile", "SetSheetfile"),
    ):
        try:
            if hasattr(old_fp, getter) and hasattr(new_fp, setter):
                getattr(new_fp, setter)(getattr(old_fp, getter)())
        except Exception as exc:
            logger.debug(f"swap_footprint: {setter} skipped: {exc}")
    try:
        datasheet = old_fp.GetFieldText("Datasheet")
        if datasheet:
            new_fp.SetField("Datasheet", datasheet)
    except Exception:
        pass
    fields_copied = _copy_custom_fields(old_fp, new_fp)

    # Pad nets by pad number. Several old pads can share a number (e.g. an
    # exposed pad split in pieces); the first net seen wins.
    old_nets: Dict[str, Any] = {}
    for pad in old_fp.Pads():
        number = str(pad.GetNumber())
        try:
            code = pad.GetNetCode()
        except Exception:
            code = None
        if isinstance(code, int) and code <= 0:
            continue  # unconnected pad: nothing to carry over
        if number not in old_nets:
            old_nets[number] = pad.GetNet()
    new_numbers = set()
    nets_kept = 0
    for pad in new_fp.Pads():
        number = str(pad.GetNumber())
        new_numbers.add(number)
        net = old_nets.get(number)
        if net is not None:
            pad.SetNet(net)
            nets_kept += 1
    dropped = sorted(
        f"{number} ({_net_name(net)})"
        for number, net in old_nets.items()
        if number not in new_numbers
    )

    new_fp.SetPosition(position)
    board.Add(new_fp)
    if was_flipped and not new_fp.IsFlipped():
        direction = getattr(pcbnew, "FLIP_DIRECTION_TOP_BOTTOM", False)
        new_fp.Flip(new_fp.GetPosition(), direction)
    # Set the rotation after the flip: the flip axis would otherwise change it.
    # Flipping about the footprint's own anchor leaves the position unchanged.
    new_fp.SetOrientation(orientation)
    delete_board_item(board, old_fp)

    report: Dict[str, Any] = {
        "reference": reference,
        "oldFootprint": old_id,
        "newFootprint": f"{lib_name}:{fp_name}",
        "padNetsKept": nets_kept,
        "side": "back" if was_flipped else "front",
    }
    if fields_copied:
        report["fieldsCopied"] = fields_copied
    if dropped:
        report["droppedPadNets"] = dropped
        report["warning"] = (
            f"{reference}: the new footprint has no pad(s) {', '.join(dropped)}; "
            "those connections were dropped"
        )
    return new_fp, report
