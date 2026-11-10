"""One-call fabrication package for JLCPCB (MCP_ISSUES F3).

``fab_package`` replaces the hand-run sequence of ``kicad-cli`` exports plus
``jlc_export.py``. In one call it produces, under ``outputDir``:

* ``gerbers/`` -- Gerbers (+ job file), Excellon drill files with separate
  PTH/NPTH and Gerber X2 drill maps, and ``<board>_gerbers_JLCPCB.zip``;
* ``<board>_BOM_JLCPCB.csv`` -- Comment, Designator, Footprint, LCSC Part #;
* ``<board>_CPL_JLCPCB.csv`` -- Designator, Mid X, Mid Y, Layer, Rotation,
  from ``kicad-cli pcb export pos`` with the JLC rotation-correction table
  (configurable) and per-reference overrides;
* ``<board>_HAND_SOLDER_parts.csv`` -- parts kept out of assembly but still
  orderable by LCSC number;
* ``<board>_fab_report.json`` -- fee/stock report (``jlc_bom_report``), the
  final DRC/ERC summaries, the stored ``assert_connectivity`` sets, and the
  schematic/board parity.

The design files are only read. Exports plot the zone fills saved in the
board, and DRC checks those same saved fills (no refill), so the report
describes exactly what is being sent to the fab.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

#: Rotation corrections for packages whose KiCad zero orientation differs from
#: JLCPCB's (the community table used by JLCKicadTools / kicad-jlcpcb-tools).
#: Patterns match the footprint name (no library prefix); first match wins.
#: These are starting points: always check the placement preview on the JLC
#: order page, and override per reference with ``rotationOverrides``.
DEFAULT_ROTATION_TABLE: List[Tuple[str, float]] = [
    (r"^SOT-223", 180),
    (r"^SOT-23", 180),
    (r"^SOT-353", 180),
    (r"^SOT-363", 180),
    (r"^SOT-89", 180),
    (r"^QFN-", 270),
    (r"^LQFP-", 270),
    (r"^TQFP-", 270),
    (r"^MSOP-", 270),
    (r"^TSSOP-", 270),
    (r"^HTSSOP-", 270),
    (r"^SSOP-", 270),
    (r"^VSSOP-", 270),
    (r"^DFN-", 270),
    (r"^SOIC-", 270),
    (r"^SOP-", 270),
    (r"^CP_Elec_", 180),
    (r"^R_Array_Convex_", 90),
    (r"^Bosch_LGA-", 90),
    (r"^PowerPAK_SO-8_Single", 270),
]

_GERBER_EXT = re.compile(r"\.(g[a-z0-9]{1,3}|gbrjob|drl|gbr)$", re.I)
_DEFAULT_TIMEOUT = 600


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _refs(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(r).strip() for r in raw if str(r).strip()]
    if isinstance(raw, str):
        return [r.strip() for r in re.split(r"[,\s;]+", raw) if r.strip()]
    return []


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    logger.info("fab_package: " + " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _cli_error(step: str, proc: subprocess.CompletedProcess) -> str:
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
    return f"{step} failed (exit {proc.returncode}): {' | '.join(detail)}"


def copper_layers(board_text: str) -> List[str]:
    """Canonical copper layer names of a .kicad_pcb, front to back."""
    m = re.search(r"\(layers\b(.*?)\n\s*\)", board_text, re.S)
    names = (
        re.findall(r'\(\s*\d+\s+"([^"]+\.Cu)"\s+(?:signal|power|mixed|jumper)', m.group(1))
        if m
        else []
    )
    if not names:
        return ["F.Cu", "B.Cu"]
    inner = sorted(
        [n for n in names if n.startswith("In")], key=lambda n: int(re.sub(r"\D", "", n) or 0)
    )
    out = [n for n in ("F.Cu",) if n in names] + inner + [n for n in ("B.Cu",) if n in names]
    return out


def rotation_rules(spec: Any) -> List[Tuple[re.Pattern, float]]:
    if spec in (None, "default", True):
        table = DEFAULT_ROTATION_TABLE
    elif spec in ("none", False):
        table = []
    elif isinstance(spec, list):
        table = []
        for i, item in enumerate(spec):
            if not isinstance(item, dict) or "pattern" not in item:
                raise ValueError(f"rotationTable[{i}] must be {{pattern, degrees}}")
            table.append((str(item["pattern"]), float(item.get("degrees", 0))))
    else:
        raise ValueError("rotationTable must be 'default', 'none' or a list of {pattern, degrees}")
    return [(re.compile(p), float(d)) for p, d in table]


def _sort_key(ref: str):
    m = re.match(r"([^\d]*)(\d*)(.*)", ref)
    prefix, num, rest = m.groups() if m else (ref, "", "")
    return (prefix, int(num) if num else -1, rest)


# --------------------------------------------------------------------------- #
# Netlist -> parts
# --------------------------------------------------------------------------- #


def parse_netlist_parts(xml_text: str) -> List[Dict[str, Any]]:
    """Components of a kicadxml netlist with the fields a BOM needs."""
    from commands.bom_tools import is_lcsc_field

    root = ET.fromstring(xml_text)
    parts = []
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        fields = {f.get("name"): (f.text or "") for f in comp.iter("field")}
        props = {p.get("name"): p.get("value", "") for p in comp.findall("property")}
        lcsc = ""
        for name, val in list(fields.items()) + list(props.items()):
            if name and is_lcsc_field(name) and val.strip():
                lcsc = val.strip()
                break
        parts.append(
            {
                "ref": ref,
                "value": comp.findtext("value") or "",
                "footprint": comp.findtext("footprint") or "",
                "lcsc": lcsc,
                "dnp": "dnp" in props,
                "in_bom": "exclude_from_bom" not in props,
                "on_board": "exclude_from_board" not in props,
            }
        )
    return parts


def export_netlist_xml(cli: str, root_sch: Path, timeout: int) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".xml", prefix="kicad_mcp_fab_")
    os.close(fd)
    try:
        proc = _run(
            [
                cli,
                "sch",
                "export",
                "netlist",
                "--format",
                "kicadxml",
                "--output",
                tmp,
                str(root_sch),
            ],
            timeout,
        )
        if proc.returncode != 0 or not os.path.getsize(tmp):
            raise RuntimeError(_cli_error("netlist export", proc))
        return Path(tmp).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# BOM / CPL / hand-solder
# --------------------------------------------------------------------------- #


def plan_assembly(
    parts: List[Dict[str, Any]], hand: List[str], excluded: List[str]
) -> Dict[str, Any]:
    """Split parts into assembled BOM groups, hand-solder groups and leftovers."""
    hand_set, excl_set = set(hand), set(excluded)
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    hand_groups: Dict[Tuple[str, str, str], List[str]] = {}
    placed: List[str] = []
    missing_lcsc, dnp, not_in_bom = [], [], []
    for p in parts:
        ref = p["ref"]
        if p["dnp"] or (p["lcsc"] or "").upper() == "DNP":
            dnp.append(ref)
            continue
        if not p["in_bom"]:
            not_in_bom.append(ref)
            continue
        fp = p["footprint"].split(":")[-1]
        key = (p["value"], fp, p["lcsc"])
        if ref in hand_set:
            hand_groups.setdefault(key, []).append(ref)
            continue
        if ref in excl_set:
            continue
        if not p["lcsc"]:
            missing_lcsc.append({"ref": ref, "value": p["value"], "footprint": fp})
            continue
        groups.setdefault(key, []).append(ref)
        placed.append(ref)
    for g in list(groups.values()) + list(hand_groups.values()):
        g.sort(key=_sort_key)
    known = {p["ref"] for p in parts}
    return {
        "groups": groups,
        "handGroups": hand_groups,
        "placed": placed,
        "missingLcsc": missing_lcsc,
        "dnp": sorted(dnp, key=_sort_key),
        "notInBom": sorted(not_in_bom, key=_sort_key),
        "unknownRefs": sorted((hand_set | excl_set) - known, key=_sort_key),
    }


def write_csv(path: Path, header: List[str], rows: List[List[Any]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)


def build_cpl(
    pos_csv: str,
    placed: List[str],
    rules: List[Tuple[re.Pattern, float]],
    overrides: Dict[str, float],
) -> Tuple[List[List[str]], List[Dict[str, Any]], List[str]]:
    """CPL rows for *placed* refs, the corrections applied, and refs absent from pos."""
    wanted = set(placed)
    rows: List[List[str]] = []
    corrections: List[Dict[str, Any]] = []
    seen = set()
    for row in csv.DictReader(io.StringIO(pos_csv)):
        ref = row.get("Ref", "")
        if ref not in wanted:
            continue
        seen.add(ref)
        side = "Top" if row.get("Side", "").lower().startswith("top") else "Bottom"
        rot = float(row.get("Rot", "0") or 0)
        package = row.get("Package", "")
        delta = 0.0
        source = None
        if ref in overrides:
            delta, source = float(overrides[ref]), "override"
        else:
            for rx, deg in rules:
                if rx.search(package):
                    delta, source = deg, rx.pattern
                    break
        if delta:
            new = (rot - delta) % 360 if side == "Bottom" else (rot + delta) % 360
            corrections.append(
                {
                    "ref": ref,
                    "package": package,
                    "side": side,
                    "kicad": rot,
                    "jlc": new,
                    "rule": source,
                }
            )
            rot = new
        rows.append([ref, row["PosX"] + "mm", row["PosY"] + "mm", side, f"{rot:.6f}"])
    rows.sort(key=lambda r: _sort_key(r[0]))
    return rows, corrections, sorted(wanted - seen, key=_sort_key)


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


def _summary(normalized: List[Dict[str, Any]], top: int = 10) -> Dict[str, Any]:
    from utils import check_summary

    c = check_summary.counts(normalized)
    ordered = sorted(
        normalized,
        key=lambda v: ({"error": 0, "warning": 1}.get(v["severity"], 2), v["type"]),
    )
    return {
        "total": c["total"],
        "by_severity": c["by_severity"],
        "by_type": c["by_type"],
        "top": [check_summary.compact(v) for v in ordered[:top]],
    }


def run_drc(cli: str, board: Path, project: Optional[str], timeout: int) -> Dict[str, Any]:
    from commands.design_rules import DesignRuleCommands
    from utils import check_summary

    check_file, stage, rules = DesignRuleCommands._resolve_drc_project(
        str(board), project, with_schematic=True
    )
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="kicad_mcp_fab_drc_")
    os.close(fd)
    try:
        cmd = [cli, "pcb", "drc", "--format", "json", "--units", "mm", "--output", tmp]
        cmd += ["--schematic-parity", "--severity-error", "--severity-warning", check_file]
        proc = _run(cmd, timeout)
        if not os.path.getsize(tmp):
            return {"ran": False, "error": _cli_error("DRC", proc)}
        data = json.loads(Path(tmp).read_text(encoding="utf-8"))
        normalized = [
            check_summary.normalize_violation(raw, category)
            for category in DesignRuleCommands.DRC_REPORT_CATEGORIES
            for raw in (data.get(category, []) or [])
        ]
        check_summary.record_run("drc", str(board), normalized)
        out = {"ran": True, "rules": rules, **_summary(normalized)}
        out["unconnected"] = sum(1 for v in normalized if v.get("category") == "unconnected_items")
        out["parityIssues"] = sum(1 for v in normalized if v.get("category") == "schematic_parity")
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if stage:
            import shutil

            shutil.rmtree(stage, ignore_errors=True)


def run_erc(cli: str, root_sch: Path, timeout: int) -> Dict[str, Any]:
    from utils import check_summary

    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="kicad_mcp_fab_erc_")
    os.close(fd)
    try:
        proc = _run(
            [cli, "sch", "erc", "--format", "json", "--output", tmp, str(root_sch)], timeout
        )
        if not os.path.getsize(tmp):
            return {"ran": False, "error": _cli_error("ERC", proc)}
        data = json.loads(Path(tmp).read_text(encoding="utf-8"))
        raw = list(data.get("violations", []) or [])
        for sheet in data.get("sheets", []) or []:
            raw.extend(sheet.get("violations", []) or [])
        normalized = [check_summary.normalize_violation(v) for v in raw]
        check_summary.record_run("erc", str(root_sch), normalized)
        return {"ran": True, **_summary(normalized)}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def stored_connectivity(root_sch: Path) -> List[Dict[str, Any]]:
    """Re-run every stored ``assert_connectivity`` expectation set, if any."""
    from commands.schematic_connectivity import (
        _store_path,
        assert_connectivity,
        find_project_file,
    )

    pro = find_project_file(root_sch)
    store = _store_path(pro.parent if pro else root_sch.parent)
    if not store.is_file():
        return []
    try:
        sets = json.loads(store.read_text(encoding="utf-8")).get("sets", {}) or {}
    except (OSError, ValueError):
        return [{"error": f"unreadable {store}"}]
    out = []
    for name in sorted(sets):
        res = assert_connectivity({"schematicPath": str(root_sch), "useStored": name})
        item: Dict[str, Any] = {"set": name, "ok": bool(res.get("success") and res.get("passed"))}
        for key in ("checkedPins", "passed", "message"):
            if key in res:
                item[key] = res[key]
        for key in ("mismatches", "missing", "singlePinNets", "unexpectedNoConnects"):
            if isinstance(res.get(key), list):
                item[key] = len(res[key])
        out.append(item)
    return out


def parity(root_sch: Path, board: Path) -> Dict[str, Any]:
    from commands.project_status import (
        _sorted_refs,
        board_inventory_from_file,
        schematic_inventory,
    )

    sch = schematic_inventory(str(root_sch))
    brd = board_inventory_from_file(str(board))
    missing_on_board = sch["boardRefs"] - brd["refs"]
    missing_in_sch = brd["refs"] - sch["refs"]
    return {
        "ok": not missing_on_board and not missing_in_sch,
        "schematicParts": len(sch["refs"]),
        "boardFootprints": brd["footprints"],
        "missingOnBoard": _sorted_refs(missing_on_board)[:50],
        "missingInSchematic": _sorted_refs(missing_in_sch)[:50],
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _resolve_inputs(params: Dict[str, Any]) -> Tuple[Path, Optional[Path], Optional[str]]:
    from commands.project_status import resolve_project_files

    board = params.get("boardPath")
    anchor = params.get("projectPath") or board or params.get("schematicPath")
    if not anchor:
        raise ValueError("boardPath or projectPath is required")
    files = resolve_project_files(str(anchor), board)
    pcb = files.get("board")
    if not pcb or not os.path.isfile(pcb):
        raise FileNotFoundError(f"Board not found for {anchor}")
    sch = params.get("schematicPath") or files.get("schematic")
    root = None
    if sch and os.path.isfile(sch):
        from commands.bom_tools import resolve_root_schematic

        root = resolve_root_schematic(str(sch))
    return Path(pcb).resolve(), root, files.get("project")


def fab_package(params: Dict[str, Any]) -> Dict[str, Any]:
    """Gerbers + drill (zipped), JLC BOM/CPL, hand-solder list, fee and check reports."""
    from utils.kicad_cli import kicad_cli_not_found_message, resolve_kicad_cli

    started = time.time()
    fab = str(params.get("fab", "jlcpcb")).lower()
    if fab != "jlcpcb":
        return {"success": False, "message": f"fab '{fab}' is not supported (only 'jlcpcb')"}
    opts = params.get("options") or {}
    if not isinstance(opts, dict):
        return {"success": False, "message": "options must be an object"}
    out_dir = params.get("outputDir")
    if not out_dir:
        return {"success": False, "message": "outputDir is required"}
    try:
        board, root_sch, project = _resolve_inputs(params)
        rules = rotation_rules(opts.get("rotationTable", "default"))
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {"success": False, "message": str(exc)}
    overrides_raw = opts.get("rotationOverrides") or {}
    try:
        overrides = {str(k): float(v) for k, v in overrides_raw.items()}
    except (AttributeError, TypeError, ValueError):
        return {"success": False, "message": "rotationOverrides must be {ref: degrees}"}
    hand = _refs(opts.get("handSolder"))
    excluded = _refs(opts.get("excludeFromAssembly"))
    make_zip = bool(opts.get("zip", True))
    run_checks = bool(opts.get("runChecks", True))
    boards = max(1, int(opts.get("boards", 5) or 5))
    timeout = max(30, min(int(params.get("timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT), 3600))

    cli = resolve_kicad_cli()
    if not cli:
        return {"success": False, "message": kicad_cli_not_found_message()}

    out = Path(out_dir).expanduser().resolve()
    gerber_dir = out / "gerbers"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    stem = board.stem
    warnings: List[str] = []
    files: Dict[str, Any] = {}

    # Stale outputs of an earlier run would otherwise end up in the zip.
    for old in gerber_dir.iterdir():
        if old.is_file() and old.name.startswith(stem + "-") and _GERBER_EXT.search(old.name):
            old.unlink()

    try:
        # 1. Gerbers
        board_text = board.read_text(encoding="utf-8", errors="replace")
        cmd = [cli, "pcb", "export", "gerbers", "--output", str(gerber_dir)]
        if opts.get("useBoardPlotParams"):
            cmd.append("--board-plot-params")
        else:
            layers = copper_layers(board_text) + [
                "F.Paste",
                "B.Paste",
                "F.Silkscreen",
                "B.Silkscreen",
                "F.Mask",
                "B.Mask",
                "Edge.Cuts",
            ]
            cmd += ["--layers", ",".join(layers), "--subtract-soldermask"]
        cmd.append(str(board))
        proc = _run(cmd, timeout)
        if proc.returncode != 0:
            return {"success": False, "message": _cli_error("Gerber export", proc)}

        # 2. Drill + maps
        proc = _run(
            [
                cli,
                "pcb",
                "export",
                "drill",
                "--output",
                str(gerber_dir) + os.sep,
                "--format",
                "excellon",
                "--excellon-units",
                "mm",
                "--excellon-separate-th",
                "--generate-map",
                "--map-format",
                "gerberx2",
                str(board),
            ],
            timeout,
        )
        if proc.returncode != 0:
            return {"success": False, "message": _cli_error("Drill export", proc)}
        fab_files = sorted(p for p in gerber_dir.iterdir() if p.is_file())
        files["gerberDir"] = str(gerber_dir)
        files["gerberFiles"] = [p.name for p in fab_files]
        if make_zip:
            zip_path = out / f"{stem}_gerbers_JLCPCB.zip"
            tmp_zip = zip_path.with_name(zip_path.name + ".tmp")
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in fab_files:
                    zf.write(p, p.name)
            os.replace(tmp_zip, zip_path)
            files["gerberZip"] = str(zip_path)

        # 3. Position file
        pos_path = out / f"{stem}-pos.csv"
        proc = _run(
            [
                cli,
                "pcb",
                "export",
                "pos",
                "--output",
                str(pos_path),
                "--format",
                "csv",
                "--units",
                "mm",
                "--side",
                "both",
                str(board),
            ],
            timeout,
        )
        if proc.returncode != 0:
            return {"success": False, "message": _cli_error("Position export", proc)}
        files["pos"] = str(pos_path)
        pos_csv = pos_path.read_text(encoding="utf-8")

        # 4. BOM / CPL / hand-solder (needs the schematic's fields)
        plan = None
        corrections: List[Dict[str, Any]] = []
        missing_pos: List[str] = []
        if root_sch is None:
            warnings.append("No schematic found next to the board: BOM and CPL were not written.")
        else:
            parts = parse_netlist_parts(export_netlist_xml(cli, root_sch, timeout))
            plan = plan_assembly(parts, hand, excluded)
            bom_rows = [
                [val, ",".join(refs), fp, lcsc] for (val, fp, lcsc), refs in plan["groups"].items()
            ]
            bom_rows.sort(key=lambda r: _sort_key(r[1].split(",")[0]))
            bom_path = out / f"{stem}_BOM_JLCPCB.csv"
            write_csv(bom_path, ["Comment", "Designator", "Footprint", "LCSC Part #"], bom_rows)
            files["bom"] = str(bom_path)
            cpl_rows, corrections, missing_pos = build_cpl(
                pos_csv, plan["placed"], rules, overrides
            )
            cpl_path = out / f"{stem}_CPL_JLCPCB.csv"
            write_csv(cpl_path, ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"], cpl_rows)
            files["cpl"] = str(cpl_path)
            if plan["handGroups"]:
                hand_rows = [
                    [val, ",".join(refs), fp, lcsc, len(refs)]
                    for (val, fp, lcsc), refs in plan["handGroups"].items()
                ]
                hand_path = out / f"{stem}_HAND_SOLDER_parts.csv"
                write_csv(
                    hand_path,
                    ["Comment", "Designator", "Footprint", "LCSC Part #", "Qty per board"],
                    hand_rows,
                )
                files["handSolder"] = str(hand_path)
            if plan["missingLcsc"]:
                warnings.append(
                    f"{len(plan['missingLcsc'])} BOM part(s) have no LCSC number and were left "
                    "out of the JLC BOM/CPL: "
                    + ", ".join(p["ref"] for p in plan["missingLcsc"][:20])
                )
            if plan["unknownRefs"]:
                warnings.append(
                    "handSolder/excludeFromAssembly name refs not in the design: "
                    + ", ".join(plan["unknownRefs"])
                )
            if missing_pos:
                warnings.append(
                    f"{len(missing_pos)} assembled part(s) have no footprint on the board: "
                    + ", ".join(missing_pos[:20])
                )
            if corrections:
                warnings.append(
                    f"{len(corrections)} CPL rotation(s) were corrected for JLC "
                    "(see rotationCorrections); check the placement preview before ordering."
                )

        # 5. Fee / stock report
        fees = None
        if plan is not None:
            fees = _fee_report(plan, boards, opts)
            if fees.get("error"):
                warnings.append(fees["error"])

        # 6. Checks
        checks: Dict[str, Any] = {}
        if run_checks:
            checks["drc"] = run_drc(cli, board, project, timeout)
            if root_sch is not None:
                checks["erc"] = run_erc(cli, root_sch, timeout)
                try:
                    checks["connectivity"] = stored_connectivity(root_sch)
                except Exception as exc:  # pragma: no cover - reported, not fatal
                    checks["connectivity"] = [{"error": str(exc)}]
                checks["parity"] = parity(root_sch, board)
    except subprocess.TimeoutExpired as exc:
        return {"success": False, "message": f"kicad-cli timed out after {exc.timeout}s"}
    except (RuntimeError, OSError, ET.ParseError) as exc:
        return {"success": False, "message": str(exc)}

    blockers = _blockers(plan, checks, fees)
    counts = {
        "gerberFiles": sum(1 for f in files.get("gerberFiles", []) if not f.endswith(".drl")),
        "drillFiles": sum(1 for f in files.get("gerberFiles", []) if f.endswith(".drl")),
    }
    if plan is not None:
        counts.update(
            bomLines=len(plan["groups"]),
            assembledParts=len(plan["placed"]),
            cplRows=len(plan["placed"]) - len(missing_pos),
            handSolderLines=len(plan["handGroups"]),
            handSolderParts=sum(len(r) for r in plan["handGroups"].values()),
            dnp=len(plan["dnp"]),
            notInBom=len(plan["notInBom"]),
            missingLcsc=len(plan["missingLcsc"]),
        )
    report = {
        "board": str(board),
        "schematic": str(root_sch) if root_sch else None,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": files,
        "counts": counts,
        "rotationCorrections": corrections,
        "fees": fees,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "assembly": (
            {
                "handSolder": hand,
                "excludeFromAssembly": excluded,
                "dnp": plan["dnp"],
                "notInBom": plan["notInBom"],
                "missingLcsc": plan["missingLcsc"],
            }
            if plan is not None
            else None
        ),
    }
    report_path = out / f"{stem}_fab_report.json"
    tmp = report_path.with_name(report_path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, report_path)
    files["report"] = str(report_path)

    fee_txt = ""
    if fees and fees.get("totals"):
        t = fees["totals"]
        fee_txt = f"; loading fee ${t['loadingFee']:.2f} ({t['extendedLines']} extended)"
    message = (
        f"Fab package in {out}: {counts['gerberFiles']} Gerber + {counts['drillFiles']} drill file(s)"
        + (
            f", BOM {counts['bomLines']} line(s) / {counts['assembledParts']} part(s), "
            f"CPL {counts['cplRows']} row(s), hand-solder {counts['handSolderLines']} line(s)"
            if plan is not None
            else ""
        )
        + fee_txt
        + ("; READY" if not blockers else f"; {len(blockers)} blocker(s)")
        + f" ({time.time() - started:.0f}s)"
    )
    result = dict(report)
    result.update(
        {
            "success": True,
            "message": message,
            "outputDir": str(out),
            "readyToOrder": not blockers,
            "rotationCorrections": corrections[:100],
        }
    )
    if fees and isinstance(fees.get("lines"), list):
        result["fees"] = {k: v for k, v in fees.items() if k != "lines"}
        result["fees"]["lines"] = fees["lines"][:30]
    return result


def _fee_report(plan: Dict[str, Any], boards: int, opts: Dict[str, Any]) -> Dict[str, Any]:
    from commands.jlc_sourcing import DEFAULT_EXTENDED_FEE, PartsDB, build_report

    lines: Dict[str, Dict[str, Any]] = {}
    for (val, fp, lcsc), refs in plan["groups"].items():
        key = lcsc.upper()
        line = lines.setdefault(
            key, {"lcsc": key, "refs": [], "qtyPerBoard": 0, "value": val, "footprint": fp}
        )
        line["refs"].extend(refs)
        line["qtyPerBoard"] += len(refs)
    try:
        db = PartsDB.open(opts.get("dbPath"))
    except Exception as exc:
        return {"error": f"Fee/stock report skipped: {exc}"}
    with db:
        try:
            rep = build_report(
                lines,
                boards,
                float(opts.get("extendedFee", DEFAULT_EXTENDED_FEE)),
                int(opts.get("alternatives", 2)),
                db,
                "summary",
            )
        except Exception as exc:
            return {"error": f"Fee/stock report failed: {exc}"}
        rep["database"] = db.describe()
    return rep


def _blockers(
    plan: Optional[Dict[str, Any]], checks: Dict[str, Any], fees: Optional[Dict[str, Any]]
) -> List[str]:
    out = []
    drc = checks.get("drc") or {}
    if drc.get("ran") and drc["by_severity"].get("error"):
        out.append(f"DRC: {drc['by_severity']['error']} error(s)")
    if drc.get("ran") is False:
        out.append("DRC did not run")
    erc = checks.get("erc") or {}
    if erc.get("ran") and erc["by_severity"].get("error"):
        out.append(f"ERC: {erc['by_severity']['error']} error(s)")
    par = checks.get("parity") or {}
    if par and not par.get("ok"):
        out.append("schematic/board parity mismatch")
    for item in checks.get("connectivity") or []:
        if not item.get("ok"):
            out.append(f"assert_connectivity set '{item.get('set')}' failed")
    if plan is not None and plan["missingLcsc"]:
        out.append(f"{len(plan['missingLcsc'])} BOM part(s) without LCSC")
    if fees and fees.get("totals"):
        t = fees["totals"]
        if t.get("stockShort"):
            out.append(f"{t['stockShort']} BOM line(s) short on stock")
        if t.get("notFound"):
            out.append(f"{t['notFound']} LCSC number(s) not in the catalog")
    return out
