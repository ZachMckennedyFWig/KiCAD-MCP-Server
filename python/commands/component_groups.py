"""Group placed PCB footprints into a named KiCad group (``PCB_GROUP``).

A group is what Ctrl+G makes in the PCB editor: its members select, move and
rotate together, and it is stored in the .kicad_pcb as ``(group "name" ...)``.
"""

import logging
from typing import Any, Dict, List

import pcbnew

logger = logging.getLogger("kicad_interface")


def _group_name(group: Any) -> str:
    try:
        return str(group.GetName() or "")
    except Exception:
        return ""


def group_components(board: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Put the footprints named by ``references`` into the group ``groupName``.

    An existing group with that name is extended rather than duplicated. A
    footprint can be in only one group, so one that already belongs to another
    group is moved out of it (and reported). Nothing changes when any reference
    is missing from the board.
    """
    if board is None:
        return {
            "success": False,
            "message": "No board is loaded",
            "errorDetails": "Load or create a board first",
        }

    references = params.get("references")
    group_name = params.get("groupName")
    if isinstance(references, str):
        references = [r.strip() for r in references.split(",") if r.strip()]
    if not isinstance(references, list) or not references:
        return {
            "success": False,
            "message": "Missing references",
            "errorDetails": "references must be a non-empty list of reference designators",
        }
    if not isinstance(group_name, str) or not group_name.strip():
        return {
            "success": False,
            "message": "Missing groupName",
            "errorDetails": "groupName must be a non-empty string",
        }
    group_name = group_name.strip()

    footprints: List[Any] = []
    missing: List[str] = []
    seen = set()
    for ref in references:
        ref = str(ref)
        if ref in seen:
            continue
        seen.add(ref)
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            missing.append(ref)
        else:
            footprints.append(fp)
    if missing:
        return {
            "success": False,
            "message": f"Component(s) not found: {', '.join(missing)}",
            "errorDetails": "No group was created or changed",
            "missing": missing,
        }

    group = None
    for existing in board.Groups():
        if _group_name(existing) == group_name:
            group = existing
            break
    created = group is None
    if created:
        group = pcbnew.PCB_GROUP(board)
        group.SetName(group_name)
        board.Add(group)

    added: List[str] = []
    already: List[str] = []
    moved: List[Dict[str, str]] = []
    for fp in footprints:
        ref = fp.GetReference()
        parent = fp.GetParentGroup()
        if parent is not None:
            parent_name = _group_name(parent)
            if parent_name == group_name and group.ContainsItem(fp):
                already.append(ref)
                continue
            parent.RemoveItem(fp)
            moved.append({"reference": ref, "fromGroup": parent_name})
        group.AddItem(fp)
        added.append(ref)

    members = sorted(
        item.GetReference() for item in group.GetItems() if hasattr(item, "GetReference")
    )
    verb = "Created" if created else "Updated"
    result: Dict[str, Any] = {
        "success": True,
        "message": f"{verb} group '{group_name}' with {len(members)} footprint(s)",
        "group": {"name": group_name, "created": created, "members": members},
        "added": added,
    }
    if already:
        result["alreadyInGroup"] = already
    if moved:
        result["movedFromOtherGroup"] = moved
        result["warnings"] = [
            f"{m['reference']} was in group '{m['fromGroup']}' and was moved "
            f"(a footprint can belong to only one group)"
            for m in moved
        ]
    return result
