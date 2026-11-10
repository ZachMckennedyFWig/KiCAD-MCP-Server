"""Project snapshots: list, restore, diff, and automatic pre-mutation snapshots (I1).

Snapshots live in ``<project>/snapshots/<id>/`` -- the directory
``snapshot_project`` already uses, so ``project_status`` reports the newest
one whichever tool made it. A snapshot written here holds only the design
files (``.kicad_pro``/``.kicad_pcb``/``.kicad_sch`` and sub-sheets,
``.kicad_dru``, ``.kicad_prl``, library tables), stored under their path
relative to the project, plus a ``snapshot.json`` manifest with a SHA-256 per
file. Full-folder snapshots made by ``snapshot_project`` have no manifest; the
same design files are read from them.

Kinds (the id prefix): ``auto`` (taken by the dispatcher before a mutating
tool when ``KICAD_MCP_AUTO_SNAPSHOT=1``; only the files that tool can write),
``safety`` (taken by ``restore_snapshot`` before it overwrites anything),
and ``legacy`` (a ``snapshot_project`` folder).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.file_io import write_bytes_atomic
from utils.sexpr_format import iter_child_offsets, match_paren, unescape_sexpr_string

logger = logging.getLogger("kicad_interface")

SNAPSHOT_DIR = "snapshots"
MANIFEST = "snapshot.json"
AUTO_ENV = "KICAD_MCP_AUTO_SNAPSHOT"
AUTO_KEEP_ENV = "KICAD_MCP_AUTO_SNAPSHOT_KEEP"
DEFAULT_AUTO_KEEP = 20
_DESIGN_SUFFIXES = (".kicad_pro", ".kicad_pcb", ".kicad_sch", ".kicad_dru", ".kicad_prl")
_DESIGN_NAMES = ("fp-lib-table", "sym-lib-table")
#: Tools that may write every sheet of the hierarchy, not just the one named.
HIERARCHY_WIDE = {
    "apply_property_map",
    "relabel_net",
    "annotate_schematic",
    "backannotate_footprints",
    "update_symbol_from_library",
}


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def design_files(base: Path) -> List[str]:
    """Design files of the project in *base*, as posix paths relative to it."""
    base = Path(base)
    found: List[str] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    roots = []
    for p in entries:
        if not p.is_file():
            continue
        if p.suffix in _DESIGN_SUFFIXES or p.name in _DESIGN_NAMES:
            found.append(p.name)
            if p.suffix == ".kicad_sch":
                roots.append(p)
    # Sub-sheets in sub-directories, reached through the hierarchy (never a
    # glob: backup folders hold stale sheet copies).
    try:
        from utils.sheet_tree import sheet_tree

        for root in roots:
            for sheet in sheet_tree(root):
                try:
                    rel = sheet.resolve().relative_to(base.resolve()).as_posix()
                except ValueError:
                    continue
                if rel not in found:
                    found.append(rel)
    except Exception as exc:  # pragma: no cover - best effort
        logger.debug(f"snapshot: sheet walk failed in {base}: {exc}")
    return found


def project_dir_for(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_dir():
        return p.resolve()
    if p.suffix in _DESIGN_SUFFIXES or p.exists():
        return p.resolve().parent
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-")[:48]


def create_snapshot(
    project_dir: Path,
    files: Optional[Iterable[str]] = None,
    label: str = "",
    kind: str = "manual",
    tool: Optional[str] = None,
) -> Dict[str, Any]:
    """Copy *files* (relative to *project_dir*; default: all design files)."""
    project_dir = Path(project_dir)
    rels = list(files) if files is not None else design_files(project_dir)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    snap_id = "_".join(p for p in (kind, stamp, _slug(label or tool or "")) if p)
    base = project_dir / SNAPSHOT_DIR
    target = base / snap_id
    target.mkdir(parents=True, exist_ok=False)
    entries = []
    for rel in rels:
        src = project_dir / rel
        if not src.is_file():
            continue
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        entries.append({"path": rel, "sha256": _sha256(dst), "size": dst.stat().st_size})
    manifest = {
        "version": 1,
        "id": snap_id,
        "label": label,
        "kind": kind,
        "tool": tool,
        "created": datetime.now().isoformat(timespec="seconds"),
        "createdTs": time.time(),
        "projectDir": str(project_dir),
        "files": entries,
    }
    (target / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------- #
# Snapshot discovery
# --------------------------------------------------------------------------- #


def _read_manifest(snap: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((snap / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def snapshot_info(snap: Path) -> Dict[str, Any]:
    manifest = _read_manifest(snap)
    if manifest:
        return {
            "id": snap.name,
            "label": manifest.get("label") or "",
            "kind": manifest.get("kind", "manual"),
            "tool": manifest.get("tool"),
            "created": manifest.get("created"),
            "createdTs": manifest.get("createdTs") or snap.stat().st_mtime,
            "files": [f["path"] for f in manifest.get("files", [])],
            "path": str(snap),
        }
    mtime = snap.stat().st_mtime
    label = snap.name.split("_snapshot_", 1)[-1] if "_snapshot_" in snap.name else snap.name
    return {
        "id": snap.name,
        "label": label,
        "kind": "legacy",
        "tool": "snapshot_project",
        "created": datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "createdTs": mtime,
        "files": design_files(snap),
        "path": str(snap),
    }


def all_snapshots(project_dir: Path) -> List[Dict[str, Any]]:
    base = Path(project_dir) / SNAPSHOT_DIR
    if not base.is_dir():
        return []
    snaps = [snapshot_info(p) for p in base.iterdir() if p.is_dir()]
    return sorted(snaps, key=lambda s: s["createdTs"], reverse=True)


def find_snapshot(project_dir: Path, key: str) -> Dict[str, Any]:
    """Resolve an id, a label (newest wins) or a unique id substring."""
    snaps = all_snapshots(project_dir)
    if not snaps:
        raise LookupError(f"No snapshots in {Path(project_dir) / SNAPSHOT_DIR}")
    if key in ("latest", "last"):
        return snaps[0]
    for s in snaps:
        if s["id"] == key:
            return s
    by_label = [s for s in snaps if s["label"] == key]
    if by_label:
        return by_label[0]
    partial = [s for s in snaps if key in s["id"]]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise LookupError(
            f"'{key}' matches {len(partial)} snapshots: " + ", ".join(s["id"] for s in partial[:5])
        )
    raise LookupError(f"No snapshot '{key}' (list_snapshots shows the ids)")


# --------------------------------------------------------------------------- #
# Automatic snapshots (dispatcher hook)
# --------------------------------------------------------------------------- #


def auto_snapshot_enabled() -> bool:
    return os.environ.get(AUTO_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _auto_keep() -> int:
    try:
        return max(1, int(os.environ.get(AUTO_KEEP_ENV, DEFAULT_AUTO_KEEP)))
    except ValueError:
        return DEFAULT_AUTO_KEEP


def prune_auto(project_dir: Path, keep: int) -> List[str]:
    base = Path(project_dir) / SNAPSHOT_DIR
    if not base.is_dir():
        return []
    autos = sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("auto_"))
    removed = []
    for old in autos[: max(0, len(autos) - keep)]:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old.name)
    return removed


def auto_snapshot_targets(iface: Any, command: str, params: Dict[str, Any]) -> List[Path]:
    """The files *command* can write, as far as the dispatcher can tell."""
    targets: List[Path] = []
    if command == "restore_snapshot":
        return []  # takes its own safety snapshot
    board_cmds = getattr(iface, "_BOARD_MUTATING_COMMANDS", set())
    if command in board_cmds or (command == "run_pcbnew_script" and params.get("save")):
        board = params.get("boardPath") if command == "run_pcbnew_script" else None
        board = board or iface._current_board_path()
        if board:
            targets.append(Path(board))
    if command in getattr(iface, "_SCHEMATIC_MUTATING_COMMANDS", set()):
        try:
            sheets = [Path(p) for p in iface._schematic_write_targets(command, params)]
        except Exception:
            sheets = []
        if command in HIERARCHY_WIDE:
            from utils.sheet_tree import sheet_tree

            expanded: List[Path] = []
            for s in sheets:
                if s.suffix == ".kicad_pro" or s.is_dir():
                    pros = [s] if s.suffix == ".kicad_pro" else sorted(s.glob("*.kicad_pro"))
                    s = pros[0].with_suffix(".kicad_sch") if len(pros) == 1 else s
                expanded.extend(sheet_tree(s) if s.suffix == ".kicad_sch" else [])
            sheets = expanded
        targets.extend(sheets)
    seen, out = set(), []
    for t in targets:
        key = os.path.normcase(str(t.resolve())) if t.exists() else None
        if key and key not in seen:
            seen.add(key)
            out.append(t.resolve())
    return out


def restore_schematic_targets(params: Dict[str, Any], board_path: Optional[str]) -> List[str]:
    """Root schematic(s) a restore_snapshot call may overwrite, for the open-in-KiCad guard."""
    if params.get("dryRun"):
        return []
    project_dir = project_dir_for(params.get("projectPath")) or project_dir_for(board_path)
    if not project_dir:
        return []
    return [
        str(project_dir / rel) for rel in design_files(project_dir) if rel.endswith(".kicad_sch")
    ]


def maybe_auto_snapshot(iface: Any, command: str, params: Dict[str, Any]) -> Optional[str]:
    """Snapshot what *command* may overwrite; None when disabled or nothing to save.

    Never raises: a failing snapshot must not block the tool it guards.
    """
    if not auto_snapshot_enabled():
        return None
    try:
        targets = auto_snapshot_targets(iface, command, params or {})
        if not targets:
            return None
        project_dir = targets[0].parent
        rels = []
        for t in targets:
            try:
                rels.append(t.relative_to(project_dir).as_posix())
            except ValueError:
                # Another directory (a sheet outside the project dir): snapshot
                # it next to its own project instead of mixing trees.
                continue
        manifest = create_snapshot(project_dir, rels, label=command, kind="auto", tool=command)
        prune_auto(project_dir, _auto_keep())
        return manifest["id"]
    except Exception as exc:
        logger.warning(f"auto-snapshot before {command} failed: {exc}")
        return None


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #

_Q = r'"((?:[^"\\]|\\.)*)"'
_NET_REF = re.compile(r"\(net\s+(?:" + _Q + r"|(\d+))")
_NET_DEF = re.compile(r"\(net\s+(\d+)\s+" + _Q + r"\s*\)")
_AT = re.compile(r"\(at\s+([-\d.eE+]+)\s+([-\d.eE+]+)(?:\s+([-\d.eE+]+))?")
_LAYER = re.compile(r"\(layer\s+" + _Q)
_LAYERS = re.compile(r"\(layers((?:\s+" + r'"(?:[^"\\]|\\.)*"' + r")+)\s*\)")
_PROP = re.compile(r"\(property\s+" + _Q + r"\s+" + _Q)
_FP_HEAD = re.compile(r"\(footprint\s+" + _Q)
_ZONE_NAME = re.compile(r"\(name\s+" + _Q)
_PRIORITY = re.compile(r"\(priority\s+(\d+)")
_LENGTH_PTS = re.compile(
    r"\(start\s+([-\d.eE+]+)\s+([-\d.eE+]+)\)\s*\(end\s+([-\d.eE+]+)\s+([-\d.eE+]+)\)"
)


def _net_of(block: str, table: Dict[str, str]) -> str:
    m = _NET_REF.search(block)
    if not m:
        return ""
    if m.group(1) is not None:
        return unescape_sexpr_string(m.group(1))
    return table.get(m.group(2), m.group(2))


def board_summary(text: str) -> Dict[str, Any]:
    """Footprints, copper counts per net and zones of a .kicad_pcb text."""
    nets: Dict[str, str] = {}
    footprints: Dict[str, Dict[str, Any]] = {}
    tracks: Dict[str, List[float]] = {}
    vias: Dict[str, int] = {}
    zones: List[Dict[str, Any]] = []
    for off in iter_child_offsets(text):
        head = text[off : off + 12]
        if head.startswith("(net "):
            m = _NET_DEF.match(text, off)
            if m:
                nets[m.group(1)] = unescape_sexpr_string(m.group(2))
            continue
        if not (
            head.startswith("(footprint")
            or head.startswith("(segment")
            or head.startswith("(arc")
            or head.startswith("(via")
            or head.startswith("(zone")
        ):
            continue
        end = match_paren(text, off)
        if end == -1:
            continue
        block = text[off : end + 1]
        if head.startswith("(footprint"):
            m = _FP_HEAD.match(block)
            lib = unescape_sexpr_string(m.group(1)) if m else ""
            ref = value = ""
            at = layer = None
            for c in iter_child_offsets(block):
                ch = block[c : c + 10]
                if ch.startswith("(at ") and at is None:
                    am = _AT.match(block, c)
                    if am:
                        at = (float(am.group(1)), float(am.group(2)), float(am.group(3) or 0))
                elif ch.startswith("(layer") and layer is None:
                    lm = _LAYER.match(block, c)
                    layer = unescape_sexpr_string(lm.group(1)) if lm else None
                elif ch.startswith("(property"):
                    pm = _PROP.match(block, c)
                    if pm and pm.group(1) == "Reference":
                        ref = unescape_sexpr_string(pm.group(2))
                    elif pm and pm.group(1) == "Value":
                        value = unescape_sexpr_string(pm.group(2))
                elif ch.startswith("(fp_text") and not ref:
                    rm = re.match(r"\(fp_text\s+reference\s+" + _Q, block[c:])
                    if rm:
                        ref = unescape_sexpr_string(rm.group(1))
            key = ref or f"?{len(footprints)}"
            if key in footprints:
                key = f"{key}#{len(footprints)}"
            footprints[key] = {
                "lib": lib,
                "value": value,
                "at": at or (0.0, 0.0, 0.0),
                "layer": layer,
            }
        elif head.startswith("(segment") or head.startswith("(arc"):
            net = _net_of(block, nets)
            entry = tracks.setdefault(net, [0, 0.0])
            entry[0] += 1
            lm = _LENGTH_PTS.search(block)
            if lm and head.startswith("(segment"):
                x1, y1, x2, y2 = (float(v) for v in lm.groups())
                entry[1] += ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        elif head.startswith("(via"):
            net = _net_of(block, nets)
            vias[net] = vias.get(net, 0) + 1
        else:
            net = _net_of(block, nets)
            lm = _LAYERS.search(block)
            if lm:
                layers = re.findall(_Q, lm.group(1))
            else:
                one = _LAYER.search(block)
                layers = [one.group(1)] if one else []
            name = _ZONE_NAME.search(block)
            prio = _PRIORITY.search(block)
            poly_at = block.find("(polygon")
            outline = ""
            if poly_at != -1:
                poly_end = match_paren(block, poly_at)
                outline = re.sub(r"\s+", " ", block[poly_at : poly_end + 1])
            keepout = "(keepout" in block
            zones.append(
                {
                    "net": net,
                    "layers": layers,
                    "name": unescape_sexpr_string(name.group(1)) if name else "",
                    "priority": int(prio.group(1)) if prio else 0,
                    "ruleArea": keepout,
                    "outline": hashlib.sha1(outline.encode("utf-8")).hexdigest()[:12],
                }
            )
    return {"footprints": footprints, "tracks": tracks, "vias": vias, "zones": zones}


def _zone_key(z: Dict[str, Any]) -> str:
    kind = "rule_area" if z["ruleArea"] else "zone"
    return f"{kind} {z['name'] or z['net'] or '-'} [{','.join(z['layers'])}] p{z['priority']}"


def diff_boards(a: Dict[str, Any], b: Dict[str, Any], limit: int) -> Dict[str, Any]:
    fa, fb = a["footprints"], b["footprints"]
    added = sorted(set(fb) - set(fa))
    removed = sorted(set(fa) - set(fb))
    moved, changed = [], []
    for ref in sorted(set(fa) & set(fb)):
        x1, y1, r1 = fa[ref]["at"]
        x2, y2, r2 = fb[ref]["at"]
        if (
            abs(x2 - x1) > 1e-6
            or abs(y2 - y1) > 1e-6
            or abs(r2 - r1) > 1e-6
            or fa[ref]["layer"] != fb[ref]["layer"]
        ):
            moved.append(
                {
                    "ref": ref,
                    "from": [x1, y1, r1, fa[ref]["layer"]],
                    "to": [x2, y2, r2, fb[ref]["layer"]],
                    "dx": round(x2 - x1, 4),
                    "dy": round(y2 - y1, 4),
                    "drot": round(r2 - r1, 3),
                }
            )
        diffs = {k: [fa[ref][k], fb[ref][k]] for k in ("lib", "value") if fa[ref][k] != fb[ref][k]}
        if diffs:
            changed.append({"ref": ref, **diffs})

    def per_net(da: Dict[str, Any], db: Dict[str, Any], count) -> List[Dict[str, Any]]:
        out = []
        for net in sorted(set(da) | set(db)):
            ca, cb = count(da.get(net)), count(db.get(net))
            if ca != cb:
                out.append({"net": net or "<no net>", "a": ca, "b": cb, "delta": cb - ca})
        return sorted(out, key=lambda d: -abs(d["delta"]))

    track_nets = per_net(a["tracks"], b["tracks"], lambda v: v[0] if v else 0)
    for item in track_nets:
        la = a["tracks"].get(item["net"] if item["net"] != "<no net>" else "", [0, 0.0])[1]
        lb = b["tracks"].get(item["net"] if item["net"] != "<no net>" else "", [0, 0.0])[1]
        item["lengthDeltaMm"] = round(lb - la, 3)
    via_nets = per_net(a["vias"], b["vias"], lambda v: v or 0)

    za: Dict[str, List[str]] = {}
    zb: Dict[str, List[str]] = {}
    for z in a["zones"]:
        za.setdefault(_zone_key(z), []).append(z["outline"])
    for z in b["zones"]:
        zb.setdefault(_zone_key(z), []).append(z["outline"])
    zones_added = sorted(k for k in zb if k not in za)
    zones_removed = sorted(k for k in za if k not in zb)
    zones_changed = sorted(k for k in za if k in zb and sorted(za[k]) != sorted(zb[k]))
    ta = sum(v[0] for v in a["tracks"].values())
    tb = sum(v[0] for v in b["tracks"].values())
    va, vb = sum(a["vias"].values()), sum(b["vias"].values())
    return {
        "footprints": {
            "a": len(fa),
            "b": len(fb),
            "added": added[:limit],
            "removed": removed[:limit],
            "moved": moved[:limit],
            "movedCount": len(moved),
            "changed": changed[:limit],
        },
        "tracks": {"a": ta, "b": tb, "delta": tb - ta, "byNet": track_nets[:limit]},
        "vias": {"a": va, "b": vb, "delta": vb - va, "byNet": via_nets[:limit]},
        "zones": {
            "a": len(a["zones"]),
            "b": len(b["zones"]),
            "added": zones_added,
            "removed": zones_removed,
            "changed": zones_changed,
        },
    }


_SCH_COUNTED = {
    "label": "labels",
    "global_label": "globalLabels",
    "hierarchical_label": "hierarchicalLabels",
    "wire": "wires",
    "junction": "junctions",
    "no_connect": "noConnects",
    "sheet": "sheets",
}


def schematic_summary(sheets: Dict[str, str]) -> Dict[str, Any]:
    """Symbols by reference and item counts per sheet, from {relpath: text}."""
    from commands.bom_tools import read_sheet_symbols

    symbols: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, Dict[str, int]] = {}
    for rel, text in sheets.items():
        c = {v: 0 for v in _SCH_COUNTED.values()}
        for off in iter_child_offsets(text):
            m = re.match(r"\(([a-z_]+)", text[off : off + 24])
            if m and m.group(1) in _SCH_COUNTED:
                c[_SCH_COUNTED[m.group(1)]] += 1
        counts[rel] = c
        for sym in read_sheet_symbols(Path(rel), text, ""):
            if sym.is_power:
                continue
            fields = dict(sym.props)
            fields.pop("Reference", None)
            for flag, val in sym.flags.items():
                fields[f"({flag})"] = "yes" if val else "no"
            fields["(lib_id)"] = sym.lib_id
            for ref in sym.refs or ["?"]:
                symbols.setdefault(ref, {"sheet": rel, "fields": fields})
    return {"symbols": symbols, "counts": counts}


def diff_schematics(a: Dict[str, Any], b: Dict[str, Any], limit: int) -> Dict[str, Any]:
    sa, sb = a["symbols"], b["symbols"]
    from commands.bom_tools import natural_key

    added = sorted(set(sb) - set(sa), key=natural_key)
    removed = sorted(set(sa) - set(sb), key=natural_key)
    changed = []
    for ref in sorted(set(sa) & set(sb), key=natural_key):
        fa, fb = sa[ref]["fields"], sb[ref]["fields"]
        diff = {
            k: [fa.get(k), fb.get(k)] for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)
        }
        if diff:
            changed.append({"ref": ref, "fields": diff})
    per_sheet = []
    for rel in sorted(set(a["counts"]) | set(b["counts"])):
        ca, cb = a["counts"].get(rel), b["counts"].get(rel)
        if ca is None or cb is None:
            per_sheet.append({"sheet": rel, "status": "added" if ca is None else "removed"})
            continue
        delta = {k: cb[k] - ca[k] for k in ca if cb[k] != ca[k]}
        if delta:
            per_sheet.append({"sheet": rel, "delta": delta})
    return {
        "symbols": {
            "a": len(sa),
            "b": len(sb),
            "added": added[:limit],
            "removed": removed[:limit],
            "changed": changed[:limit],
            "changedCount": len(changed),
        },
        "sheets": per_sheet,
    }


# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #


class SnapshotCommands:
    """list_snapshots / restore_snapshot / diff_snapshots (MCP_ISSUES I1)."""

    def __init__(self, interface: Any = None):
        self.iface = interface

    # -- helpers ------------------------------------------------------------ #

    def _project_dir(self, params: Dict[str, Any]) -> Path:
        explicit = project_dir_for(params.get("projectPath"))
        if explicit:
            return explicit
        board = None
        if self.iface is not None:
            try:
                board = self.iface._authoritative_board_path()
            except Exception:
                board = None
        found = project_dir_for(board)
        if not found:
            raise ValueError("projectPath is required (no board is loaded)")
        return found

    @staticmethod
    def _files_of(project_dir: Path, snap: Dict[str, Any]) -> Dict[str, Path]:
        base = Path(snap["path"])
        return {rel: base / rel for rel in snap["files"] if (base / rel).is_file()}

    # -- list --------------------------------------------------------------- #

    def list_snapshots(self, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            project_dir = self._project_dir(params)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        try:
            limit = max(1, int(params.get("limit", 20)))
        except (TypeError, ValueError):
            limit = 20
        kind = params.get("kind")
        snaps = all_snapshots(project_dir)
        if kind:
            snaps = [s for s in snaps if s["kind"] == kind]
        shown = [
            {
                "id": s["id"],
                "label": s["label"],
                "kind": s["kind"],
                "tool": s["tool"],
                "created": s["created"],
                "files": len(s["files"]),
            }
            for s in snaps[:limit]
        ]
        return {
            "success": True,
            "message": f"{len(snaps)} snapshot(s) in {project_dir / SNAPSHOT_DIR}"
            + (f"; showing {len(shown)}" if len(shown) < len(snaps) else ""),
            "projectDir": str(project_dir),
            "autoSnapshot": {
                "enabled": auto_snapshot_enabled(),
                "keep": _auto_keep(),
                "env": AUTO_ENV,
            },
            "count": len(snaps),
            "snapshots": shown,
        }

    # -- restore ------------------------------------------------------------ #

    def restore_snapshot(self, params: Dict[str, Any]) -> Dict[str, Any]:
        key = params.get("snapshot") or params.get("label") or params.get("id")
        if not key:
            return {"success": False, "message": "snapshot (id or label) is required"}
        dry_run = bool(params.get("dryRun", False))
        only = params.get("files")
        try:
            project_dir = self._project_dir(params)
            snap = find_snapshot(project_dir, str(key))
        except (ValueError, LookupError) as exc:
            return {"success": False, "message": str(exc)}
        files = self._files_of(project_dir, snap)
        if only:
            wanted = {str(f).replace("\\", "/") for f in only}
            files = {r: p for r, p in files.items() if r in wanted or Path(r).name in wanted}
        if not files:
            return {"success": False, "message": f"Snapshot {snap['id']} holds no design files"}

        plan = []
        for rel, src in sorted(files.items()):
            dst = project_dir / rel
            if not dst.exists():
                status = "create"
            elif _sha256(dst) == _sha256(src):
                status = "unchanged"
            else:
                status = "overwrite"
            plan.append({"path": rel, "action": status})
        to_write = [p for p in plan if p["action"] != "unchanged"]
        current = set(design_files(project_dir))
        extra = sorted(current - set(snap["files"])) if not only else []

        board_path = None
        warnings: List[str] = []
        if self.iface is not None:
            try:
                board_path = self.iface._current_board_path()
            except Exception:
                board_path = None
            if getattr(self.iface, "session_backend", None) == "ipc":
                warnings.append(
                    "The board is owned by the live KiCad GUI (IPC): reload it there "
                    "(File > Revert) after the restore."
                )
        board_rel = None
        if board_path:
            try:
                board_rel = Path(board_path).resolve().relative_to(project_dir).as_posix()
            except ValueError:
                board_rel = None
        board_touched = board_rel is not None and any(p["path"] == board_rel for p in to_write)
        if board_touched and not dry_run and not params.get("force"):
            dirty = self.iface._dirty_state(board_path).get("dirty")
            if dirty:
                return {
                    "success": False,
                    "message": (
                        "The loaded board has unsaved in-memory changes that the restore would "
                        "discard. Save them (save_project) or pass force=true."
                    ),
                }

        result: Dict[str, Any] = {
            "success": True,
            "snapshot": {k: snap[k] for k in ("id", "label", "kind", "created")},
            "dryRun": dry_run,
            "files": plan,
            "changedFiles": len(to_write),
            "keptFilesNotInSnapshot": extra,
            "warnings": warnings,
        }
        if dry_run or not to_write:
            result["message"] = (
                f"{'Would restore' if dry_run else 'Nothing to restore:'} "
                f"{len(to_write)} file(s) from {snap['id']}"
            )
            return result

        safety = create_snapshot(
            project_dir,
            [p["path"] for p in to_write if (project_dir / p["path"]).exists()],
            label=f"before-restore-{snap['id']}"[:60],
            kind="safety",
            tool="restore_snapshot",
        )
        written = []
        try:
            for item in to_write:
                data = files[item["path"]].read_bytes()
                dst = project_dir / item["path"]
                dst.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomic(dst, data)
                written.append(item["path"])
        except OSError as exc:
            # Put back what was already replaced, from the safety snapshot.
            safety_dir = project_dir / SNAPSHOT_DIR / safety["id"]
            for rel in written:
                src = safety_dir / rel
                try:
                    if src.is_file():
                        write_bytes_atomic(project_dir / rel, src.read_bytes())
                    else:
                        (project_dir / rel).unlink()
                except OSError:
                    pass
            return {
                "success": False,
                "message": f"Restore failed and was rolled back: {exc}",
                "safetySnapshot": safety["id"],
            }
        result["safetySnapshot"] = safety["id"]
        result["message"] = (
            f"Restored {len(written)} file(s) from {snap['id']}; previous state saved as "
            f"{safety['id']}"
        )
        if board_touched:
            # The dispatcher reloads the in-memory board from this file.
            result["_boardFileWritten"] = {"mode": "disk", "path": board_path}
            result["boardReloaded"] = True
        if any(p.endswith(".kicad_sch") for p in written):
            warnings.append(
                "Schematic files were replaced on disk; if they are open in KiCad, "
                "reload them there before saving."
            )
        return result

    # -- diff --------------------------------------------------------------- #

    def _side(self, project_dir: Path, key: str) -> Tuple[str, Dict[str, Path]]:
        if key in ("current", "", None):
            return "current", {rel: project_dir / rel for rel in design_files(project_dir)}
        snap = find_snapshot(project_dir, key)
        return snap["id"], self._files_of(project_dir, snap)

    def diff_snapshots(self, params: Dict[str, Any]) -> Dict[str, Any]:
        a_key = params.get("a")
        b_key = params.get("b") or "current"
        if not a_key:
            return {"success": False, "message": "a (snapshot id or label) is required"}
        try:
            limit = max(1, int(params.get("limit", 50)))
        except (TypeError, ValueError):
            limit = 50
        try:
            project_dir = self._project_dir(params)
            a_name, a_files = self._side(project_dir, str(a_key))
            b_name, b_files = self._side(project_dir, str(b_key))
        except (ValueError, LookupError) as exc:
            return {"success": False, "message": str(exc)}

        file_changes = []
        for rel in sorted(set(a_files) | set(b_files)):
            pa, pb = a_files.get(rel), b_files.get(rel)
            if pa is None or pb is None:
                file_changes.append({"path": rel, "status": "added" if pa is None else "removed"})
            elif _sha256(pa) != _sha256(pb):
                file_changes.append({"path": rel, "status": "modified"})

        out: Dict[str, Any] = {
            "success": True,
            "a": a_name,
            "b": b_name,
            "files": file_changes,
        }
        # Board: the .kicad_pcb both sides hold (the project's own first).
        boards = sorted(r for r in set(a_files) & set(b_files) if r.endswith(".kicad_pcb"))
        stem = project_dir.name
        boards.sort(key=lambda r: (Path(r).stem != stem, r))
        if boards:
            rel = boards[0]
            if any(f["path"] == rel for f in file_changes):
                ba = board_summary(a_files[rel].read_text(encoding="utf-8", errors="replace"))
                bb = board_summary(b_files[rel].read_text(encoding="utf-8", errors="replace"))
                out["board"] = {"file": rel, **diff_boards(ba, bb, limit)}
            else:
                out["board"] = {"file": rel, "unchanged": True}
        sch_a = {
            r: p.read_text(encoding="utf-8", errors="replace")
            for r, p in a_files.items()
            if r.endswith(".kicad_sch")
        }
        sch_b = {
            r: p.read_text(encoding="utf-8", errors="replace")
            for r, p in b_files.items()
            if r.endswith(".kicad_sch")
        }
        if sch_a or sch_b:
            if any(f["path"].endswith(".kicad_sch") for f in file_changes):
                # Compare only sheets both sides hold, so a partial (auto)
                # snapshot is not read as "every other sheet was deleted".
                common = set(sch_a) & set(sch_b)
                if set(sch_a) != set(sch_b) and common:
                    sch_a = {r: t for r, t in sch_a.items() if r in common}
                    sch_b = {r: t for r, t in sch_b.items() if r in common}
                out["schematic"] = diff_schematics(
                    schematic_summary(sch_a), schematic_summary(sch_b), limit
                )
            else:
                out["schematic"] = {"unchanged": True}
        parts = [f"{len(file_changes)} file(s) differ"]
        board = out.get("board") or {}
        if "footprints" in board:
            fp = board["footprints"]
            parts.append(
                f"footprints +{len(fp['added'])} -{len(fp['removed'])} moved {fp['movedCount']}, "
                f"tracks {board['tracks']['delta']:+d}, vias {board['vias']['delta']:+d}, "
                f"zones changed {len(board['zones']['changed'])}"
            )
        sch = out.get("schematic") or {}
        if "symbols" in sch:
            s = sch["symbols"]
            parts.append(
                f"symbols +{len(s['added'])} -{len(s['removed'])} changed {s['changedCount']}"
            )
        out["message"] = f"{a_name} -> {b_name}: " + "; ".join(parts)
        return out
