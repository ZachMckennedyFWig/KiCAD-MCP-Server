"""Orchestrated Freerouting runs (MCP_ISSUES R2, #5l, #5m, principle 6).

``autoroute`` switches to this pipeline when any orchestration option is given
(``async``, ``nets``, ``lockExisting``, ``stripRuleAreas``,
``forbidRegionsForNets``, ``passes``, ``stallTimeout``, ``importMode``,
``evaluate``). Without them the legacy single/best-of-N flow in
``freerouting.py`` runs unchanged.

Flow (everything after step 1 runs in a background job thread that only
manages child processes and text files -- it never touches the live board):

1. *Stage* (main thread): the in-memory board is written to a staging dir with
   ``SaveBoard(..., aSkipSettings=True)`` next to copies of the project's
   ``.kicad_pro``/``.kicad_dru``/``fp-lib-table``; its hash is the baseline.
2. *Export* the DSN from that disk copy in a child process (#5l), with the
   project's net classes applied.
3. *Rewrite* the DSN: grow edge-hole keepouts, strip pour-only (or all)
   rule-area keepouts that KiCad exports as hard obstacles, mark existing
   wiring ``fix`` (``lockExisting``), restrict routing to ``nets`` (other nets
   keep their pads and copper as fixed obstacles), add keepouts for
   ``forbidRegionsForNets`` entries that cover every routed net.
4. *Baseline DRC* with kicad-cli (``--refill-zones``) on the staged copy.
5. *Freerouting* (1..N attempts) with output streamed into the job log;
   ``stallTimeout`` kills an attempt that makes no progress (no better pass
   score / fewer unrouted / new stage) for that many seconds.
6. *Import* the best SES into a *copy* in a child process: SES wiring of nets
   that are not being imported is filtered out and their copper is locked for
   the import (KiCad deletes every unlocked track on SES import) and unlocked
   again afterwards; zones are refilled; the copy is saved.
7. *Evaluate* the copy with kicad-cli DRC: unconnected items as KiCad counts
   them (plane connections satisfied by zones do not count) and the DRC delta.
8. *Decide*: ``importMode: "always"`` or ``"if_better"`` (fewer unconnected
   items without more DRC errors, or as many unconnected with fewer errors).

The board is only changed by the *apply step* (``job_apply`` for ``async``
runs; done inline for synchronous ones), on the main command thread: it checks
that neither the in-memory board nor the file changed since staging, then
replaces the ``.kicad_pcb`` with the routed copy (atomic rename) and reports
``_boardFileWritten: disk`` so the dispatcher reloads the board.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from utils.jobs import JOBS, Job, JobContext, run_process

logger = logging.getLogger("kicad_interface")

ORCHESTRATION_KEYS = (
    "async",
    "nets",
    "lockExisting",
    "stripRuleAreas",
    "forbidRegionsForNets",
    "passes",
    "stallTimeout",
    "importMode",
    "evaluate",
    "lockRouted",
)

STRIP_POLICIES = ("pour_only", "all", "none")
IMPORT_MODES = ("always", "if_better")
WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autoroute_worker.py")


def wants_orchestration(params: Dict[str, Any]) -> bool:
    return any(params.get(k) is not None for k in ORCHESTRATION_KEYS)


# ---------------------------------------------------------------------------
# S-expression helpers for DSN / SES text
# ---------------------------------------------------------------------------


def _sexpr_end(text: str, start: int) -> int:
    depth = 0
    in_string = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == '"':
            in_string = not in_string
        elif in_string:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _find_block(text: str, head: str, start: int = 0) -> Optional[Tuple[int, int]]:
    m = re.compile(r"\(" + re.escape(head) + r"(?=[\s)])").search(text, start)
    if not m:
        return None
    end = _sexpr_end(text, m.start())
    return (m.start(), end) if end > 0 else None


def _children(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Direct child expressions of the block ``text[start:end]``."""
    out = []
    i = text.index("(", start) + 1  # skip the block's own paren
    in_string = False
    while i < end - 1:
        ch = text[i]
        if ch == '"':
            in_string = not in_string
        elif not in_string and ch == "(":
            e = _sexpr_end(text, i)
            if e < 0:
                break
            out.append((i, e))
            i = e
            continue
        i += 1
    return out


def _head(text: str, s: int) -> str:
    m = re.match(r"\(([^\s()]+)", text[s : s + 64])
    return m.group(1) if m else ""


_NAME_RE = r'("[^"]*"|[^\s()]+)'


def _unquote(name: str) -> str:
    return name[1:-1] if len(name) >= 2 and name[0] == '"' and name[-1] == '"' else name


def _net_of(expr: str) -> Optional[str]:
    m = re.search(r"\(net\s+" + _NAME_RE, expr)
    return _unquote(m.group(1)) if m else None


def _splice(text: str, edits: List[Tuple[int, int, str]]) -> str:
    for s, e, rep in sorted(edits, key=lambda x: x[0], reverse=True):
        text = text[:s] + rep + text[e:]
    return text


# ---------------------------------------------------------------------------
# DSN rewrites (pure text, unit-tested)
# ---------------------------------------------------------------------------

_SHAPE_RE = re.compile(r"\((polygon|rect|path|circle)\s+" + _NAME_RE + r"\s+([-\d.eE\s]+)")


def _shape_bbox_mm(expr: str) -> Optional[Tuple[str, Tuple[float, float, float, float]]]:
    """Layer and bbox (board mm, y down) of a DSN keepout's shape (unit um)."""
    m = _SHAPE_RE.search(expr)
    if not m:
        return None
    kind, layer = m.group(1), _unquote(m.group(2))
    nums = [float(v) for v in m.group(3).split()]
    if kind in ("polygon", "path"):
        coords = nums[1:]  # first number is the aperture width
    elif kind == "rect":
        coords = nums[:4]
    else:  # circle: diameter [x y]
        d = nums[0]
        cx, cy = (nums[1], nums[2]) if len(nums) >= 3 else (0.0, 0.0)
        coords = [cx - d / 2, cy - d / 2, cx + d / 2, cy + d / 2]
    xs, ys = coords[0::2], coords[1::2]
    if not xs or not ys:
        return None
    return layer, (min(xs) / 1000, -max(ys) / 1000, max(xs) / 1000, -min(ys) / 1000)


def strip_rule_area_keepouts(
    dsn: str, rule_areas: Sequence[Dict[str, Any]], policy: str, tol_mm: float = 0.06
) -> Tuple[str, Dict[str, Any]]:
    """Remove the board-level keepouts KiCad exported for rule areas.

    KiCad's Specctra exporter writes a rule area that forbids *only* copper
    pours (tracks and vias allowed) as a plain ``keepout`` -- a hard obstacle
    to Freerouting (MCP_ISSUES R2: it blocked all four pin rows of an RP2350).
    ``policy="pour_only"`` strips the keepouts of such rule areas; ``"all"``
    strips those of every rule area; ``"none"`` keeps everything. Keepouts are
    matched to rule areas by layer and bounding box. Edge.Cuts hole keepouts
    and keepouts inside footprints are never touched. The board itself is not
    modified, so nothing needs restoring afterwards.
    """
    report: Dict[str, Any] = {"policy": policy, "stripped": [], "ruleAreas": len(rule_areas)}
    if policy == "none" or not rule_areas:
        return dsn, report
    blk = _find_block(dsn, "structure")
    if not blk:
        return dsn, report
    wanted = [
        ra
        for ra in rule_areas
        if policy == "all" or (not ra.get("noTracks") and not ra.get("noVias"))
    ]
    edits = []
    for s, e in _children(dsn, *blk):
        if _head(dsn, s) not in ("keepout", "via_keepout", "wire_keepout"):
            continue
        expr = dsn[s:e]
        shape = _shape_bbox_mm(expr)
        if not shape:
            continue
        layer, bb = shape
        for ra in wanted:
            if layer not in (ra.get("copperLayers") or ra.get("layers") or []):
                continue
            rb = ra["bbox"]
            if all(abs(a - b) <= tol_mm for a, b in zip(bb, rb)):
                # drop the whole line including its indentation
                ls = dsn.rfind("\n", 0, s) + 1
                le = dsn.find("\n", e)
                le = e if le < 0 else le
                if dsn[ls:s].strip() == "" and dsn[e:le].strip() == "":
                    edits.append((ls, le + 1 if le < len(dsn) else le, ""))
                else:
                    edits.append((s, e, ""))
                report["stripped"].append(
                    {"name": ra.get("name", ""), "layer": layer, "bbox": [round(v, 3) for v in bb]}
                )
                break
    report["strippedCount"] = len(report["stripped"])
    return _splice(dsn, edits), report


def lock_existing_wiring(dsn: str) -> Tuple[str, int]:
    """Mark every existing wire and via in the DSN ``fix`` (Freerouting won't move them)."""
    blk = _find_block(dsn, "wiring")
    if not blk:
        return dsn, 0
    s, e = blk
    section = dsn[s:e]
    new, n = re.subn(r"\(type\s+(?:route|protect|normal)\)", "(type fix)", section)
    return dsn[:s] + new + dsn[e:], n


def restrict_to_nets(dsn: str, nets: Iterable[str]) -> Tuple[str, Dict[str, Any]]:
    """Route only ``nets``: every other net loses its pin list and its wiring is fixed.

    The other nets' pads stay in the design (as net-less obstacles) and their
    existing copper stays as fixed obstacles, so the routed nets keep clear of
    them, but Freerouting has nothing to connect for them.
    """
    keep = set(nets)
    found: Set[str] = set()
    excluded = 0
    edits: List[Tuple[int, int, str]] = []
    blk = _find_block(dsn, "network")
    if blk:
        for s, e in _children(dsn, *blk):
            if _head(dsn, s) != "net":
                continue
            m = re.match(r"\(net\s+" + _NAME_RE, dsn[s:e])
            if not m:
                continue
            name = _unquote(m.group(1))
            if name in keep:
                found.add(name)
                continue
            excluded += 1
            for cs, ce in _children(dsn, s, e):
                if _head(dsn, cs) == "pins":
                    edits.append((cs, ce, "(pins)"))
    fixed = 0
    blk = _find_block(dsn, "wiring")
    if blk:
        for s, e in _children(dsn, *blk):
            expr = dsn[s:e]
            net = _net_of(expr)
            if net is not None and net not in keep:
                new, n = re.subn(r"\(type\s+(?:route|protect|normal)\)", "(type fix)", expr)
                if n:
                    fixed += 1
                    edits.append((s, e, new))
    report = {
        "routedNets": sorted(found),
        "unknownNets": sorted(keep - found),
        "excludedNets": excluded,
        "otherNetWiringFixed": fixed,
    }
    return _splice(dsn, edits), report


def _dsn_layer(name: str) -> str:
    return f'"{name}"' if re.search(r"[\s()]", name) else name


def add_region_keepouts(dsn: str, regions: Sequence[Dict[str, Any]]) -> Tuple[str, int]:
    """Add ``(keepout ...)`` polygons (board mm) to the DSN structure section."""
    blk = _find_block(dsn, "structure")
    if not blk or not regions:
        return dsn, 0
    lines = []
    for i, reg in enumerate(regions):
        pts = reg["polygon"]
        coords = "  ".join(f"{x * 1000:.1f} {-y * 1000:.1f}" for x, y in list(pts) + [pts[0]])
        for layer in reg["layers"]:
            lines.append(
                f'    (keepout "mcp_forbid_{i}" (polygon {_dsn_layer(layer)} 0  {coords}))'
            )
    s, e = blk
    insert_at = e - 1  # before the structure's closing paren
    text = dsn[:insert_at].rstrip(" ") + "\n".join(lines) + "\n  " + dsn[insert_at:]
    return text, len(lines)


def grow_edge_keepouts(dsn: str, edge_mm: float) -> str:
    """Grow Edge.Cuts hole keepouts to the copper-to-edge clearance (as the legacy path does)."""
    from commands.freerouting import _grow_hole_keepouts

    if not re.search(r"\(unit um\)", dsn):
        return dsn
    m = re.search(r"\(rule\s*\(width [\d.]+\)\s*\(clearance ([\d.]+)\)", dsn)
    track_um = float(m.group(1)) if m else 0.0
    return _grow_hole_keepouts(dsn, edge_mm * 1000 - track_um)


# ---------------------------------------------------------------------------
# SES helpers
# ---------------------------------------------------------------------------


def _ses_net_blocks(ses: str) -> List[Tuple[str, int, int]]:
    blk = _find_block(ses, "network_out")
    if not blk:
        return []
    out = []
    for s, e in _children(ses, *blk):
        if _head(ses, s) != "net":
            continue
        m = re.match(r"\(net\s+" + _NAME_RE, ses[s:e])
        if m:
            out.append((_unquote(m.group(1)), s, e))
    return out


def ses_nets(ses: str) -> List[str]:
    return [n for n, _, _ in _ses_net_blocks(ses)]


def filter_ses_nets(ses: str, keep: Set[str]) -> Tuple[str, List[str]]:
    """Drop the SES wiring of every net not in ``keep``."""
    edits, dropped = [], []
    for name, s, e in _ses_net_blocks(ses):
        if name not in keep:
            dropped.append(name)
            edits.append((s, e, ""))
    return _splice(ses, edits), dropped


def _ses_resolution_um(ses: str) -> float:
    blk = _find_block(ses, "routes")
    m = re.search(r"\(resolution\s+(\w+)\s+([\d.]+)\)", ses[blk[0] : blk[1]] if blk else ses)
    if not m:
        return 1.0
    unit, val = m.group(1).lower(), float(m.group(2))
    per_unit_um = {"um": 1.0, "mm": 1000.0, "mil": 25.4, "inch": 25400.0, "cm": 10000.0}.get(
        unit, 1.0
    )
    return per_unit_um / val


def ses_new_copper(
    ses: str,
) -> Dict[str, List[Tuple[str, Optional[str], List[Tuple[float, float]]]]]:
    """New (not ``protect``/``fix``) wires and vias per net, in board mm."""
    scale = _ses_resolution_um(ses) / 1000.0
    out: Dict[str, List[Tuple[str, Optional[str], List[Tuple[float, float]]]]] = {}
    for name, s, e in _ses_net_blocks(ses):
        for cs, ce in _children(ses, s, e):
            expr = ses[cs:ce]
            if re.search(r"\(type\s+(?:protect|fix)\)", expr):
                continue
            head = _head(ses, cs)
            if head == "wire":
                m = re.search(r"\(path\s+" + _NAME_RE + r"\s+([-\d.\s]+)", expr)
                if not m:
                    continue
                nums = [float(v) for v in m.group(2).split()][1:]
                pts = [(x * scale, -y * scale) for x, y in zip(nums[0::2], nums[1::2])]
                out.setdefault(name, []).append(("wire", _unquote(m.group(1)), pts))
            elif head == "via":
                m = re.match(r"\(via\s+" + _NAME_RE + r"\s+([-\d.]+)\s+([-\d.]+)", expr)
                if m:
                    pt = (float(m.group(2)) * scale, -float(m.group(3)) * scale)
                    out.setdefault(name, []).append(("via", None, [pt]))
    return out


def point_in_polygon(x: float, y: float, pts: Sequence[Tuple[float, float]]) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def _copper_enters(items, polygon, layers: Optional[Set[str]]) -> bool:
    for kind, layer, pts in items:
        if kind == "wire" and layers and layer not in layers:
            continue
        if kind == "via" or len(pts) == 1:
            if point_in_polygon(pts[0][0], pts[0][1], polygon):
                return True
            continue
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            steps = max(1, int(math.hypot(bx - ax, by - ay) / 0.1))
            for k in range(steps + 1):
                x = ax + (bx - ax) * k / steps
                y = ay + (by - ay) * k / steps
                if point_in_polygon(x, y, polygon):
                    return True
    return False


def normalize_region(region: Any) -> List[Tuple[float, float]]:
    """``{x1,y1,x2,y2}`` / ``{polygon:[...]}`` / ``[[x,y],...]`` -> polygon (mm)."""
    if isinstance(region, dict) and "polygon" in region:
        region = region["polygon"]
    if isinstance(region, dict):
        if all(k in region for k in ("x1", "y1", "x2", "y2")):
            x1, y1, x2, y2 = (float(region[k]) for k in ("x1", "y1", "x2", "y2"))
            return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        if all(k in region for k in ("x", "y", "width", "height")):
            x, y, w, h = (float(region[k]) for k in ("x", "y", "width", "height"))
            return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        raise ValueError(f"region needs x1,y1,x2,y2 or polygon: {region}")
    pts = []
    for p in region or []:
        if isinstance(p, dict):
            pts.append((float(p["x"]), float(p["y"])))
        else:
            pts.append((float(p[0]), float(p[1])))
    if len(pts) < 3:
        raise ValueError("a region polygon needs at least 3 points")
    return pts


# ---------------------------------------------------------------------------
# Freerouting output parsing (progress + stall detection)
# ---------------------------------------------------------------------------

_UNROUTED_RE = re.compile(r"\((\d+) unrouted and (\d+) violations\)")
_SCORE_RE = re.compile(r"score:? ([\d.]+)")
_FANOUT_RE = re.compile(r"Fanout pass #(\d+).*?with (\d+) SMD pins fanouted")
_STAGE_RE = re.compile(
    r"(stage started|Starting routing|Starting optimization|Optimization was completed|"
    r"stage completed|Loading board file|Saving '|Saved \d+ bytes)",
    re.IGNORECASE,
)
_UNFIXABLE_RE = re.compile(r"(\d+) unfixable violation")
_PREEXISTING_RE = re.compile(r"Board has (\d+) pre-existing clearance violation")


class FreeroutingProgress:
    """Turns Freerouting's log lines into progress events and a summary."""

    def __init__(self, ctx: Optional[JobContext], max_passes: int, attempt: int, attempts: int):
        self.ctx = ctx
        self.max_passes = max(1, int(max_passes or 1))
        self.attempt = attempt
        self.attempts = attempts
        self.best_score: Optional[float] = None
        self.best_unrouted: Optional[int] = None
        self.unrouted: Optional[int] = None
        self.violations: Optional[int] = None
        self.passes = 0
        self.stage = "loading"
        self.preexisting_violations: Optional[int] = None
        self.unfixable_violations: Optional[int] = None
        self.saw_stage = False

    def _progress(self, message: str) -> None:
        if self.ctx is None:
            return
        base = 0.12 + 0.72 * (self.attempt - 1) / self.attempts
        span = 0.72 / self.attempts
        frac = base + span * min(1.0, self.passes / self.max_passes)
        self.ctx.progress(frac, message)

    def feed(self, line: str) -> None:
        m = _PREEXISTING_RE.search(line)
        if m:
            self.preexisting_violations = int(m.group(1))
        m = _UNFIXABLE_RE.search(line)
        if m and self.unfixable_violations is None:
            self.unfixable_violations = int(m.group(1))
        improved = False
        m = _UNROUTED_RE.search(line)
        if m:
            self.unrouted, self.violations = int(m.group(1)), int(m.group(2))
            if self.best_unrouted is None or self.unrouted < self.best_unrouted:
                self.best_unrouted = self.unrouted
                improved = True
        if " pass #" in line:
            mm = re.search(r"pass #(\d+)", line)
            if mm and "Auto-routing" in line:
                self.passes = max(self.passes, int(mm.group(1)))
            ms = _SCORE_RE.search(line)
            if ms:
                score = float(ms.group(1))
                if self.best_score is None or score > self.best_score + 1e-9:
                    self.best_score = score
                    improved = True
            mf = _FANOUT_RE.search(line)
            if mf and int(mf.group(2)) > 0:
                improved = True
        if _STAGE_RE.search(line):
            self.saw_stage = True
            self.stage = line.strip()[-80:]
            improved = True
        elif not self.saw_stage and line.strip():
            improved = True  # still loading: any output counts
        if improved:
            parts = [f"Freerouting attempt {self.attempt}/{self.attempts}"]
            if self.passes:
                parts.append(f"pass {self.passes}")
            if self.unrouted is not None:
                parts.append(f"{self.unrouted} unrouted (Freerouting's count)")
            self._progress(", ".join(parts))

    def summary(self) -> Dict[str, Any]:
        return {
            "passes": self.passes,
            "unroutedReportedByFreerouting": self.unrouted,
            "violationsReportedByFreerouting": self.violations,
            "bestScore": self.best_score,
            "preexistingViolations": self.preexisting_violations,
            "unfixableViolations": self.unfixable_violations,
        }


# ---------------------------------------------------------------------------
# DRC evaluation
# ---------------------------------------------------------------------------

_NET_IN_DESC = re.compile(r"\[([^\]]+)\]")


def summarize_drc(drc_data: Dict[str, Any]) -> Dict[str, Any]:
    """Counts + keyed violation lists from a kicad-cli DRC JSON report."""
    unconnected = drc_data.get("unconnected_items", []) or []
    per_net: Dict[str, int] = {}
    for item in unconnected:
        descs = [it.get("description", "") for it in item.get("items", []) or []]
        nets = {m.group(1) for d in descs for m in [_NET_IN_DESC.search(d)] if m}
        for n in nets or {"?"}:
            per_net[n] = per_net.get(n, 0) + 1
    violations = []
    for v in drc_data.get("violations", []) or []:
        descs = sorted(it.get("description", "") for it in v.get("items", []) or [])
        pos = (v.get("items") or [{}])[0].get("pos") or {}
        violations.append(
            {
                "key": v.get("type", "unknown") + "|" + "|".join(descs),
                "type": v.get("type", "unknown"),
                "severity": v.get("severity", "error"),
                "message": v.get("description", ""),
                "location": {"x": pos.get("x"), "y": pos.get("y")},
                "items": descs,
            }
        )
    by_type: Dict[str, int] = {}
    for v in violations:
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    return {
        "unconnected": len(unconnected),
        "unconnectedByNet": dict(sorted(per_net.items(), key=lambda kv: (-kv[1], kv[0]))),
        "violations": violations,
        "errors": sum(1 for v in violations if v["severity"] == "error"),
        "warnings": sum(1 for v in violations if v["severity"] == "warning"),
        "byType": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def drc_delta(before: Dict[str, Any], after: Dict[str, Any], limit: int = 10) -> Dict[str, Any]:
    bk = {v["key"] for v in before.get("violations", [])}
    ak = {v["key"] for v in after.get("violations", [])}
    new = [v for v in after.get("violations", []) if v["key"] not in bk]
    resolved = [v for v in before.get("violations", []) if v["key"] not in ak]

    def brief(v: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v[k] for k in ("type", "severity", "message", "location", "items")}

    return {
        "unconnectedBefore": before.get("unconnected"),
        "unconnectedAfter": after.get("unconnected"),
        "errorsBefore": before.get("errors"),
        "errorsAfter": after.get("errors"),
        "newCount": len(new),
        "resolvedCount": len(resolved),
        "new": [brief(v) for v in new[:limit]],
        "resolved": [brief(v) for v in resolved[:limit]],
    }


def import_decision(
    mode: str, before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]
) -> Tuple[bool, str]:
    if mode == "always":
        return True, "importMode=always"
    if not before or not after:
        return False, "importMode=if_better but the result could not be evaluated"
    ub, ua = before["unconnected"], after["unconnected"]
    eb, ea = before.get("errors") or 0, after.get("errors") or 0
    if ua < ub and ea <= eb:
        return True, f"better: unconnected {ub} -> {ua}, DRC errors {eb} -> {ea}"
    if ua == ub and ea < eb:
        return True, f"better: unconnected unchanged ({ua}), DRC errors {eb} -> {ea}"
    return False, (
        f"not better: unconnected {ub} -> {ua}, DRC errors {eb} -> {ea} "
        "(if_better needs fewer unconnected items without more DRC errors)"
    )


# ---------------------------------------------------------------------------
# Staging (main thread)
# ---------------------------------------------------------------------------


def file_sha256(path: str) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def serialize_board(board: Any, path: str) -> bool:
    """Write the in-memory board to ``path`` without touching project settings."""
    import pcbnew

    try:
        return bool(pcbnew.SaveBoard(path, board, True))
    except TypeError:  # very old builds without aSkipSettings
        return False


def stage_project(board: Any, board_path: str, stage_dir: str) -> Dict[str, Any]:
    """Copy the in-memory board and its project rules into ``stage_dir``."""
    stem = os.path.splitext(os.path.basename(board_path))[0]
    staged = os.path.join(stage_dir, stem + ".kicad_pcb")
    source = "memory"
    if not serialize_board(board, staged):
        shutil.copyfile(board_path, staged)
        source = "disk"
    src_dir = os.path.dirname(os.path.abspath(board_path))
    for ext in (".kicad_pro", ".kicad_dru"):
        src = os.path.join(src_dir, stem + ext)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(stage_dir, stem + ext))
    prj_mod = src_dir.replace("\\", "/")
    for table in ("fp-lib-table", "sym-lib-table"):
        src = os.path.join(src_dir, table)
        if os.path.isfile(src):
            with open(src, "r", encoding="utf-8") as fh:
                text = fh.read()
            text = text.replace("${KIPRJMOD}", prj_mod).replace("$(KIPRJMOD)", prj_mod)
            with open(os.path.join(stage_dir, table), "w", encoding="utf-8") as fh:
                fh.write(text)
    return {
        "stem": stem,
        "staged": staged,
        "source": source,
        "baselineHash": file_sha256(staged),
        "diskHash": file_sha256(board_path),
    }


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def _worker_env() -> Dict[str, str]:
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_worker(ctx: JobContext, args: List[str], timeout: float = 900) -> Dict[str, Any]:
    report = os.path.join(tempfile.mkdtemp(prefix="kicad-mcp-worker-"), "report.json")
    cmd = [sys.executable, WORKER_SCRIPT] + args + ["--report", report]
    res = run_process(ctx, cmd, env=_worker_env(), timeout=timeout, log_prefix="[worker] ")
    try:
        with open(report, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {
            "ok": False,
            "error": f"worker produced no report (exit {res['returncode']}, {res['reason']}): "
            + " | ".join(res["tail"][-5:]),
        }
    finally:
        shutil.rmtree(os.path.dirname(report), ignore_errors=True)
    return data


def run_drc_json(
    ctx: JobContext, kicad_cli: str, board_file: str, timeout: float = 900
) -> Optional[Dict[str, Any]]:
    out = board_file + ".drc.json"
    cmd = [kicad_cli, "pcb", "drc", "--format", "json", "--output", out, "--units", "mm"]
    cmd += ["--refill-zones", board_file]
    res = run_process(ctx, cmd, timeout=timeout, log_prefix="[drc] ")
    if res["returncode"] != 0 or not os.path.isfile(out):
        # kicad-cli < 10 has no --refill-zones: retry without it.
        cmd.remove("--refill-zones")
        res = run_process(ctx, cmd, timeout=timeout, log_prefix="[drc] ")
    try:
        with open(out, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _fail(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "message": message, **extra}


def autoroute_job(ctx: JobContext, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Background body of an orchestrated autoroute (see module docstring)."""
    from commands.freerouting import (
        _build_freerouting_cmd,
        _describe_exit,
        _reconcile_ses_net_names,
        _score_ses,
        _strip_ses_placement,
        _trim_shared_freerouting_log,
    )

    t_start = time.time()
    stage = cfg["stageDir"]
    stem = cfg["stem"]
    staged = cfg["staged"]
    dsn_path = os.path.join(stage, stem + ".dsn")
    ses_path = os.path.join(stage, stem + ".ses")
    result: Dict[str, Any] = {
        "success": True,
        "pipeline": "orchestrated",
        "boardPath": cfg["boardPath"],
        "baselineSource": cfg["baselineSource"],
        "options": cfg["options"],
    }
    warnings: List[str] = list(cfg.get("warnings") or [])

    # 1. export ------------------------------------------------------------
    ctx.progress(0.02, "exporting DSN from the staged board copy")
    info = run_worker(ctx, ["export", "--board", staged, "--dsn", dsn_path], timeout=600)
    if not info.get("ok"):
        return _fail("DSN export failed", errorDetails=info.get("error"), logTail=_tail(ctx))
    if info.get("warning"):
        warnings.append(info["warning"])
    ctx.check_cancelled()

    # 2. rewrite -----------------------------------------------------------
    with open(dsn_path, "r", encoding="utf-8") as fh:
        dsn = fh.read()
    dsn_report: Dict[str, Any] = {"exportedFrom": "disk copy of the in-memory board"}
    dsn = grow_edge_keepouts(dsn, float(info.get("copperEdgeClearanceMm") or 0))
    dsn, dsn_report["ruleAreas"] = strip_rule_area_keepouts(
        dsn, info.get("ruleAreas") or [], cfg["stripRuleAreas"]
    )
    if cfg["lockExisting"]:
        dsn, dsn_report["lockedWiring"] = lock_existing_wiring(dsn)
    nets = cfg.get("nets")
    if nets:
        dsn, dsn_report["netFilter"] = restrict_to_nets(dsn, nets)
        dsn_report["netFilter"]["note"] = (
            "Pads of the other nets are exported without a net, so Freerouting reports "
            "their contacts with (fixed) copper as pre-existing 'unfixable' violations; "
            "that is expected and does not affect the routed nets."
        )
        if dsn_report["netFilter"]["unknownNets"]:
            warnings.append(
                "nets not found in the DSN (no pads?): "
                + ", ".join(dsn_report["netFilter"]["unknownNets"][:20])
            )
        routed_nets = set(dsn_report["netFilter"]["routedNets"])
    else:
        routed_nets = set(info.get("nets") or [])
    hard_regions, soft_regions = [], []
    copper_layers = cfg.get("copperLayers") or []
    for idx, entry in enumerate(cfg.get("forbid") or []):
        layers = entry.get("layers") or copper_layers
        reg = {"index": idx, "polygon": entry["polygon"], "layers": layers, "nets": entry["nets"]}
        if routed_nets and routed_nets <= set(entry["nets"]):
            hard_regions.append(reg)
        else:
            soft_regions.append(reg)
    if hard_regions:
        dsn, dsn_report["forbidKeepoutsAdded"] = add_region_keepouts(dsn, hard_regions)
    with open(dsn_path, "w", encoding="utf-8") as fh:
        fh.write(dsn)
    result["dsn"] = dsn_report
    ctx.check_cancelled()

    # 3. baseline evaluation --------------------------------------------
    kicad_cli = cfg.get("kicadCli")
    before = after = None
    if cfg["evaluate"]:
        if kicad_cli:
            ctx.progress(0.06, "baseline DRC (kicad-cli, zones refilled)")
            data = run_drc_json(ctx, kicad_cli, staged)
            before = summarize_drc(data) if data else None
        if before is None:
            r = run_worker(ctx, ["unconnected", "--board", staged], timeout=600)
            if r.get("ok"):
                before = {"unconnected": r["unconnected"], "violations": [], "errors": None}
                warnings.append("kicad-cli DRC unavailable: violations were not evaluated")
    ctx.check_cancelled()

    # 4. Freerouting --------------------------------------------------------
    attempts = cfg["attempts"]
    schedule = cfg["passSchedule"]
    attempt_results = []
    best = None
    best_tail: List[str] = []
    stop_reason = None
    for idx in range(attempts):
        ctx.check_cancelled()
        mp = cfg["maxPasses"] if attempts == 1 else schedule[idx % len(schedule)]
        try:
            os.remove(ses_path)
        except FileNotFoundError:
            pass
        cmd = _build_freerouting_cmd(
            cfg["jar"], dsn_path, ses_path, mp, cfg["useDocker"], single_thread=attempts > 1
        )
        tracker = FreeroutingProgress(ctx, mp, idx + 1, attempts)
        ctx.log(f"--- Freerouting attempt {idx + 1}/{attempts} (max passes {mp}) ---")
        res = run_process(
            ctx,
            cmd,
            cwd=stage,
            timeout=cfg["timeout"],
            stall_timeout=cfg.get("stallTimeout"),
            on_line=tracker.feed,
        )
        entry = {
            "attempt": idx + 1,
            "max_passes": mp,
            "elapsed_seconds": res["elapsed"],
            "stopReason": res["reason"],
            **tracker.summary(),
        }
        if res["reason"] == "cancelled":
            entry["ok"] = False
            attempt_results.append(entry)
            stop_reason = "cancelled"
            break
        if res["reason"] in ("timeout", "stalled") or res["returncode"] != 0:
            entry["ok"] = False
            if res["reason"] == "stalled":
                entry["error"] = (
                    f"no progress for {cfg.get('stallTimeout')} s (no better pass score, no "
                    "fewer unrouted, no new stage); Freerouting was stopped and wrote no SES"
                )
            elif res["reason"] == "timeout":
                entry["error"] = f"attempt timed out after {cfg['timeout']} s"
            else:
                entry["error"] = f"exit code {res['returncode']}{_describe_exit(res['returncode'])}"
            attempt_results.append(entry)
            stop_reason = res["reason"]
            continue
        if not os.path.isfile(ses_path):
            entry.update(ok=False, error="no SES produced")
            attempt_results.append(entry)
            continue
        with open(ses_path, "r", encoding="utf-8", errors="replace") as fh:
            ses_text = fh.read()
        score = _score_ses(ses_text, cfg.get("targetNets") or [])
        entry.update(ok=True, **score)
        attempt_results.append(entry)
        # Fewer unrouted (Freerouting's own count) wins; SES score breaks ties.
        rank = (
            -(tracker.unrouted if tracker.unrouted is not None else 10**9),
            score["score"],
        )
        if best is None or rank > best[0]:
            best = (rank, idx, ses_text)
            best_tail = list(res["tail"])
    _trim_shared_freerouting_log()
    result["freerouting"] = {
        "mode": "docker" if cfg["useDocker"] else "direct",
        "attempts": attempt_results,
        "stopReason": stop_reason,
    }
    if best is None:
        result.update(
            success=False,
            message="Freerouting produced no usable result",
            errorDetails=(attempt_results[-1].get("error") if attempt_results else "no attempts"),
            logTail=_tail(ctx),
            elapsedSeconds=round(time.time() - t_start, 1),
        )
        if before:
            result["kicad"] = {"before": _brief(before)}
        result["warnings"] = warnings
        return result
    result["freerouting"]["bestAttempt"] = best[1] + 1
    ctx.check_cancelled()

    # 5. SES post-processing -------------------------------------------------
    ctx.progress(0.86, "preparing SES import")
    ses_text = _strip_ses_placement(best[2])
    ses_text, remapped = _reconcile_ses_net_names(ses_text, info.get("nets") or [])
    import_nets = set(routed_nets)
    rejected = []
    if soft_regions:
        copper = ses_new_copper(ses_text)
        for reg in soft_regions:
            layers = set(reg["layers"]) if reg.get("layers") else None
            for net in reg["nets"]:
                if net in import_nets and _copper_enters(
                    copper.get(net, []), reg["polygon"], layers
                ):
                    import_nets.discard(net)
                    rejected.append({"net": net, "region": reg["index"]})
    if rejected:
        result["forbiddenRegionRejected"] = rejected
        warnings.append(
            f"{len(rejected)} net(s) routed through a forbidden region were not imported "
            "(their connections stay unrouted); route them in a separate run with only the "
            "forbidden nets, which turns the region into a hard keepout"
        )
    ses_text, dropped = filter_ses_nets(ses_text, import_nets)
    import_ses = os.path.join(stage, stem + ".import.ses")
    with open(import_ses, "w", encoding="utf-8") as fh:
        fh.write(ses_text)
    nets_file = os.path.join(stage, "import_nets.json")
    with open(nets_file, "w", encoding="utf-8") as fh:
        json.dump(sorted(import_nets), fh)

    # 6. import into a copy -------------------------------------------------
    ctx.progress(0.9, "importing SES into a board copy")
    routed_dir = os.path.join(stage, "routed")
    os.makedirs(routed_dir, exist_ok=True)
    for ext in (".kicad_pro", ".kicad_dru"):
        src = os.path.join(stage, stem + ext)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(routed_dir, stem + ext))
    for table in ("fp-lib-table", "sym-lib-table"):
        if os.path.isfile(os.path.join(stage, table)):
            shutil.copyfile(os.path.join(stage, table), os.path.join(routed_dir, table))
    routed = os.path.join(routed_dir, stem + ".kicad_pcb")
    args = ["import", "--board", staged, "--ses", import_ses, "--out", routed]
    args += ["--import-nets", nets_file]
    if cfg["lockExisting"]:
        args.append("--lock-all-existing")
    if cfg["lockRouted"]:
        args.append("--lock-routed")
    imp = run_worker(ctx, args, timeout=900)
    if not imp.get("ok"):
        result.update(
            success=False,
            message="SES import into the board copy failed (the board was not changed)",
            errorDetails=imp.get("error"),
            logTail=_tail(ctx),
        )
        result["warnings"] = warnings
        return result
    result["import"] = {
        k: v for k, v in imp.items() if k not in ("ok",) and not k.startswith("unconnected")
    }
    if remapped:
        result["import"]["netsRemapped"] = sorted(set(remapped))
    ctx.check_cancelled()

    # 7. evaluate ---------------------------------------------------------------
    if cfg["evaluate"]:
        if kicad_cli:
            ctx.progress(0.95, "DRC on the routed copy")
            data = run_drc_json(ctx, kicad_cli, routed)
            after = summarize_drc(data) if data else None
        if after is None:
            after = {
                "unconnected": imp.get("unconnectedAfterRefill"),
                "violations": [],
                "errors": None,
            }
    if before or after:
        result["kicad"] = {
            "before": _brief(before) if before else None,
            "after": _brief(after) if after else None,
            "note": (
                "Counts are KiCad's (zones refilled): plane connections that a zone "
                "satisfies are not unrouted, unlike Freerouting's own count."
            ),
        }
    if before and after:
        result["drcDelta"] = drc_delta(before, after)

    # 8. decide ---------------------------------------------------------------
    do_import, why = import_decision(cfg["importMode"], before, after)
    result["decision"] = {"import": do_import, "mode": cfg["importMode"], "reason": why}
    result["routedCopy"] = routed
    result["logTail"] = best_tail[-25:]
    result["elapsedSeconds"] = round(time.time() - t_start, 1)
    if warnings:
        result["warnings"] = warnings
    if cfg.get("keepArtifacts"):
        kept = {}
        for src in (dsn_path, ses_path):
            if os.path.isfile(src):
                dest = os.path.join(os.path.dirname(cfg["boardPath"]), os.path.basename(src))
                shutil.copy2(src, dest)
                kept[os.path.basename(src)] = dest
        result["artifacts"] = kept
    result["message"] = f"Routing finished in {result['elapsedSeconds']} s; " + (
        "ready to apply" if do_import else "result NOT applied: " + why
    )
    ctx.progress(1.0, result["message"])
    return result


def _tail(ctx: JobContext, n: int = 25) -> List[str]:
    job = JOBS.get(ctx.job_id)
    return list(job.log)[-n:] if job else []


def _brief(summary: Dict[str, Any]) -> Dict[str, Any]:
    by_net = summary.get("unconnectedByNet") or {}
    return {
        "unconnected": summary.get("unconnected"),
        "unconnectedByNet": dict(list(by_net.items())[:15]),
        "drcErrors": summary.get("errors"),
        "drcWarnings": summary.get("warnings"),
        "violationsByType": summary.get("byType"),
    }


# ---------------------------------------------------------------------------
# Apply step (main thread)
# ---------------------------------------------------------------------------


def make_apply_fn(owner: Any, cfg: Dict[str, Any]):
    """Build the job's apply step. ``owner`` has ``.board`` (FreeroutingCommands)."""

    def apply_fn(job: Job, params: Dict[str, Any]) -> Dict[str, Any]:
        res = job.result or {}
        board_path = cfg["boardPath"]
        if not res.get("success"):
            return _fail("The routing job did not succeed; nothing to apply")
        decision = res.get("decision") or {}
        if not decision.get("import") and not params.get("importAnyway"):
            return _fail(
                "The routed result was not accepted: " + str(decision.get("reason")),
                hint="Pass importAnyway=true to job_apply to write it anyway.",
                kicad=res.get("kicad"),
            )
        routed = res.get("routedCopy")
        if not routed or not os.path.isfile(routed):
            return _fail("The routed board copy is gone (job pruned?); run autoroute again")
        if file_sha256(board_path) != cfg["diskHash"]:
            return _fail(
                "The board file changed on disk since routing started; the result was not "
                "applied so that change is not lost. Run autoroute again."
            )
        board = getattr(owner, "board", None)
        if board is not None and cfg["baselineSource"] == "memory":
            probe_dir = tempfile.mkdtemp(prefix="kicad-mcp-apply-")
            try:
                probe = os.path.join(probe_dir, "probe.kicad_pcb")
                if serialize_board(board, probe) and file_sha256(probe) != cfg["baselineHash"]:
                    return _fail(
                        "The board in memory changed since routing started; the result was "
                        "not applied so those edits are not lost. Run autoroute again."
                    )
            finally:
                shutil.rmtree(probe_dir, ignore_errors=True)
        tmp = board_path + ".mcp-autoroute.tmp"
        shutil.copyfile(routed, tmp)
        os.replace(tmp, board_path)
        out = {
            "success": True,
            "message": f"Routed board written to {board_path} and reloaded",
            "boardPath": board_path,
            "kicad": res.get("kicad"),
            "decision": decision,
            "_boardFileWritten": {"mode": "disk", "path": board_path},
        }
        if not decision.get("import"):
            out["warnings"] = ["Applied with importAnyway although: " + str(decision.get("reason"))]
        if not cfg.get("keepStage"):
            shutil.rmtree(cfg["stageDir"], ignore_errors=True)
        return out

    return apply_fn


def cleanup_fn(cfg: Dict[str, Any]):
    def _cleanup(job: Job) -> None:
        shutil.rmtree(cfg["stageDir"], ignore_errors=True)

    return _cleanup
