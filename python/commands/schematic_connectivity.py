"""Hierarchy-wide schematic connectivity: power-driver checks, netlist assertions
and net renaming.

Three consumers share the code here:

* ``power_driver_warnings`` — the "power nets without PWR_FLAG" warning that the
  batch connect tools attach to their result (#6). It builds the nets of the
  whole sheet hierarchy from the files on disk (wires, junctions, every label
  kind, power symbols, sheet pins) and flags only nets that contain a
  ``power_in`` pin and no ``power_out`` pin. A PWR_FLAG has a ``power_out`` pin,
  so a flag anywhere on the net — on any sheet — clears the warning. Plain
  signal nets have no ``power_in`` pin and are never reported. kicad-cli's
  netlist cannot be used for this: it leaves out power symbols and PWR_FLAGs.
* ``assert_connectivity`` (S2) — checks intended pin->net assignments against
  the netlist ``kicad-cli sch export netlist`` produces, returning only the
  problems.
* ``relabel_net`` (S3) — renames one net's labels: global labels on every sheet,
  local and hierarchical labels only where they are that net, plus the matching
  sheet pins.

All coordinates are schematic millimetres, origin at the sheet's top-left
corner, Y growing downwards (KiCad's file convention).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import sexpdata
from sexpdata import Symbol

logger = logging.getLogger("kicad_interface")

_IU_PER_MM = 10000
_LABEL_KINDS = ("label", "global_label", "hierarchical_label")
_STORE_DIR = ".kicad-mcp"
_STORE_FILE = "connectivity.json"

Point = Tuple[int, int]


def _iu(x: Any, y: Any) -> Point:
    return (round(float(x) * _IU_PER_MM), round(float(y) * _IU_PER_MM))


def _tag(item: Any) -> Optional[str]:
    if isinstance(item, list) and item and isinstance(item[0], Symbol):
        return str(item[0])
    return None


def _child(item: list, name: str) -> Optional[list]:
    for part in item[1:]:
        if _tag(part) == name:
            return part
    return None


def _children(item: list, name: str) -> Iterable[list]:
    for part in item[1:]:
        if _tag(part) == name:
            yield part


def _prop(item: list, name: str) -> Optional[str]:
    for part in _children(item, "property"):
        if len(part) >= 3 and str(part[1]) == name:
            return str(part[2])
    return None


def _load_sexp(path: Path, cache: Dict[str, list]) -> list:
    key = str(Path(path).resolve())
    if key not in cache:
        cache[key] = sexpdata.loads(Path(path).read_text(encoding="utf-8"))
    return cache[key]


# ──────────────────────────────────────────────────────────────────────────
#  Project / hierarchy discovery
# ──────────────────────────────────────────────────────────────────────────
def find_project_file(start: Path) -> Optional[Path]:
    """The ``.kicad_pro`` in *start*'s directory or the nearest parent, or None."""
    start = Path(start).resolve()
    directory = start if start.is_dir() else start.parent
    for d in (directory, *directory.parents):
        pros = sorted(d.glob("*.kicad_pro"))
        if pros:
            # Prefer the project whose root schematic exists.
            for pro in pros:
                if pro.with_suffix(".kicad_sch").exists():
                    return pro
            return pros[0]
    return None


def _sheet_blocks(sexp: list, parent_dir: Path) -> List[Dict[str, Any]]:
    """Every ``(sheet ...)`` block: uuid, child file and its sheet pins."""
    blocks = []
    for item in sexp:
        if _tag(item) != "sheet":
            continue
        sheetfile = _prop(item, "Sheetfile") or _prop(item, "Sheet file")
        uuid_part = _child(item, "uuid")
        uid = str(uuid_part[1]) if uuid_part and len(uuid_part) > 1 else str(id(item))
        pins = []
        for pin in _children(item, "pin"):
            at = _child(pin, "at")
            if len(pin) > 1 and at is not None and len(at) >= 3:
                pins.append((str(pin[1]), _iu(at[1], at[2])))
        blocks.append(
            {
                "uuid": uid,
                "file": (parent_dir / sheetfile).resolve() if sheetfile else None,
                "pins": pins,
            }
        )
    return blocks


def hierarchy_instances(
    root: Path, cache: Dict[str, list], max_depth: int = 32
) -> List[Tuple[Tuple[str, ...], Path]]:
    """(instance path, file) for the root and every sheet instance below it.

    The instance path is the tuple of sheet uuids from the root, so a file used
    by two sheet symbols appears twice with distinct paths. Missing or
    recursive sheet files are skipped.
    """
    root = Path(root).resolve()
    out: List[Tuple[Tuple[str, ...], Path]] = []

    def walk(path: Tuple[str, ...], f: Path, ancestors: Tuple[str, ...]) -> None:
        out.append((path, f))
        if len(path) >= max_depth:
            return
        try:
            sexp = _load_sexp(f, cache)
        except Exception as e:  # unreadable sheet: report its own nets only
            logger.warning(f"Could not read sheet {f}: {e}")
            return
        for block in _sheet_blocks(sexp, f.parent):
            child = block["file"]
            if child is None or not child.exists() or str(child) in ancestors:
                continue
            walk(path + (block["uuid"],), child, ancestors + (str(child),))

    walk((), root, (str(root),))
    return out


def hierarchy_files(root: Path, cache: Optional[Dict[str, list]] = None) -> List[Path]:
    """Unique sheet files of the hierarchy rooted at *root* (root first)."""
    seen: List[Path] = []
    for _path, f in hierarchy_instances(root, cache if cache is not None else {}):
        if f not in seen:
            seen.append(f)
    return seen


def find_root_schematic(sch_path: Path, cache: Optional[Dict[str, list]] = None) -> Path:
    """The project's root schematic when *sch_path* belongs to its hierarchy.

    Falls back to *sch_path* itself (a standalone sheet, or a sheet the
    project's root does not reference).
    """
    sch_path = Path(sch_path).resolve()
    pro = find_project_file(sch_path)
    if pro is None:
        return sch_path
    root = pro.with_suffix(".kicad_sch").resolve()
    if not root.exists():
        return sch_path
    if root == sch_path:
        return root
    try:
        if sch_path in hierarchy_files(root, cache):
            return root
    except Exception as e:
        logger.debug(f"hierarchy scan of {root} failed: {e}")
    return sch_path


# ──────────────────────────────────────────────────────────────────────────
#  Per-sheet net graph
# ──────────────────────────────────────────────────────────────────────────
class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[Any, Any] = {}

    def find(self, a: Any) -> Any:
        self.parent.setdefault(a, a)
        root = a
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[a] != root:
            self.parent[a], a = root, self.parent[a]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


class _Group:
    __slots__ = ("pins", "global_names", "local_names", "hier_names", "sheet_pins")

    def __init__(self) -> None:
        self.pins: List[Dict[str, Any]] = []
        self.global_names: Set[str] = set()
        self.local_names: Set[str] = set()
        self.hier_names: Set[str] = set()
        self.sheet_pins: List[Tuple[str, str]] = []


def _lib_symbol_index(sexp: list) -> Dict[str, list]:
    lib = next((item for item in sexp if _tag(item) == "lib_symbols"), None)
    index: Dict[str, list] = {}
    if lib is None:
        return index
    for sym in lib[1:]:
        if _tag(sym) == "symbol" and len(sym) > 1:
            index[str(sym[1])] = sym
    return index


def _power_kind(sym_def: list) -> Optional[str]:
    """'global' / 'local' for a power symbol definition, None otherwise."""
    power = _child(sym_def, "power")
    if power is None:
        return None
    if len(power) > 1 and str(power[1]) == "local":
        return "local"
    return "global"


class SheetGraph:
    """Connectivity of one sheet file, independent of where it is instanced.

    Nodes are points in KiCad internal units. Wires join their end points; a
    wire end, label, sheet pin or junction lying on the interior of a wire joins
    that wire (a component pin on a wire interior does not, as in eeschema).
    Labels with the same name on one sheet are joined; global labels and power
    symbols carry a global name that the hierarchy pass joins across sheets.
    """

    def __init__(self, sexp: list, sheet_file: Path):
        from commands.pin_locator import PinLocator
        from commands.wire_dragger import WireDragger

        self.uf = _UnionFind()
        self.items: Dict[Point, _Group] = {}
        uf = self.uf

        segments: List[Tuple[Point, Point]] = []
        for item in sexp:
            if _tag(item) != "wire":
                continue
            pts = _child(item, "pts")
            if pts is None:
                continue
            xy = [p for p in pts[1:] if _tag(p) == "xy" and len(p) >= 3]
            if len(xy) >= 2:
                a, b = _iu(xy[0][1], xy[0][2]), _iu(xy[-1][1], xy[-1][2])
                segments.append((a, b))
                uf.union(a, b)

        horizontal: Dict[int, List[Tuple[int, int, Point]]] = {}
        vertical: Dict[int, List[Tuple[int, int, Point]]] = {}
        other: List[Tuple[Point, Point]] = []
        for a, b in segments:
            if a[1] == b[1]:
                horizontal.setdefault(a[1], []).append((min(a[0], b[0]), max(a[0], b[0]), a))
            elif a[0] == b[0]:
                vertical.setdefault(a[0], []).append((min(a[1], b[1]), max(a[1], b[1]), a))
            else:
                other.append((a, b))

        def join_interior(pt: Point) -> None:
            for lo, hi, a in horizontal.get(pt[1], ()):
                if lo < pt[0] < hi:
                    uf.union(pt, a)
            for lo, hi, a in vertical.get(pt[0], ()):
                if lo < pt[1] < hi:
                    uf.union(pt, a)
            for a, b in other:
                if pt in (a, b):
                    continue
                if (b[0] - a[0]) * (pt[1] - a[1]) != (b[1] - a[1]) * (pt[0] - a[0]):
                    continue
                if min(a[0], b[0]) <= pt[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= pt[1] <= max(
                    a[1], b[1]
                ):
                    uf.union(pt, a)

        for a, b in segments:
            join_interior(a)
            join_interior(b)

        def group_at(pt: Point) -> _Group:
            uf.find(pt)
            return self.items.setdefault(pt, _Group())

        same_name: Dict[str, Point] = {}

        for item in sexp:
            tag = _tag(item)
            if tag == "junction":
                at = _child(item, "at")
                if at is not None and len(at) >= 3:
                    join_interior(_iu(at[1], at[2]))
            elif tag in _LABEL_KINDS and len(item) > 1:
                at = _child(item, "at")
                if at is None or len(at) < 3:
                    continue
                pt = _iu(at[1], at[2])
                join_interior(pt)
                name = str(item[1])
                grp = group_at(pt)
                if tag == "global_label":
                    grp.global_names.add(name)
                else:
                    if tag == "label":
                        grp.local_names.add(name)
                    else:
                        grp.hier_names.add(name)
                    if name in same_name:
                        uf.union(pt, same_name[name])
                    else:
                        same_name[name] = pt
            elif tag == "sheet":
                uuid_part = _child(item, "uuid")
                uid = str(uuid_part[1]) if uuid_part and len(uuid_part) > 1 else str(id(item))
                for pin in _children(item, "pin"):
                    at = _child(pin, "at")
                    if len(pin) > 1 and at is not None and len(at) >= 3:
                        pt = _iu(at[1], at[2])
                        join_interior(pt)
                        group_at(pt).sheet_pins.append((uid, str(pin[1])))

        lib_index = _lib_symbol_index(sexp)
        for item in sexp:
            if _tag(item) != "symbol" or _child(item, "lib_id") is None:
                continue
            lib_name_part = _child(item, "lib_name")
            lib_id = str(_child(item, "lib_id")[1])
            key = str(lib_name_part[1]) if lib_name_part is not None else lib_id
            sym_def = lib_index.get(key) or lib_index.get(lib_id)
            if sym_def is None:
                continue
            at = _child(item, "at")
            if at is None or len(at) < 3:
                continue
            sx, sy = float(at[1]), float(at[2])
            rot = float(at[3]) if len(at) > 3 else 0.0
            mirror = _child(item, "mirror")
            mx = mirror is not None and len(mirror) > 1 and str(mirror[1]) == "x"
            my = mirror is not None and len(mirror) > 1 and str(mirror[1]) == "y"
            unit_part = _child(item, "unit")
            unit = int(unit_part[1]) if unit_part is not None and len(unit_part) > 1 else 1
            ref = _prop(item, "Reference") or "?"
            value = _prop(item, "Value") or ""
            power = _power_kind(sym_def)
            try:
                pin_defs = PinLocator.parse_symbol_definition(sym_def)
            except Exception:
                continue
            for num, pdef in pin_defs.items():
                if pdef.get("unit", 0) not in (0, unit):
                    continue
                wx, wy = WireDragger.pin_world_xy(pdef["x"], pdef["y"], sx, sy, rot, mx, my)
                pt = _iu(round(wx, 4), round(wy, 4))
                ptype = str(pdef.get("type", "passive"))
                grp = group_at(pt)
                grp.pins.append(
                    {"ref": ref, "pin": str(num), "type": ptype, "power_symbol": bool(power)}
                )
                # A power symbol's power_in pin names a net: global by default,
                # sheet-local for KiCad 10's "(power local)" symbols. A
                # PWR_FLAG's pin is power_out and names nothing.
                if power and ptype == "power_in" and value:
                    if power == "local":
                        grp.local_names.add(value)
                        if value in same_name:
                            uf.union(pt, same_name[value])
                        else:
                            same_name[value] = pt
                    else:
                        grp.global_names.add(value)

    def groups(self) -> Dict[Point, _Group]:
        """Merged item groups keyed by their union-find root."""
        merged: Dict[Point, _Group] = {}
        for pt, grp in self.items.items():
            root = self.uf.find(pt)
            tgt = merged.setdefault(root, _Group())
            tgt.pins.extend(grp.pins)
            tgt.global_names |= grp.global_names
            tgt.local_names |= grp.local_names
            tgt.hier_names |= grp.hier_names
            tgt.sheet_pins.extend(grp.sheet_pins)
        return merged

    def root_of(self, x_mm: float, y_mm: float) -> Optional[Point]:
        pt = _iu(x_mm, y_mm)
        if pt in self.items or pt in self.uf.parent:
            return self.uf.find(pt)
        return None


class HierarchyNets:
    """Nets of a whole sheet hierarchy, built from the files on disk."""

    def __init__(self, root: Path, cache: Optional[Dict[str, list]] = None):
        cache = cache if cache is not None else {}
        self.root = Path(root).resolve()
        self.instances = hierarchy_instances(self.root, cache)
        self.graphs: Dict[str, SheetGraph] = {}
        self.sheet_groups: Dict[str, Dict[Point, _Group]] = {}
        for _path, f in self.instances:
            key = str(f)
            if key in self.graphs:
                continue
            try:
                self.graphs[key] = SheetGraph(_load_sexp(f, cache), f)
                self.sheet_groups[key] = self.graphs[key].groups()
            except Exception as e:
                logger.warning(f"Connectivity of {f} not built: {e}")

        uf = _UnionFind()
        self.uf = uf
        file_of = {path: str(f) for path, f in self.instances}
        for path, f in self.instances:
            groups = self.sheet_groups.get(str(f), {})
            for gkey, grp in groups.items():
                node = (path, gkey)
                uf.find(node)
                for name in grp.global_names:
                    uf.union(node, ("G", name))
                for sheet_uuid, pin_name in grp.sheet_pins:
                    child_path = path + (sheet_uuid,)
                    child_file = file_of.get(child_path)
                    if child_file is None:
                        continue
                    for ckey, cgrp in self.sheet_groups.get(child_file, {}).items():
                        if pin_name in cgrp.hier_names:
                            uf.union(node, (child_path, ckey))

        nets: Dict[Any, Dict[str, Any]] = {}
        for path, f in self.instances:
            for gkey, grp in self.sheet_groups.get(str(f), {}).items():
                net = nets.setdefault(
                    uf.find((path, gkey)),
                    {"pins": [], "names": set(), "global_names": set()},
                )
                net["pins"].extend(grp.pins)
                net["names"] |= grp.local_names | grp.hier_names | grp.global_names
                net["global_names"] |= grp.global_names
        self.nets = nets

    def net_at(self, sheet_file: Path, x_mm: float, y_mm: float) -> List[Dict[str, Any]]:
        """Every hierarchy net touching (x, y) on any instance of *sheet_file*."""
        key = str(Path(sheet_file).resolve())
        graph = self.graphs.get(key)
        if graph is None:
            return []
        gkey = graph.root_of(x_mm, y_mm)
        if gkey is None:
            return []
        out = []
        for path, f in self.instances:
            if str(f) == key:
                net = self.nets.get(self.uf.find((path, gkey)))
                if net is not None and net not in out:
                    out.append(net)
        return out


def _undriven(net: Dict[str, Any]) -> bool:
    types = {p["type"] for p in net["pins"]}
    return "power_in" in types and "power_out" not in types


def power_driver_warnings(
    sch_path: Path, placed: List[Dict[str, Any]], root: Optional[Path] = None
) -> List[str]:
    """Warn about nets touched by *placed* that ERC will call undriven.

    *placed* entries carry ``net`` and ``position`` {x, y} (the batch_connect
    result rows). A net is reported only when, evaluated over the whole
    hierarchy, it holds a ``power_in`` pin and no ``power_out`` pin — PWR_FLAG
    and regulator outputs are ``power_out``. Never raises.
    """
    try:
        if not placed:
            return []
        cache: Dict[str, list] = {}
        sch_path = Path(sch_path).resolve()
        root = Path(root).resolve() if root else find_root_schematic(sch_path, cache)
        nets = HierarchyNets(root, cache)
        if str(sch_path) not in nets.graphs:
            nets = HierarchyNets(sch_path, cache)
        missing: Dict[str, Dict[str, Any]] = {}
        for row in placed:
            pos = row.get("position") or {}
            if "x" not in pos or "y" not in pos:
                continue
            for net in nets.net_at(sch_path, pos["x"], pos["y"]):
                if _undriven(net):
                    name = row.get("net") or (sorted(net["names"])[0] if net["names"] else "?")
                    missing.setdefault(str(name), net)
        if not missing:
            return []
        details = []
        for name in sorted(missing):
            sinks = [
                f"{p['ref']}/{p['pin']}"
                for p in missing[name]["pins"]
                if p["type"] == "power_in" and not p["power_symbol"]
            ]
            details.append(f"{name} (power_in: {', '.join(sinks[:4])})" if sinks else name)
        return [
            "Power nets without PWR_FLAG: "
            + "; ".join(details)
            + ". These nets have a power_in pin but no power_out pin anywhere in the "
            "hierarchy, so ERC will report them as undriven. Add a PWR_FLAG (or wire "
            "the source's power_out pin) to each."
        ]
    except Exception as e:
        logger.debug(f"power driver check skipped: {e}")
        return []


# ──────────────────────────────────────────────────────────────────────────
#  Label scope ("auto": local or global)
# ──────────────────────────────────────────────────────────────────────────
LABEL_SCOPES = {"local": "label", "global": "global_label"}


def _sheet_net_names(sexp: list) -> Dict[str, Set[str]]:
    """Names a sheet gives nets, by kind: global (global labels, global power
    symbols), local (labels, local power symbols) and hierarchical."""
    out: Dict[str, Set[str]] = {"global": set(), "local": set(), "hierarchical": set()}
    kind_of = {"global_label": "global", "label": "local", "hierarchical_label": "hierarchical"}
    for item in sexp:
        tag = _tag(item)
        if tag in kind_of and len(item) > 1:
            out[kind_of[tag]].add(str(item[1]))
    lib_index = _lib_symbol_index(sexp)
    for item in sexp:
        if _tag(item) != "symbol" or _child(item, "lib_id") is None:
            continue
        lib_name_part = _child(item, "lib_name")
        lib_id = str(_child(item, "lib_id")[1])
        key = str(lib_name_part[1]) if lib_name_part is not None else lib_id
        sym_def = lib_index.get(key) or lib_index.get(lib_id)
        power = _power_kind(sym_def) if sym_def is not None else None
        value = _prop(item, "Value")
        if power and value:
            out["global" if power == "global" else "local"].add(value)
    return out


def resolve_label_scope(
    sch_path: Path, net_names: Iterable[str], cache: Optional[Dict[str, list]] = None
) -> Dict[str, Dict[str, Any]]:
    """Pick a label kind per net for ``scope: "auto"``.

    A net whose name already appears on another sheet of the hierarchy (any
    label kind or a power symbol), or as a global label on this sheet, gets a
    ``global_label``; any other net a local ``label``. Each entry is
    ``{"labelType": ..., "sheets": [other sheet files using the name],
    "localOnly": [those where it is only a local/hierarchical label]}``. A
    global label does not join a same-named local label, so callers warn about
    ``localOnly`` sheets. Falls back to local labels when the hierarchy cannot
    be read.
    """
    cache = cache if cache is not None else {}
    names = list(dict.fromkeys(str(n) for n in net_names))
    out: Dict[str, Dict[str, Any]] = {
        n: {"labelType": "label", "sheets": [], "localOnly": []} for n in names
    }
    try:
        sch_path = Path(sch_path).resolve()
        root = find_root_schematic(sch_path, cache)
        per_file: Dict[str, Dict[str, Set[str]]] = {}
        for f in hierarchy_files(root, cache):
            try:
                per_file[str(f)] = _sheet_net_names(_load_sexp(f, cache))
            except Exception as e:
                logger.debug(f"label scope: cannot read {f}: {e}")
        here = per_file.get(str(sch_path)) or _sheet_net_names(_load_sexp(sch_path, cache))
    except Exception as e:
        logger.warning(f"label scope auto: hierarchy not readable, using local labels: {e}")
        return out
    for name in names:
        entry = out[name]
        for sheet_key, kinds in per_file.items():
            if sheet_key == str(sch_path):
                continue
            if name in kinds["global"]:
                entry["sheets"].append(sheet_key)
            elif name in kinds["local"] or name in kinds["hierarchical"]:
                entry["sheets"].append(sheet_key)
                entry["localOnly"].append(sheet_key)
        if entry["sheets"] or name in here["global"]:
            entry["labelType"] = "global_label"
    return out


# ──────────────────────────────────────────────────────────────────────────
#  kicad-cli netlist
# ──────────────────────────────────────────────────────────────────────────
def export_netlist(root_sch: Path, timeout: int = 120) -> Dict[str, Any]:
    """Run ``kicad-cli sch export netlist --format kicadxml`` and parse it.

    Arguments go to kicad-cli as a list (no shell), so nothing is mangled by
    MSYS path conversion. Returns {"nets": {name: [node...]}, "components":
    {ref: {...}}}; each node is {ref, pin, pinfunction, pintype}. Raises
    RuntimeError with kicad-cli's message on failure.
    """
    from utils.kicad_cli import kicad_cli_not_found_message, resolve_kicad_cli

    cli = resolve_kicad_cli()
    if not cli:
        raise FileNotFoundError(kicad_cli_not_found_message())
    fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="kicad_mcp_netlist_")
    os.close(fd)
    try:
        cmd = [
            cli,
            "sch",
            "export",
            "netlist",
            "--format",
            "kicadxml",
            "--output",
            tmp,
            str(root_sch),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not Path(tmp).exists() or Path(tmp).stat().st_size == 0:
            raise RuntimeError(
                f"kicad-cli netlist export failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return parse_netlist_xml(Path(tmp).read_text(encoding="utf-8"))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def parse_netlist_xml(text: str) -> Dict[str, Any]:
    root = ET.fromstring(text)
    components: Dict[str, Dict[str, Any]] = {}
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        lib = comp.find("libsource")
        components[ref] = {
            "value": comp.findtext("value", ""),
            "footprint": comp.findtext("footprint", ""),
            "lib": f"{lib.get('lib')}:{lib.get('part')}" if lib is not None else "",
        }
    nets: Dict[str, List[Dict[str, str]]] = {}
    for net in root.findall("./nets/net"):
        nodes = [
            {
                "ref": n.get("ref", ""),
                "pin": n.get("pin", ""),
                "pinfunction": n.get("pinfunction", ""),
                "pintype": n.get("pintype", ""),
            }
            for n in net.findall("node")
        ]
        nets[net.get("name", "")] = nodes
    return {"nets": nets, "components": components}


def _leaf(name: str) -> str:
    return name.rstrip("/").rsplit("/", 1)[-1] if "/" in name else name


def net_names_match(expected: str, actual: str) -> bool:
    """Full match, or match on the last hierarchical path component."""
    if expected == actual:
        return True
    return _leaf(expected) == _leaf(actual)


_NC_VALUES = {"", "nc", "no_connect", "noconnect", "unconnected"}


def _is_nc_expectation(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in _NC_VALUES)


def _is_unconnected_net(name: str, nodes: List[Dict[str, str]]) -> bool:
    return _leaf(name).startswith("unconnected-") or (
        len(nodes) == 1 and "no_connect" in nodes[0].get("pintype", "")
    )


def _store_path(project_dir: Path) -> Path:
    return Path(project_dir) / _STORE_DIR / _STORE_FILE


def _resolve_targets(params: Dict[str, Any]) -> Tuple[Path, Path]:
    """(root schematic, project directory) from schematicPath / projectPath."""
    sch = params.get("schematicPath")
    proj = params.get("projectPath")
    if sch:
        sch_path = Path(sch)
        if not sch_path.exists():
            raise FileNotFoundError(f"Schematic not found: {sch}")
        root = find_root_schematic(sch_path)
        pro = find_project_file(sch_path)
        return root, (pro.parent if pro else sch_path.resolve().parent)
    if proj:
        p = Path(proj)
        if p.is_dir():
            pro = find_project_file(p / "x")
            if pro is None:
                raise FileNotFoundError(f"No .kicad_pro in {proj}")
        else:
            pro = p if p.suffix == ".kicad_pro" else p.with_suffix(".kicad_pro")
        root = pro.with_suffix(".kicad_sch")
        if not root.exists():
            raise FileNotFoundError(f"Root schematic not found: {root}")
        return root.resolve(), pro.resolve().parent
    raise ValueError("schematicPath or projectPath is required")


def assert_connectivity(params: Dict[str, Any]) -> Dict[str, Any]:
    """Compare intended pin->net assignments with kicad-cli's netlist (S2)."""
    try:
        root, project_dir = _resolve_targets(params)
    except (FileNotFoundError, ValueError) as e:
        return {"success": False, "message": str(e)}

    store = _store_path(project_dir)
    expectations: Dict[str, Dict[str, Any]] = {}
    use_stored = params.get("useStored")
    if use_stored:
        set_name = use_stored if isinstance(use_stored, str) else "default"
        try:
            stored = json.loads(store.read_text(encoding="utf-8"))
            stored_set = stored.get("sets", {}).get(set_name)
        except (OSError, ValueError):
            stored_set = None
        if stored_set is None:
            return {
                "success": False,
                "message": f"No stored expectation set '{set_name}' in {store}",
            }
        for ref, pins in stored_set.items():
            expectations.setdefault(ref, {}).update({str(k): v for k, v in pins.items()})
    given = params.get("expectations") or {}
    if not isinstance(given, dict):
        return {"success": False, "message": "expectations must be {ref: {pin: net}}"}
    for ref, pins in given.items():
        if not isinstance(pins, dict):
            return {"success": False, "message": f"expectations['{ref}'] must be {{pin: net}}"}
        expectations.setdefault(str(ref), {}).update({str(k): v for k, v in pins.items()})
    if not expectations:
        return {"success": False, "message": "expectations (or useStored) is required"}

    saved_to = None
    save_as = params.get("saveAs")
    if save_as:
        set_name = save_as if isinstance(save_as, str) else "default"
        try:
            data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
        except (OSError, ValueError):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("sets", {})[set_name] = expectations
        store.parent.mkdir(parents=True, exist_ok=True)
        tmp = store.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, store)
        saved_to = {"path": str(store), "set": set_name}

    try:
        netlist = export_netlist(root, timeout=int(params.get("timeout", 120)))
    except FileNotFoundError as e:
        return {"success": False, "message": str(e), "savedTo": saved_to}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "kicad-cli netlist export timed out"}
    except Exception as e:
        return {"success": False, "message": str(e), "savedTo": saved_to}

    # Index nodes by (ref, pin number) and (ref, pin function).
    by_pin: Dict[Tuple[str, str], Tuple[str, Dict[str, str]]] = {}
    by_func: Dict[Tuple[str, str], Tuple[str, Dict[str, str]]] = {}
    for net_name, nodes in netlist["nets"].items():
        for node in nodes:
            by_pin[(node["ref"], node["pin"])] = (net_name, node)
            func = node.get("pinfunction") or ""
            if func:
                by_func.setdefault((node["ref"], func), (net_name, node))
                stripped = re.sub(r"_" + re.escape(node["pin"]) + r"$", "", func)
                by_func.setdefault((node["ref"], stripped), (net_name, node))

    mismatches: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    unexpected_nc: List[Dict[str, Any]] = []
    checked = 0
    for ref, pins in sorted(expectations.items()):
        if ref not in netlist["components"]:
            missing.append({"ref": ref, "reason": "component not in netlist"})
            continue
        for pin, expected in pins.items():
            hit = by_pin.get((ref, pin)) or by_func.get((ref, pin))
            if hit is None:
                missing.append({"ref": ref, "pin": pin, "reason": "pin not in netlist"})
                continue
            checked += 1
            net_name, node = hit
            nodes = netlist["nets"][net_name]
            unconnected = _is_unconnected_net(net_name, nodes)
            nc_flag = "no_connect" in node.get("pintype", "")
            if _is_nc_expectation(expected):
                if not unconnected:
                    mismatches.append(
                        {
                            "ref": ref,
                            "pin": node["pin"],
                            "pinfunction": node.get("pinfunction", ""),
                            "expected": "NC",
                            "actual": net_name,
                        }
                    )
                continue
            if nc_flag:
                unexpected_nc.append(
                    {
                        "ref": ref,
                        "pin": node["pin"],
                        "pinfunction": node.get("pinfunction", ""),
                        "expected": expected,
                        "net": net_name,
                    }
                )
            if unconnected or not net_names_match(str(expected), net_name):
                mismatches.append(
                    {
                        "ref": ref,
                        "pin": node["pin"],
                        "pinfunction": node.get("pinfunction", ""),
                        "expected": expected,
                        "actual": "unconnected" if unconnected else net_name,
                    }
                )

    listed = set(expectations)
    nc_expected = {
        (ref, pin)
        for ref, pins in expectations.items()
        for pin, v in pins.items()
        if _is_nc_expectation(v)
    }
    single_pin: List[Dict[str, Any]] = []
    for net_name, nodes in netlist["nets"].items():
        touching = [n for n in nodes if n["ref"] in listed]
        if not touching:
            continue
        if len(nodes) == 1 and not _is_unconnected_net(net_name, nodes):
            n = nodes[0]
            if (n["ref"], n["pin"]) not in nc_expected:
                single_pin.append({"net": net_name, "ref": n["ref"], "pin": n["pin"]})
        # A no-connect flag on a pin that is wired to something else.
        if len(nodes) > 1:
            for n in touching:
                if "no_connect" in n.get("pintype", "") and not any(
                    u["ref"] == n["ref"] and u["pin"] == n["pin"] for u in unexpected_nc
                ):
                    unexpected_nc.append(
                        {
                            "ref": n["ref"],
                            "pin": n["pin"],
                            "pinfunction": n.get("pinfunction", ""),
                            "net": net_name,
                            "reason": "no-connect flag on a connected pin",
                        }
                    )

    passed = not (mismatches or missing or single_pin or unexpected_nc)
    result: Dict[str, Any] = {
        "success": True,
        "passed": passed,
        "message": (
            f"{checked} pin(s) checked: {len(mismatches)} mismatch(es), "
            f"{len(missing)} missing, {len(single_pin)} single-pin net(s), "
            f"{len(unexpected_nc)} unexpected no-connect(s)"
        ),
        "checkedPins": checked,
        "mismatches": mismatches,
        "missing": missing,
        "singlePinNets": single_pin,
        "unexpectedNoConnects": unexpected_nc,
        "rootSchematic": str(root),
    }
    if saved_to:
        result["savedTo"] = saved_to
    return result


# ──────────────────────────────────────────────────────────────────────────
#  relabel_net
# ──────────────────────────────────────────────────────────────────────────
def _sheet_name(item: list) -> str:
    return _prop(item, "Sheetname") or _prop(item, "Sheet name") or ""


def _resolve_sheet_path(root: Path, names: List[str], cache: Dict[str, list]) -> Optional[Path]:
    """The file of the sheet instance ``/names[0]/names[1]/...`` below *root*."""
    current = Path(root).resolve()
    for name in names:
        try:
            sexp = _load_sexp(current, cache)
        except Exception:
            return None
        nxt = None
        for item in sexp:
            if _tag(item) != "sheet" or _sheet_name(item) != name:
                continue
            sheetfile = _prop(item, "Sheetfile") or _prop(item, "Sheet file")
            if sheetfile:
                nxt = (current.parent / sheetfile).resolve()
            break
        if nxt is None or not nxt.exists():
            return None
        current = nxt
    return current


def _sheet_name_paths(root: Path, cache: Dict[str, list], max_depth: int = 32) -> Dict[str, str]:
    """{file: "/A/B/"} -- the first sheet-name path that instances each file."""
    out: Dict[str, str] = {}

    def walk(f: Path, prefix: str, depth: int) -> None:
        if str(f) in out or depth > max_depth:
            return
        out[str(f)] = prefix
        try:
            sexp = _load_sexp(f, cache)
        except Exception:
            return
        for item in sexp:
            if _tag(item) != "sheet":
                continue
            sheetfile = _prop(item, "Sheetfile") or _prop(item, "Sheet file")
            if sheetfile:
                child = (f.parent / sheetfile).resolve()
                if child.exists():
                    walk(child, f"{prefix}{_sheet_name(item)}/", depth + 1)

    walk(Path(root).resolve(), "/", 0)
    return out


def _label_points(sexp: list, name: str) -> List[Tuple[str, float, float]]:
    """(kind, x_mm, y_mm) of every label of any kind whose text is *name*."""
    out = []
    for item in sexp:
        tag = _tag(item)
        if tag in _LABEL_KINDS and len(item) > 1 and str(item[1]) == name:
            at = _child(item, "at")
            if at is not None and len(at) >= 3:
                out.append((tag, float(at[1]), float(at[2])))
    return out


def _relabel_scope(
    files: List[Path],
    cache: Dict[str, list],
    old: str,
    start: Path,
    include_power: bool,
) -> Set[str]:
    """Sheet files on which the local/hierarchical labels ``old`` are the renamed net.

    A local label is sheet-scoped: same-named local labels on two sheets are two
    nets. The renamed net starts on *start*; it continues
    * down a sheet pin named ``old`` that is wired into it (the child's
      hierarchical label is the same net),
    * up from a renamed hierarchical label to the parent sheet pin, and on into
      the parent when that pin is wired to the parent's label ``old``,
    * onto any sheet where a label ``old`` is wired to a global label ``old``
      (or, with *include_power*, a power symbol ``old``) -- those are renamed
      hierarchy-wide, so a local label on the same wire is the same net.
    """
    graphs: Dict[str, SheetGraph] = {}
    groups: Dict[str, Dict[Point, _Group]] = {}
    blocks: Dict[str, Dict[str, Path]] = {}
    points: Dict[str, List[Tuple[str, float, float]]] = {}
    for f in files:
        key = str(f)
        try:
            sexp = _load_sexp(f, cache)
            graphs[key] = SheetGraph(sexp, f)
            groups[key] = graphs[key].groups()
        except Exception as e:
            logger.warning(f"relabel_net: connectivity of {f} not built: {e}")
            continue
        blocks[key] = {
            b["uuid"]: b["file"] for b in _sheet_blocks(sexp, f.parent) if b["file"] is not None
        }
        points[key] = _label_points(sexp, old)

    scope: Set[str] = {str(Path(start).resolve())}
    while True:
        before = set(scope)
        hier_files = {
            k for k in scope if any(kind == "hierarchical_label" for kind, *_ in points.get(k, []))
        }
        for key, graph in graphs.items():
            roots: Set[Point] = set()
            for kind, x, y in points.get(key, []):
                if kind == "global_label" or key in scope:
                    root = graph.root_of(x, y)
                    if root is not None:
                        roots.add(root)
            for gkey, grp in groups[key].items():
                if include_power and old in grp.global_names:
                    roots.add(gkey)
                for uid, pin in grp.sheet_pins:
                    child = blocks[key].get(uid)
                    if pin == old and child is not None and str(child) in hier_files:
                        roots.add(gkey)
            for gkey in roots:
                net_grp = groups[key].get(gkey)
                if net_grp is None:
                    continue
                if old in net_grp.local_names or old in net_grp.hier_names:
                    scope.add(key)
                for uid, pin in net_grp.sheet_pins:
                    child = blocks[key].get(uid)
                    if pin == old and child is not None and str(child) in graphs:
                        scope.add(str(child))
        if scope == before:
            return scope


def relabel_net(params: Dict[str, Any]) -> Dict[str, Any]:
    """Rename one net's labels across the hierarchy (S3).

    ``oldName`` is a label text (``+5V``) or a KiCad net path (``/Power/+5V``,
    ``/+5V`` for the root sheet). Local labels are sheet-scoped, so they are
    renamed only on the sheet the path names (or, for a plain name, the given
    ``schematicPath``) and on sheets the same net reaches: through a sheet pin
    and its hierarchical label, or by being wired to a renamed global label.
    Same-named local labels on other sheets are different nets and are left
    alone (reported in ``untouchedLocalLabels``). Global labels named
    ``oldName`` are renamed on every sheet, as are, with
    ``includePowerSymbols``, power symbols. A renamed hierarchical label takes
    the parent sheet pins bound to it along, so the sheet interface stays
    intact. ``sheetOnly`` limits every label rename to the given sheet. Edits
    are textual, so the rest of each file is untouched.
    """
    from utils.sexpr_format import escape_sexpr_string

    sch = params.get("schematicPath")
    old = params.get("oldName")
    new = params.get("newName")
    dry_run = bool(params.get("dryRun", False))
    include_power = bool(params.get("includePowerSymbols", False))
    if not sch:
        return {"success": False, "message": "schematicPath is required"}
    if not old or not new:
        return {"success": False, "message": "oldName and newName are required"}
    sch_path = Path(sch)
    if not sch_path.exists():
        return {"success": False, "message": f"Schematic not found: {sch}"}
    old_leaf = _leaf(str(old))
    new_name = str(new)
    if "/" in new_name:
        return {
            "success": False,
            "message": "newName must be a plain label text (no hierarchical '/' path)",
        }
    if old_leaf == new_name:
        return {"success": False, "message": "oldName and newName are the same"}

    cache: Dict[str, list] = {}
    root = find_root_schematic(sch_path, cache)
    files = hierarchy_files(root, cache)

    # Where the local net lives: the sheet a "/Sheet/Sub/NAME" path names, else
    # the sheet the caller gave.
    start = sch_path.resolve()
    old_str = str(old)
    if old_str.startswith("/"):
        sheet_names = [p for p in old_str.rstrip("/").split("/")[1:-1] if p]
        resolved = _resolve_sheet_path(root, sheet_names, cache)
        if resolved is None:
            return {
                "success": False,
                "message": (
                    f"Sheet path '/{'/'.join(sheet_names)}' of '{old_str}' not found in the "
                    f"hierarchy of {root.name} (sheet names, as in KiCad's net names)"
                ),
                "rootSchematic": str(root),
            }
        start = resolved

    old_q = re.escape(escape_sexpr_string(old_leaf))
    new_raw = escape_sexpr_string(new_name)
    local_re = re.compile(r'(\((label|hierarchical_label)\s+)"' + old_q + '"')
    global_re = re.compile(r'(\((global_label)\s+)"' + old_q + '"')
    contents = {str(f): f.read_text(encoding="utf-8") for f in files}

    # sheetOnly: every label kind on the given sheet only.
    only = str(sch_path.resolve()) if params.get("sheetOnly") else None
    if only:
        scope = {only}
    else:
        scope = _relabel_scope(files, cache, old_leaf, start, include_power)
    # A child whose hierarchical labels are renamed -> its parent sheet pins follow.
    hier_files = {
        k
        for k in scope
        if k in contents
        and re.search(r'\(hierarchical_label\s+"' + old_q + '"', contents[k]) is not None
    }

    changes: List[Dict[str, Any]] = []
    untouched: List[Dict[str, Any]] = []
    total = 0
    for f in files:
        key = str(f)
        text = contents[key]
        counts = {k: 0 for k in _LABEL_KINDS}

        def _sub_label(m: "re.Match[str]") -> str:
            counts[m.group(2)] += 1
            return m.group(1) + '"' + new_raw + '"'

        new_text = text
        if key in scope:
            new_text = local_re.sub(_sub_label, new_text)
        elif not only:
            n_local = len(local_re.findall(text))
            if n_local:
                untouched.append({"file": key, "labels": n_local})
        if not only or key == only:
            new_text = global_re.sub(_sub_label, new_text)

        sheet_pins = 0
        if hier_files:
            new_text, sheet_pins = _rename_sheet_pins(
                new_text, f.parent, hier_files, old_q, new_raw
            )

        power = 0
        if include_power and (not only or key == only):
            new_text, power = _rename_power_values(new_text, escape_sexpr_string(old_leaf), new_raw)

        n = sum(counts.values()) + sheet_pins + power
        if n:
            total += n
            entry = {"file": key, **{k: v for k, v in counts.items() if v}}
            if sheet_pins:
                entry["sheet_pin"] = sheet_pins
            if power:
                entry["power_symbol"] = power
            changes.append(entry)
            if not dry_run:
                tmp = f.with_name(f.name + ".relabel.tmp")
                tmp.write_text(new_text, encoding="utf-8")
                os.replace(tmp, f)

    note = ""
    if untouched:
        name_paths = _sheet_name_paths(root, cache)
        for u in untouched:
            u["netPath"] = name_paths.get(u["file"], "/") + old_leaf
        note = (
            f"Local/hierarchical labels '{old_leaf}' on "
            + ", ".join(f"{Path(u['file']).name} (net {u['netPath']})" for u in untouched)
            + " belong to other nets (local labels are per sheet) and were not renamed. "
            "To rename one, pass its net path as oldName or its sheet as schematicPath."
        )
    if total == 0:
        out: Dict[str, Any] = {
            "success": False,
            "message": f"No label of net '{old_str}' found in the hierarchy of {root.name}",
            "rootSchematic": str(root),
            "sheetsScanned": len(files),
        }
        if untouched:
            out["untouchedLocalLabels"] = untouched
            out["message"] += ". " + note
        return out
    result: Dict[str, Any] = {
        "success": True,
        "dryRun": dry_run,
        "message": (
            f"{'Would rename' if dry_run else 'Renamed'} {total} item(s) "
            f"'{old_leaf}' -> '{new_name}' in {len(changes)} sheet(s)"
        ),
        "renamed": total,
        "changes": changes,
        "rootSchematic": str(root),
        "sheetsScanned": len(files),
    }
    if untouched:
        result["untouchedLocalLabels"] = untouched
        result["warnings"] = [note]
    return result


def _find_close(text: str, start: int) -> int:
    depth = 0
    i = start
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _rename_sheet_pins(
    text: str, parent_dir: Path, hier_files: Set[str], old_q: str, new_raw: str
) -> Tuple[str, int]:
    """Rename ``(pin "old" ...)`` inside sheet blocks whose child file was relabelled."""
    out = []
    pos = 0
    count = 0
    pin_re = re.compile(
        r'(\(pin\s+)"' + old_q + r'"(\s+(?:input|output|bidirectional|' r"tri_state|passive))"
    )
    file_re = re.compile(r'\(property\s+"Sheet\s?file"\s+"([^"]*)"')
    for m in re.finditer(r"\(sheet\b(?!_)", text):
        start = m.start()
        if start < pos:
            continue
        end = _find_close(text, start)
        if end < 0:
            break
        block = text[start : end + 1]
        fm = file_re.search(block)
        child = str((parent_dir / fm.group(1)).resolve()) if fm else None
        if child in hier_files:
            block, n = pin_re.subn(
                lambda mm: mm.group(1) + '"' + new_raw + '"' + mm.group(2), block
            )
            count += n
        out.append(text[pos:start])
        out.append(block)
        pos = end + 1
    out.append(text[pos:])
    return "".join(out), count


def _rename_power_values(text: str, old_esc: str, new_esc: str) -> Tuple[str, int]:
    """Rename the Value of placed power symbols (``#PWR``/``#FLG`` excluded: PWR_FLAG)."""
    count = 0
    out = []
    pos = 0
    lib_start = text.find("(lib_symbols")
    lib_end = _find_close(text, lib_start) if lib_start >= 0 else -1
    value_re = re.compile(r'(\(property\s+"Value"\s+)"' + re.escape(old_esc) + '"')
    for m in re.finditer(r"\(symbol\s+\(", text):
        start = m.start()
        if start < pos or (lib_start >= 0 and lib_start <= start <= lib_end):
            continue
        end = _find_close(text, start)
        if end < 0:
            break
        block = text[start : end + 1]
        if re.search(r'\(property\s+"Reference"\s+"#PWR', block):
            block, n = value_re.subn(lambda mm: mm.group(1) + '"' + new_esc + '"', block, count=1)
            count += n
        out.append(text[pos:start])
        out.append(block)
        pos = end + 1
    out.append(text[pos:])
    return "".join(out), count
