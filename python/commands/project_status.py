"""``project_status``: a ~10-line re-orientation report (MCP_ISSUES I2).

After a context compaction the cheapest way back is one call that answers
"where are we?": how big the design is, whether board and schematic agree,
what the last DRC/ERC said, what is unsaved, and when the last snapshot was
taken. Everything here is read from disk or from state the server already
holds -- nothing is re-run (DRC/ERC counts come from the history that
``run_drc``/``run_erc`` record, see ``utils.check_summary``).
"""

import glob
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from utils import check_summary
from utils.sexpr_format import iter_child_offsets, match_paren, unescape_sexpr_string
from utils.sheet_tree import sheet_tree

logger = logging.getLogger("kicad_interface")

_QUOTED = r'"((?:[^"\\]|\\.)*)"'
_SYMBOL_HEAD = re.compile(r"\(symbol\s*\(lib_id\s+" + _QUOTED)
_FOOTPRINT_HEAD = re.compile(r"\(footprint\s+" + _QUOTED)
_REFERENCE_PROP = re.compile(r'\(property\s+"Reference"\s+' + _QUOTED)
_FP_TEXT_REFERENCE = re.compile(r"\(fp_text\s+reference\s+" + _QUOTED)
_INSTANCE_REFERENCE = re.compile(r"\(reference\s+" + _QUOTED + r"\s*\)")
_ON_BOARD_NO = re.compile(r"\(on_board\s+no\)")
_DNP_YES = re.compile(r"\(dnp\s+yes\)")
_ATTR = re.compile(r"\(attr\b([^()]*)\)")
_LIST_CAP = 10


# ---------------------------------------------------------------------------
# Locating the project
# ---------------------------------------------------------------------------


def resolve_project_files(
    project_path: Optional[str], board_path: Optional[str]
) -> Dict[str, Optional[str]]:
    """Find the .kicad_pro, root .kicad_sch and .kicad_pcb of a project.

    ``project_path`` may be a .kicad_pro/.kicad_sch/.kicad_pcb file or a
    directory; otherwise the loaded board's path is used.
    """
    pro = sch = pcb = None
    anchor = project_path or board_path
    if anchor:
        anchor = os.path.abspath(os.path.expanduser(str(anchor)))
        if os.path.isdir(anchor):
            pros = sorted(glob.glob(os.path.join(anchor, "*.kicad_pro")))
            if len(pros) == 1:
                pro = pros[0]
            elif len(pros) > 1:
                raise ValueError(f"{anchor} holds {len(pros)} projects; pass the .kicad_pro to use")
            else:
                schs = sorted(glob.glob(os.path.join(anchor, "*.kicad_sch")))
                pcbs = sorted(glob.glob(os.path.join(anchor, "*.kicad_pcb")))
                sch = schs[0] if len(schs) == 1 else None
                pcb = pcbs[0] if len(pcbs) == 1 else None
        else:
            stem, ext = os.path.splitext(anchor)
            if ext == ".kicad_pro":
                pro = anchor
            elif ext == ".kicad_sch":
                sch = anchor
            elif ext == ".kicad_pcb":
                pcb = anchor
            if os.path.isfile(stem + ".kicad_pro"):
                pro = stem + ".kicad_pro"
    if pro:
        stem = os.path.splitext(pro)[0]
        if not sch and os.path.isfile(stem + ".kicad_sch"):
            sch = stem + ".kicad_sch"
        if not pcb and os.path.isfile(stem + ".kicad_pcb"):
            pcb = stem + ".kicad_pcb"
    if board_path and not pcb:
        pcb = board_path
    return {"project": pro, "schematic": sch, "board": pcb}


# ---------------------------------------------------------------------------
# Schematic side
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def schematic_inventory(root_sch: str) -> Dict[str, Any]:
    """Sheets and placed parts of a hierarchy, from the files on disk.

    Power symbols (``#PWR``/``#FLG`` references or ``power:`` lib ids) are not
    parts. A reused sub-sheet contributes every instance's reference.
    """
    sheets = sheet_tree(Path(root_sch))
    refs: Set[str] = set()
    board_refs: Set[str] = set()
    dnp: Set[str] = set()
    unannotated = 0
    for sheet in sheets:
        try:
            text = _read(str(sheet))
        except OSError:
            continue
        for offset in iter_child_offsets(text):
            m = _SYMBOL_HEAD.match(text, offset)
            if not m:
                continue
            end = match_paren(text, offset)
            if end == -1:
                continue
            block = text[offset : end + 1]
            lib_id = unescape_sexpr_string(m.group(1))
            names = {unescape_sexpr_string(r) for r in _INSTANCE_REFERENCE.findall(block)}
            prop = _REFERENCE_PROP.search(block)
            if not names and prop:
                names = {unescape_sexpr_string(prop.group(1))}
            names = {n for n in names if n and not n.startswith("#")}
            if lib_id.lower().startswith("power:") or not names:
                continue
            for name in names:
                if name.endswith("?"):
                    unannotated += 1
                    continue
                refs.add(name)
                if not _ON_BOARD_NO.search(block):
                    board_refs.add(name)
                if _DNP_YES.search(block):
                    dnp.add(name)
    return {
        "sheets": [str(s) for s in sheets],
        "refs": refs,
        "boardRefs": board_refs,
        "dnp": dnp,
        "unannotated": unannotated,
    }


# ---------------------------------------------------------------------------
# Board side
# ---------------------------------------------------------------------------


def board_inventory_from_file(pcb_path: str) -> Dict[str, Any]:
    """Footprint references from a .kicad_pcb on disk (no pcbnew needed)."""
    text = _read(pcb_path)
    refs: Set[str] = set()
    board_only: Set[str] = set()
    count = 0
    for offset in iter_child_offsets(text):
        if not _FOOTPRINT_HEAD.match(text, offset):
            continue
        end = match_paren(text, offset)
        if end == -1:
            continue
        block = text[offset : end + 1]
        count += 1
        m = _REFERENCE_PROP.search(block) or _FP_TEXT_REFERENCE.search(block)
        ref = unescape_sexpr_string(m.group(1)) if m else ""
        if not ref:
            continue
        attr = _ATTR.search(block)
        if attr and "board_only" in attr.group(1):
            board_only.add(ref)
        else:
            refs.add(ref)
    return {"footprints": count, "refs": refs, "boardOnly": board_only, "source": "file"}


def board_inventory_from_memory(board: Any) -> Dict[str, Any]:
    """Footprint references and unrouted count from a loaded pcbnew board."""
    refs: Set[str] = set()
    board_only: Set[str] = set()
    footprints = list(board.GetFootprints())
    for fp in footprints:
        ref = fp.GetReference()
        if not ref:
            continue
        try:
            is_board_only = bool(fp.IsBoardOnly())
        except Exception:
            is_board_only = False
        (board_only if is_board_only else refs).add(ref)
    unrouted = None
    try:
        conn = board.GetConnectivity()
        try:
            conn.RecalculateRatsnest()
        except Exception:
            pass
        unrouted = int(conn.GetUnconnectedCount(False))
    except Exception as exc:
        logger.debug(f"project_status: unrouted count unavailable: {exc}")
    return {
        "footprints": len(footprints),
        "refs": refs,
        "boardOnly": board_only,
        "unrouted": unrouted,
        "source": "memory",
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _age(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    secs = max(0, int(time.time() - float(ts)))
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60}m ago"
    if secs < 172800:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _check_line(kind: str, last: Optional[Dict[str, Any]]) -> Tuple[str, Optional[Dict]]:
    label = kind.upper()
    if not last:
        return f"{label}: never run (via run_{kind}) for this file", None
    sev = last.get("by_severity", {}) or {}
    top = sorted((last.get("by_type") or {}).items(), key=lambda kv: -kv[1])[:3]
    top_txt = ", ".join(f"{t} {n}" for t, n in top)
    line = (
        f"{label}: {last.get('total', 0)} ({sev.get('error', 0)} err, "
        f"{sev.get('warning', 0)} warn) {_age(last.get('timestamp'))}"
    )
    if top_txt:
        line += f"; top: {top_txt}"
    info = {
        "total": last.get("total", 0),
        "by_severity": sev,
        "by_type": last.get("by_type") or {},
        "ranAt": check_summary._iso(last.get("timestamp")),
    }
    return line, info


def _last_snapshot(project_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    if not project_dir:
        return None
    base = Path(project_dir) / "snapshots"
    if not base.is_dir():
        return None
    snaps = [p for p in base.iterdir() if p.is_dir()]
    if not snaps:
        return None
    newest = max(snaps, key=lambda p: p.stat().st_mtime)
    return {"name": newest.name, "path": str(newest), "time": newest.stat().st_mtime}


def _sorted_refs(refs: Set[str]) -> List[str]:
    def key(r: str):
        m = re.match(r"([^\d]*)(\d*)(.*)", r)
        prefix, num, rest = m.groups() if m else (r, "", "")
        return (prefix, int(num) if num else -1, rest)

    return sorted(refs, key=key)


def build_project_status(
    project_path: Optional[str],
    board_path: Optional[str],
    loaded_board: Any = None,
    dirty: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the status report. ``loaded_board`` is the in-memory pcbnew
    board when it is the project's board (gives the unrouted count)."""
    files = resolve_project_files(project_path, board_path)
    pro, sch, pcb = files["project"], files["schematic"], files["board"]
    if not (pro or sch or pcb):
        raise ValueError(
            "No project found: open a project/board or pass projectPath "
            "(.kicad_pro, .kicad_sch, .kicad_pcb or its directory)"
        )
    project_dir = os.path.dirname(pro or sch or pcb)
    name = os.path.splitext(os.path.basename(pro or sch or pcb))[0]
    lines = [f"Project: {name} ({project_dir})"]
    out: Dict[str, Any] = {"project": files}

    # Schematic
    sch_inv = None
    if sch and os.path.isfile(sch):
        sch_inv = schematic_inventory(sch)
        unann = sch_inv["unannotated"]
        lines.append(
            f"Schematic: {len(sch_inv['sheets'])} sheet(s), {len(sch_inv['refs'])} part(s)"
            + (f", {len(sch_inv['dnp'])} DNP" if sch_inv["dnp"] else "")
            + (f", {unann} unannotated" if unann else "")
        )
        out["schematic"] = {
            "sheets": len(sch_inv["sheets"]),
            "components": len(sch_inv["refs"]),
            "dnp": len(sch_inv["dnp"]),
            "unannotated": unann,
        }
    else:
        lines.append("Schematic: none found")

    # Board
    brd_inv = None
    if pcb and os.path.isfile(pcb):
        if loaded_board is not None:
            try:
                brd_inv = board_inventory_from_memory(loaded_board)
            except Exception as exc:
                logger.debug(f"project_status: in-memory board unreadable ({exc}); using file")
        if brd_inv is None:
            brd_inv = board_inventory_from_file(pcb)
        unrouted = brd_inv.get("unrouted")
        last_drc_for_unrouted = None
        if unrouted is None:
            last = check_summary.load_last_run("drc", pcb)
            if last:
                n = sum(
                    1
                    for v in last.get("violations", [])
                    if v.get("category") == "unconnected_items"
                )
                last_drc_for_unrouted = n
        if unrouted is not None:
            unrouted_txt = f"{unrouted} unrouted"
        elif last_drc_for_unrouted is not None:
            unrouted_txt = f"{last_drc_for_unrouted} unrouted (as of last DRC)"
        else:
            unrouted_txt = "unrouted: n/a (board not loaded; run open_board or run_drc)"
        lines.append(
            f"Board: {brd_inv['footprints']} footprint(s), {unrouted_txt}"
            + (f", {len(brd_inv['boardOnly'])} board-only" if brd_inv["boardOnly"] else "")
            + ("" if brd_inv["source"] == "memory" else " [read from file]")
        )
        out["board"] = {
            "footprints": brd_inv["footprints"],
            "unrouted": unrouted if unrouted is not None else last_drc_for_unrouted,
            "unroutedSource": (
                "memory"
                if unrouted is not None
                else ("last_drc" if last_drc_for_unrouted is not None else None)
            ),
            "boardOnly": len(brd_inv["boardOnly"]),
            "source": brd_inv["source"],
        }
    else:
        lines.append("Board: none found")

    # Parity
    if sch_inv is not None and brd_inv is not None:
        sch_side = sch_inv["boardRefs"]
        brd_side = brd_inv["refs"]
        missing_on_board = sch_side - brd_side
        missing_in_sch = brd_side - sch_inv["refs"]
        if not missing_on_board and not missing_in_sch:
            lines.append(f"Parity: OK ({len(sch_side)} refs match)")
        else:
            parts = []
            if missing_on_board:
                show = _sorted_refs(missing_on_board)[:_LIST_CAP]
                more = len(missing_on_board) - len(show)
                parts.append(
                    f"{len(missing_on_board)} not on board ({', '.join(show)}"
                    + (f", +{more}" if more > 0 else "")
                    + ")"
                )
            if missing_in_sch:
                show = _sorted_refs(missing_in_sch)[:_LIST_CAP]
                more = len(missing_in_sch) - len(show)
                parts.append(
                    f"{len(missing_in_sch)} not in schematic ({', '.join(show)}"
                    + (f", +{more}" if more > 0 else "")
                    + ")"
                )
            lines.append("Parity: " + "; ".join(parts))
        out["parity"] = {
            "ok": not missing_on_board and not missing_in_sch,
            "missingOnBoard": _sorted_refs(missing_on_board),
            "missingInSchematic": _sorted_refs(missing_in_sch),
        }

    # Last checks (cached; never re-run here)
    if pcb:
        line, info = _check_line("drc", check_summary.load_last_run("drc", pcb))
        lines.append(line)
        out["lastDrc"] = info
    if sch:
        line, info = _check_line("erc", check_summary.load_last_run("erc", sch))
        lines.append(line)
        out["lastErc"] = info

    # Dirty flags
    if dirty is not None:
        state = dirty.get("dirty")
        txt = {True: "UNSAVED changes", False: "saved", None: "unknown"}.get(state, str(state))
        extra = " (disk changed externally)" if dirty.get("diskChangedExternally") else ""
        lines.append(f"Board in memory: {txt}{extra}")
        out["dirty"] = dirty
    elif pcb:
        lines.append("Board in memory: not loaded")

    # Snapshot
    snap = _last_snapshot(project_dir)
    if snap:
        lines.append(f"Last snapshot: {snap['name']} ({_age(snap['time'])})")
        snap["time"] = check_summary._iso(snap["time"])
    else:
        lines.append("Last snapshot: none (snapshot_project)")
    out["lastSnapshot"] = snap

    out["lines"] = lines
    out["text"] = "\n".join(lines)
    return out
