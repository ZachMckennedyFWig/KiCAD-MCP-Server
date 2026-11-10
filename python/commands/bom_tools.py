"""Bulk property / BOM mapping across a schematic hierarchy (MCP_ISSUES S4).

``apply_property_map`` replaces the per-project ``lcsc_map.py`` script: a list
of rules, each ``{match: {...}, set: {...}}``, is applied to every placed
symbol in every sheet reachable from the root schematic. It is idempotent (a
second run changes nothing), all-or-nothing across sheets, and reports which
parts no rule matched and which BOM parts still have no LCSC number.

The same symbol reader backs ``collect_parts`` which ``jlc_bom_report`` and
``fab_package`` use to build a BOM from the schematic without kicad-cli.

Edits are made on the file text (principle 2 of MCP_ISSUES §2.1): only the
property value token or flag token that changes is touched, new properties
copy the indentation of the block they go into, and each sheet is written with
an atomic replace.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.file_io import FileTransaction, read_text_preserve_newline, write_text_atomic
from utils.sexpr_format import (
    escape_sexpr_string,
    iter_child_offsets,
    match_paren,
    unescape_sexpr_string,
)
from utils.sheet_tree import sheet_tree

logger = logging.getLogger("kicad_interface")

_Q = r'"((?:[^"\\]|\\.)*)"'
_SYMBOL_HEAD = re.compile(r"\(symbol\s*\(")
_LIB_ID = re.compile(r"\(lib_id\s+" + _Q)
_PROPERTY = re.compile(r"\(property\s+" + _Q + r"\s+" + _Q)
_FLAG = re.compile(r"\((dnp|in_bom|on_board|exclude_from_sim|in_pos_files)\s+(yes|no)\s*\)")
_AT = re.compile(r"\(at\s+([-\d.eE+]+)\s+([-\d.eE+]+)")
_UNIT = re.compile(r"\(unit\s+(\d+)\s*\)")
_INSTANCES = re.compile(r"\(instances[\s(]")
_PROJECT = re.compile(r"\(project\s+" + _Q)
_REFERENCE = re.compile(r"\(reference\s+" + _Q + r"\s*\)")

#: Symbol flags a rule may set. KiCad writes them as ``(name yes|no)``.
FLAG_KEYS = ("dnp", "in_bom", "on_board", "exclude_from_sim", "in_pos_files")
#: Field names treated as "the LCSC number" when reading a part.
_LCSC_FIELD = re.compile(
    r"^\s*(lcsc|jlc|jlcpcb)(\s*[-_ ]?\s*(part|pn|p/n))?\s*(#|no\.?|number)?\s*$", re.I
)
#: Flag order KiCad uses, for inserting a flag a block does not have yet.
_FLAG_ORDER = ("exclude_from_sim", "in_bom", "on_board", "in_pos_files", "dnp")

_LIST_CAP = 100


# --------------------------------------------------------------------------- #
# Reading placed symbols
# --------------------------------------------------------------------------- #


def is_lcsc_field(name: str) -> bool:
    return bool(_LCSC_FIELD.match(name or ""))


def natural_key(ref: str) -> Tuple[str, int, str]:
    m = re.match(r"([^\d]*)(\d*)(.*)", ref or "")
    prefix, num, rest = m.groups() if m else (ref, "", "")
    return (prefix, int(num) if num else -1, rest)


def _children(block: str) -> List[Tuple[int, int]]:
    """(start, end) of each direct child list of *block* (end inclusive)."""
    out = []
    for off in iter_child_offsets(block):
        end = match_paren(block, off)
        if end != -1:
            out.append((off, end))
    return out


def _instance_refs(block: str, children: List[Tuple[int, int]], project: str) -> List[str]:
    for start, end in children:
        if not _INSTANCES.match(block, start):
            continue
        inst = block[start : end + 1]
        by_project: Dict[str, List[str]] = {}
        order: List[str] = []
        for p_off in iter_child_offsets(inst):
            m = _PROJECT.match(inst, p_off)
            if not m:
                continue
            p_end = match_paren(inst, p_off)
            name = unescape_sexpr_string(m.group(1))
            refs = [unescape_sexpr_string(r) for r in _REFERENCE.findall(inst[p_off:p_end])]
            by_project.setdefault(name, []).extend(refs)
            order.append(name)
        if project and project in by_project:
            return by_project[project]
        merged: List[str] = []
        for name in order:
            merged.extend(by_project[name])
        return merged
    return []


class PlacedSymbol:
    """One placed ``(symbol ...)`` block of a sheet, parsed from its text."""

    def __init__(self, sheet: Path, start: int, end: int, block: str, project: str):
        self.sheet = sheet
        self.start = start
        self.end = end
        self.block = block
        children = _children(block)
        self.lib_id = ""
        self.props: Dict[str, str] = {}
        self.flags: Dict[str, bool] = {}
        self.unit = "1"
        for c_start, c_end in children:
            head = block[c_start : c_start + 40]
            if head.startswith("(lib_id"):
                m = _LIB_ID.match(block, c_start)
                if m:
                    self.lib_id = unescape_sexpr_string(m.group(1))
            elif head.startswith("(property"):
                m = _PROPERTY.match(block, c_start)
                if m:
                    name = unescape_sexpr_string(m.group(1))
                    self.props.setdefault(name, unescape_sexpr_string(m.group(2)))
            elif head.startswith("(unit"):
                m = _UNIT.match(block, c_start)
                if m:
                    self.unit = m.group(1)
            else:
                m = _FLAG.match(block, c_start)
                if m:
                    self.flags[m.group(1)] = m.group(2) == "yes"
        refs = _instance_refs(block, children, project)
        if not refs and self.props.get("Reference"):
            refs = [self.props["Reference"]]
        # Keep order, drop duplicates (multi-unit entries repeat nothing here,
        # but a symbol listed under two project names would).
        seen = set()
        self.refs = [r for r in refs if not (r in seen or seen.add(r))]

    @property
    def value(self) -> str:
        return self.props.get("Value", "")

    @property
    def footprint(self) -> str:
        return self.props.get("Footprint", "")

    @property
    def is_power(self) -> bool:
        if self.lib_id.lower().startswith("power:"):
            return True
        refs = self.refs or [self.props.get("Reference", "")]
        return any(r.startswith("#") for r in refs)

    def lcsc(self) -> Tuple[Optional[str], str]:
        """(field name, value) of the LCSC-like field; (None, "") if absent."""
        for name, value in self.props.items():
            if is_lcsc_field(name):
                return name, value.strip()
        return None, ""

    def flag(self, name: str, default: bool) -> bool:
        return self.flags.get(name, default)


def project_name_for(root: Path) -> str:
    pros = sorted(Path(root).parent.glob("*.kicad_pro"))
    stem_pro = Path(root).with_suffix(".kicad_pro")
    if stem_pro.exists():
        return stem_pro.stem
    return pros[0].stem if len(pros) == 1 else ""


def read_sheet_symbols(sheet: Path, text: str, project: str) -> List[PlacedSymbol]:
    symbols: List[PlacedSymbol] = []
    for offset in iter_child_offsets(text):
        if not _SYMBOL_HEAD.match(text, offset):
            continue
        end = match_paren(text, offset)
        if end == -1:
            continue
        block = text[offset : end + 1]
        sym = PlacedSymbol(sheet, offset, end, block, project)
        if sym.lib_id:
            symbols.append(sym)
    return symbols


def load_hierarchy(root: Path) -> Tuple[List[Path], Dict[Path, Tuple[str, str]], str]:
    """Sheets of the design (root first) with their text and newline style."""
    root = Path(root)
    if not root.is_file():
        raise FileNotFoundError(f"Schematic not found: {root}")
    sheets = sheet_tree(root)
    texts: Dict[Path, Tuple[str, str]] = {}
    for sheet in sheets:
        texts[sheet] = read_text_preserve_newline(sheet)
    return sheets, texts, project_name_for(root)


def resolve_root_schematic(path: str) -> Path:
    """The root sheet for *path* (a sheet, a .kicad_pro, or a project dir)."""
    p = Path(path).expanduser()
    if p.is_dir():
        pros = sorted(p.glob("*.kicad_pro"))
        if len(pros) != 1:
            raise ValueError(f"{p} holds {len(pros)} .kicad_pro files; pass the root .kicad_sch")
        p = pros[0]
    if p.suffix == ".kicad_pro":
        p = p.with_suffix(".kicad_sch")
    if p.suffix != ".kicad_sch":
        raise ValueError(f"Not a schematic: {path}")
    if not p.is_file():
        raise FileNotFoundError(f"Schematic not found: {p}")
    try:
        from commands.schematic_connectivity import find_root_schematic

        return Path(find_root_schematic(p))
    except Exception as exc:  # pragma: no cover - fall back to the given sheet
        logger.debug(f"find_root_schematic failed for {p}: {exc}")
        return p


def collect_parts(root: Path) -> List[Dict[str, Any]]:
    """Every non-power part of the design, one entry per reference.

    ``{ref, value, footprint, lcsc, lcscField, lib_id, dnp, in_bom, on_board,
    fields, sheet}``. Units of a multi-unit part are merged; a part placed on a
    reused sheet appears once per instance reference.
    """
    sheets, texts, project = load_hierarchy(root)
    parts: Dict[str, Dict[str, Any]] = {}
    for sheet in sheets:
        text = texts[sheet][0]
        for sym in read_sheet_symbols(sheet, text, project):
            if sym.is_power:
                continue
            field, lcsc = sym.lcsc()
            for ref in sym.refs:
                if not ref or ref.endswith("?"):
                    continue
                entry = parts.get(ref)
                if entry is None:
                    parts[ref] = {
                        "ref": ref,
                        "value": sym.value,
                        "footprint": sym.footprint,
                        "lcsc": lcsc,
                        "lcscField": field,
                        "lib_id": sym.lib_id,
                        "dnp": sym.flag("dnp", False),
                        "in_bom": sym.flag("in_bom", True),
                        "on_board": sym.flag("on_board", True),
                        "fields": dict(sym.props),
                        "sheet": str(sheet),
                    }
                elif lcsc and not entry["lcsc"]:
                    entry["lcsc"], entry["lcscField"] = lcsc, field
    return sorted(parts.values(), key=lambda p: natural_key(p["ref"]))


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

_MATCH_ALIASES = {
    "value": "value",
    "values": "value",
    "valueRegex": "valueRegex",
    "value_regex": "valueRegex",
    "footprint_contains": "footprint_contains",
    "footprintContains": "footprint_contains",
    "ref_regex": "ref_regex",
    "refRegex": "ref_regex",
    "lib_id": "lib_id",
    "libId": "lib_id",
}
_FLAG_ALIASES = {
    "dnp": "dnp",
    "in_bom": "in_bom",
    "inBom": "in_bom",
    "on_board": "on_board",
    "onBoard": "on_board",
    "exclude_from_sim": "exclude_from_sim",
    "excludeFromSim": "exclude_from_sim",
    "in_pos_files": "in_pos_files",
    "inPosFiles": "in_pos_files",
}


class Rule:
    def __init__(self, index: int, raw: Any):
        if not isinstance(raw, dict):
            raise ValueError(f"rules[{index}] must be an object {{match, set}}")
        self.index = index
        match = raw.get("match")
        assign = raw.get("set")
        if not isinstance(match, dict) or not match:
            raise ValueError(f"rules[{index}].match must be a non-empty object")
        if not isinstance(assign, dict) or not assign:
            raise ValueError(f"rules[{index}].set must be a non-empty object")
        self.match: Dict[str, Any] = {}
        for key, val in match.items():
            canon = _MATCH_ALIASES.get(key)
            if canon is None:
                raise ValueError(
                    f"rules[{index}].match: unknown key '{key}' (use value, valueRegex, "
                    "footprint_contains, ref_regex, lib_id)"
                )
            if canon in ("valueRegex", "ref_regex"):
                try:
                    val = re.compile(str(val))
                except re.error as exc:
                    raise ValueError(f"rules[{index}].match.{key}: bad regex: {exc}")
            elif canon == "value":
                val = [str(v) for v in val] if isinstance(val, list) else [str(val)]
            else:
                val = str(val)
            self.match[canon] = val
        self.fields: Dict[str, str] = {}
        self.flags: Dict[str, bool] = {}
        for key, val in assign.items():
            canon = _FLAG_ALIASES.get(key)
            if canon:
                if not isinstance(val, bool):
                    raise ValueError(f"rules[{index}].set.{key} must be true or false")
                self.flags[canon] = val
                continue
            if key in ("Reference",):
                raise ValueError(f"rules[{index}].set: Reference cannot be set by a property map")
            if not isinstance(val, (str, int, float)) or isinstance(val, bool):
                raise ValueError(f"rules[{index}].set.{key} must be a string")
            self.fields[str(key)] = str(val)
        self.label = raw.get("name") or raw.get("label")

    def matches(self, sym: PlacedSymbol) -> bool:
        m = self.match
        if "value" in m and sym.value not in m["value"]:
            return False
        if "valueRegex" in m and not m["valueRegex"].fullmatch(sym.value):
            return False
        if "footprint_contains" in m and m["footprint_contains"].lower() not in (
            sym.footprint.lower()
        ):
            return False
        if "lib_id" in m and sym.lib_id != m["lib_id"]:
            return False
        if "ref_regex" in m and not any(m["ref_regex"].fullmatch(r) for r in sym.refs):
            return False
        return True


# --------------------------------------------------------------------------- #
# Text edits on one symbol block
# --------------------------------------------------------------------------- #


def _line_indent(text: str, offset: int) -> Optional[str]:
    """Whitespace before *offset* on its line, or None if not first on the line."""
    i = offset - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    if i >= 0 and text[i] != "\n":
        return None
    return text[i + 1 : offset]


def _find_property(block: str, name: str) -> Optional[Tuple[int, int, int, int]]:
    """(child start, child end, value start, value end) of property *name*."""
    for start, end in _children(block):
        m = _PROPERTY.match(block, start)
        if m and unescape_sexpr_string(m.group(1)) == name:
            return start, end, m.start(2), m.end(2)
    return None


def _set_property(block: str, name: str, value: str) -> str:
    found = _find_property(block, name)
    escaped = escape_sexpr_string(value)
    if found:
        _s, _e, v_start, v_end = found
        return block[:v_start] + escaped + block[v_end:]
    children = _children(block)
    props = [(s, e) for s, e in children if _PROPERTY.match(block, s)]
    anchor = props[-1] if props else (children[-1] if children else None)
    at = _AT.search(block)
    x, y = (at.group(1), at.group(2)) if at else ("0", "0")
    if anchor is None:
        insert_at = len(block) - 1
        indent = None
    else:
        insert_at = anchor[1] + 1
        indent = _line_indent(block, anchor[0])
    if indent is None:
        prop = (
            f' (property "{escape_sexpr_string(name)}" "{escaped}" (at {x} {y} 0) (hide yes)'
            " (effects (font (size 1.27 1.27))))"
        )
        return block[:insert_at] + prop + block[insert_at:]
    unit = "\t" if "\t" in indent or not indent else "  "
    i1, i2, i3 = indent + unit, indent + unit * 2, indent + unit * 3
    prop = (
        f'\n{indent}(property "{escape_sexpr_string(name)}" "{escaped}"'
        f"\n{i1}(at {x} {y} 0)\n{i1}(hide yes)\n{i1}(effects\n{i2}(font\n{i3}(size 1.27 1.27)"
        f"\n{i2})\n{i1})\n{indent})"
    )
    return block[:insert_at] + prop + block[insert_at:]


def _set_flag(block: str, flag: str, value: bool) -> str:
    token = "yes" if value else "no"
    children = _children(block)
    for start, end in children:
        m = _FLAG.match(block, start)
        if m and m.group(1) == flag:
            return block[: m.start(2)] + token + block[m.end(2) :]
    # Missing flag (older writers): insert it after the flag that precedes it
    # in KiCad's order, else after (unit ...)/(at ...).
    anchor = None
    wanted = _FLAG_ORDER.index(flag)
    for start, end in children:
        m = _FLAG.match(block, start)
        if m and _FLAG_ORDER.index(m.group(1)) < wanted:
            anchor = (start, end)
    if anchor is None:
        for start, end in children:
            head = block[start : start + 6]
            if head.startswith("(unit") or head.startswith("(at "):
                anchor = (start, end)
    if anchor is None:
        anchor = children[0] if children else None
    if anchor is None:
        return block[:-1] + f" ({flag} {token}))"
    indent = _line_indent(block, anchor[0])
    sep = f"\n{indent}" if indent is not None else " "
    return block[: anchor[1] + 1] + f"{sep}({flag} {token})" + block[anchor[1] + 1 :]


# --------------------------------------------------------------------------- #
# apply_property_map
# --------------------------------------------------------------------------- #


def _plan_symbol(
    sym: PlacedSymbol, rules: List[Rule], first_only: bool
) -> Tuple[List[int], Dict[str, str], Dict[str, bool]]:
    matched: List[int] = []
    fields: Dict[str, str] = {}
    flags: Dict[str, bool] = {}
    for rule in rules:
        if not rule.matches(sym):
            continue
        matched.append(rule.index)
        fields.update(rule.fields)
        flags.update(rule.flags)
        if first_only:
            break
    return matched, fields, flags


def _resolve_field_name(sym: PlacedSymbol, name: str) -> str:
    """``LCSC`` updates an existing LCSC-like field (``LCSC Part #``...) in place."""
    if name in sym.props:
        return name
    if is_lcsc_field(name):
        existing, _ = sym.lcsc()
        if existing:
            return existing
    return name


def apply_property_map(params: Dict[str, Any]) -> Dict[str, Any]:
    """Apply ``rules`` to every placed symbol of the hierarchy (S4)."""
    schematic_path = params.get("schematicPath") or params.get("projectPath")
    if not schematic_path:
        return {"success": False, "message": "schematicPath is required"}
    raw_rules = params.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        return {"success": False, "message": "rules must be a non-empty list"}
    dry_run = bool(params.get("dryRun", False))
    first_only = bool(params.get("firstMatchOnly", False))
    try:
        rules = [Rule(i, r) for i, r in enumerate(raw_rules)]
        root = resolve_root_schematic(str(schematic_path))
        sheets, texts, project = load_hierarchy(root)
    except (ValueError, FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        return {"success": False, "message": str(exc)}

    rule_hits: Dict[int, List[str]] = {r.index: [] for r in rules}
    changed: Dict[str, Dict[str, Any]] = {}
    unmatched: Dict[str, Dict[str, str]] = {}
    matched_refs: set = set()
    final_state: Dict[str, Dict[str, Any]] = {}
    new_texts: Dict[Path, str] = {}

    for sheet in sheets:
        text, _nl = texts[sheet]
        symbols = read_sheet_symbols(sheet, text, project)
        pieces: List[str] = []
        pos = 0
        sheet_changed = False
        for sym in symbols:
            if sym.is_power:
                continue
            refs = [r for r in sym.refs if r] or [sym.props.get("Reference", "?")]
            matched, fields, flags = _plan_symbol(sym, rules, first_only)
            for idx in matched:
                rule_hits[idx].extend(r for r in refs if r not in rule_hits[idx])
            block = sym.block
            changes: Dict[str, List[Any]] = {}
            for name, value in fields.items():
                target = _resolve_field_name(sym, name)
                old = sym.props.get(target)
                if old == value:
                    continue
                block = _set_property(block, target, value)
                changes[target] = [old, value]
            for flag, value in flags.items():
                default = flag != "dnp" and flag != "exclude_from_sim"
                old_flag = sym.flags.get(flag)
                if old_flag is not None and old_flag == value:
                    continue
                if old_flag is None and default == value:
                    # KiCad's default already applies; writing it is not a change
                    # anyone can observe, so leave the file alone (idempotent).
                    continue
                block = _set_flag(block, flag, value)
                changes[flag] = [old_flag, value]
            for ref in refs:
                if matched:
                    matched_refs.add(ref)
                    unmatched.pop(ref, None)
                elif ref not in matched_refs:
                    unmatched[ref] = {
                        "ref": ref,
                        "value": sym.value,
                        "footprint": sym.footprint,
                        "sheet": sheet.name,
                    }
            # State after the change, for the missing-LCSC report.
            after = PlacedSymbol(sheet, 0, len(block) - 1, block, project)
            for ref in refs:
                state = final_state.setdefault(
                    ref,
                    {
                        "ref": ref,
                        "value": after.value,
                        "footprint": after.footprint,
                        "lcsc": "",
                        "dnp": after.flag("dnp", False),
                        "in_bom": after.flag("in_bom", True),
                    },
                )
                lcsc = after.lcsc()[1]
                if lcsc:
                    state["lcsc"] = lcsc
            if changes:
                key = ",".join(refs)
                entry = changed.setdefault(
                    key, {"refs": refs, "sheet": sheet.name, "changes": {}, "units": 0}
                )
                entry["changes"].update(changes)
                entry["units"] += 1
                pieces.append(text[pos : sym.start])
                pieces.append(block)
                pos = sym.end + 1
                sheet_changed = True
        if sheet_changed:
            pieces.append(text[pos:])
            new_texts[sheet] = "".join(pieces)

    missing = [
        {k: s[k] for k in ("ref", "value", "footprint")}
        for s in final_state.values()
        if not s["lcsc"] and s["in_bom"] and not s["dnp"]
    ]
    missing.sort(key=lambda s: natural_key(s["ref"]))
    unmatched_list = sorted(unmatched.values(), key=lambda s: natural_key(s["ref"]))

    written: List[str] = []
    if new_texts and not dry_run:
        transactions = [FileTransaction(p) for p in new_texts]
        try:
            for sheet, new_text in new_texts.items():
                write_text_atomic(sheet, new_text, texts[sheet][1])
                written.append(str(sheet))
        except Exception as exc:
            for tx in transactions:
                try:
                    tx.rollback()
                except Exception:  # pragma: no cover - best effort
                    pass
            return {
                "success": False,
                "message": f"Writing failed; every sheet was restored: {exc}",
            }

    changed_list = sorted(changed.values(), key=lambda c: natural_key(c["refs"][0]))
    rules_report = []
    for rule in rules:
        hits = sorted(rule_hits[rule.index], key=natural_key)
        item: Dict[str, Any] = {"index": rule.index, "matched": len(hits), "refs": hits[:50]}
        if rule.label:
            item["name"] = rule.label
        if not hits:
            item["warning"] = "matched no part"
        rules_report.append(item)

    n_changed = len(changed_list)
    verb = "Would change" if dry_run else "Changed"
    message = (
        f"{verb} {n_changed} part(s) in {len(new_texts)} sheet(s); "
        f"{len(unmatched_list)} part(s) matched no rule; "
        f"{len(missing)} BOM part(s) without LCSC"
    )
    return {
        "success": True,
        "message": message,
        "dryRun": dry_run,
        "rootSchematic": str(root),
        "sheetsScanned": len(sheets),
        "sheetsChanged": [str(p) for p in new_texts] if dry_run else written,
        "changedParts": n_changed,
        "changed": changed_list[:200],
        "changedTruncated": n_changed > 200,
        "rules": rules_report,
        "unmatchedCount": len(unmatched_list),
        "unmatched": unmatched_list[:_LIST_CAP],
        "missingLcscCount": len(missing),
        "missingLcsc": missing[:_LIST_CAP],
    }


def summarize_parts(parts: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    parts = list(parts)
    return {
        "parts": len(parts),
        "dnp": sum(1 for p in parts if p["dnp"]),
        "notInBom": sum(1 for p in parts if not p["in_bom"]),
        "withLcsc": sum(1 for p in parts if p["lcsc"]),
    }
