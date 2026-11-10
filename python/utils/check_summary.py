"""Compact, filterable, delta-aware DRC/ERC results (MCP_ISSUES Q5, principles 3-4).

``run_drc`` and ``run_erc`` used to return either everything (ERC: every
violation) or counts only (DRC: a file path for the rest). A design loop needs
neither: it needs counts, the first few problems with coordinates, and what
changed since the last run. This module provides that, shared by both tools:

* ``normalize_violation``  -- one compact dict per violation, with a stable key
* ``filter_violations``    -- ``ignore`` type globs, ``nets``, ``refs``, ``region``
* ``build_check_response`` -- ``detail`` = summary | items | full, ``limit``
* ``load_last_run`` / ``record_run`` -- per-file history used for the delta and
  by ``project_status`` (which reports the last counts without re-running).

History lives next to the checked file in ``.kicad-mcp/checks/`` (falling back
to the user data dir when the project directory is read-only), keyed by the
checked file's name and check kind.
"""

import fnmatch
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

DETAIL_LEVELS = ("summary", "items", "full")
DEFAULT_LIMIT = 20
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
HISTORY_DIRNAME = ".kicad-mcp"


# ---------------------------------------------------------------------------
# Normalisation and keys
# ---------------------------------------------------------------------------


def violation_key(vtype: str, items: Iterable[Dict[str, Any]], message: str = "") -> str:
    """A key that survives re-runs: violation type plus the items it names.

    Item UUIDs are stable across runs (KiCad keeps them in the file); when an
    item has none, its description is used. Positions are deliberately left
    out so that nudging a footprint doesn't turn an old problem into a "new"
    one.
    """
    parts = []
    for it in items or []:
        ident = it.get("uuid") or it.get("description") or ""
        parts.append(str(ident))
    if not parts:
        parts.append(message or "")
    return vtype + "|" + "|".join(sorted(parts))


def normalize_violation(raw: Dict[str, Any], category: Optional[str] = None) -> Dict[str, Any]:
    """Compact form of a kicad-cli violation (DRC or ERC JSON)."""
    items = raw.get("items", []) or []
    vtype = raw.get("type", "unknown")
    message = raw.get("description", raw.get("message", ""))
    loc: Dict[str, Any] = {}
    if items and isinstance(items[0].get("pos"), dict):
        loc = {"x": items[0]["pos"].get("x", 0), "y": items[0]["pos"].get("y", 0)}
    out = {
        "key": violation_key(vtype, items, message),
        "type": vtype,
        "severity": raw.get("severity", "error"),
        "message": message,
        "location": loc,
        "items": [
            {
                "description": it.get("description", ""),
                "uuid": it.get("uuid"),
                "pos": it.get("pos"),
            }
            for it in items
        ],
    }
    if category:
        out["category"] = category
    return out


def compact(v: Dict[str, Any], with_items: bool = True) -> Dict[str, Any]:
    """The per-item shape returned for detail=summary/items."""
    out = {
        "type": v["type"],
        "severity": v["severity"],
        "message": v["message"],
        "location": v.get("location") or {},
    }
    if v.get("category") and v["category"] != "violations":
        out["category"] = v["category"]
    if with_items and v.get("items"):
        out["items"] = [it.get("description", "") for it in v["items"]]
    return out


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return [str(v) for v in value if str(v).strip()]


def parse_region(region: Any) -> Optional[Tuple[float, float, float, float]]:
    if not region:
        return None
    if isinstance(region, dict):
        try:
            x1, y1 = float(region["x1"]), float(region["y1"])
            x2, y2 = float(region["x2"]), float(region["y2"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("region must be {x1, y1, x2, y2} in mm")
    else:
        try:
            x1, y1, x2, y2 = (float(v) for v in region)
        except (TypeError, ValueError):
            raise ValueError("region must be {x1, y1, x2, y2} in mm")
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _texts(v: Dict[str, Any]) -> List[str]:
    return [v.get("message", "")] + [it.get("description", "") for it in v.get("items", [])]


_BRACKETED = re.compile(r"\[([^\]]+)\]|'([^']+)'|\"([^\"]+)\"")


def _mentions_net(v: Dict[str, Any], nets: List[str]) -> bool:
    """Net names appear as ``[NET]`` (pads, tracks, zones) or quoted (labels).

    ``+5V`` also matches the hierarchical ``/Power/+5V``.
    """
    for text in _texts(v):
        for m in _BRACKETED.finditer(text):
            name = next(g for g in m.groups() if g is not None)
            for net in nets:
                if name == net or name.endswith("/" + net.lstrip("/")):
                    return True
    return False


def _mentions_ref(v: Dict[str, Any], refs: List[str]) -> bool:
    for text in _texts(v):
        for ref in refs:
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(ref) + r"(?![A-Za-z0-9_])", text):
                return True
    return False


def _in_region(v: Dict[str, Any], box: Tuple[float, float, float, float]) -> bool:
    x1, y1, x2, y2 = box
    points = [it.get("pos") for it in v.get("items", []) if isinstance(it.get("pos"), dict)]
    if not points and v.get("location"):
        points = [v["location"]]
    for p in points:
        try:
            x, y = float(p.get("x", 0)), float(p.get("y", 0))
        except (TypeError, ValueError):
            continue
        if x1 <= x <= x2 and y1 <= y <= y2:
            return True
    return False


def filter_violations(
    violations: List[Dict[str, Any]], params: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Apply ``ignore``/``nets``/``refs``/``region``/``severity`` from params.

    Returns (kept, info) where info describes the filters and how many
    violations were dropped by ``ignore``.
    """
    ignore = _as_list(params.get("ignore"))
    nets = _as_list(params.get("nets"))
    refs = _as_list(params.get("refs"))
    box = parse_region(params.get("region"))
    severity = params.get("severity")
    if severity in (None, "", "all"):
        severity = None

    kept = []
    ignored = 0
    for v in violations:
        if ignore and any(fnmatch.fnmatchcase(v["type"], pat) for pat in ignore):
            ignored += 1
            continue
        if severity and v["severity"] != severity:
            continue
        if nets and not _mentions_net(v, nets):
            continue
        if refs and not _mentions_ref(v, refs):
            continue
        if box and not _in_region(v, box):
            continue
        kept.append(v)

    info: Dict[str, Any] = {}
    if ignore:
        info["ignore"] = ignore
        info["ignoredCount"] = ignored
    if nets:
        info["nets"] = nets
    if refs:
        info["refs"] = refs
    if box:
        info["region"] = {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]}
    if severity:
        info["severity"] = severity
    return kept, info


def _sorted(violations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(violations, key=lambda v: (_SEVERITY_ORDER.get(v["severity"], 3), v["type"]))


def counts(violations: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_severity = {"error": 0, "warning": 0, "info": 0}
    by_type: Dict[str, int] = {}
    for v in violations:
        if v["severity"] in by_severity:
            by_severity[v["severity"]] += 1
        by_type[v["type"]] = by_type.get(v["type"], 0) + 1
    by_type = dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])))
    return {"total": len(violations), "by_severity": by_severity, "by_type": by_type}


# ---------------------------------------------------------------------------
# History (previous run) for deltas
# ---------------------------------------------------------------------------


def _history_candidates(kind: str, target_path: str) -> List[Path]:
    target = Path(target_path).resolve()
    name = f"{target.name}.{kind}.json"
    local = target.parent / HISTORY_DIRNAME / "checks" / name
    digest = hashlib.sha1(str(target).lower().encode("utf-8")).hexdigest()[:16]
    try:
        from utils.platform_helper import PlatformHelper

        fallback = PlatformHelper.get_data_dir() / "check_history" / f"{digest}.{name}"
    except Exception:  # pragma: no cover - PlatformHelper always importable in-tree
        fallback = Path.home() / ".kicad-mcp" / "check_history" / f"{digest}.{name}"
    return [local, fallback]


def load_last_run(kind: str, target_path: str) -> Optional[Dict[str, Any]]:
    """The last recorded run of ``kind`` ("drc"/"erc") for ``target_path``, or None."""
    best = None
    for path in _history_candidates(kind, target_path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if best is None or data.get("timestamp", 0) > best.get("timestamp", 0):
            best = data
    return best


def record_run(kind: str, target_path: str, violations: List[Dict[str, Any]]) -> Optional[str]:
    """Persist this run (atomically) and return where it was written."""
    record = {
        "kind": kind,
        "target": str(Path(target_path).resolve()),
        "timestamp": time.time(),
        **counts(violations),
        "violations": [
            {
                "key": v["key"],
                "type": v["type"],
                "severity": v["severity"],
                "message": v["message"],
                "location": v.get("location") or {},
                "category": v.get("category"),
                "items": [
                    {"description": it.get("description", ""), "pos": it.get("pos")}
                    for it in v.get("items", [])
                ],
            }
            for v in violations
        ],
    }
    payload = json.dumps(record)
    for path in _history_candidates(kind, target_path):
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
            return str(path)
        except OSError as exc:
            logger.debug(f"Cannot write check history {path}: {exc}")
            try:
                tmp.unlink()
            except OSError:
                pass
    return None


def compute_delta(
    previous: Optional[Dict[str, Any]],
    current: List[Dict[str, Any]],
    params: Dict[str, Any],
    limit: Optional[int],
) -> Dict[str, Any]:
    """New and resolved violations versus ``previous`` (filtered like the view)."""
    if not previous:
        return {
            "firstRun": True,
            "newCount": 0,
            "resolvedCount": 0,
            "newCountAll": 0,
            "resolvedCountAll": 0,
            "new": [],
            "resolved": [],
        }
    prev_violations = previous.get("violations", []) or []
    prev_keys = {v.get("key") for v in prev_violations}
    cur_keys = {v["key"] for v in current}
    new = [v for v in current if v["key"] not in prev_keys]
    resolved = [
        {
            "key": v.get("key", ""),
            "type": v.get("type", "unknown"),
            "severity": v.get("severity", "error"),
            "message": v.get("message", ""),
            "location": v.get("location") or {},
            "items": v.get("items") or [],
        }
        for v in prev_violations
        if v.get("key") not in cur_keys
    ]
    all_new, all_resolved = len(new), len(resolved)
    new, _ = filter_violations(new, params)
    resolved, _ = filter_violations(resolved, params)
    cap = limit if limit is not None else len(new) + len(resolved)
    return {
        "firstRun": False,
        "previousRunAt": _iso(previous.get("timestamp")),
        "previousTotal": previous.get("total"),
        # Counts/lists below follow the call's filters; the *All* counts don't.
        "newCount": len(new),
        "resolvedCount": len(resolved),
        "newCountAll": all_new,
        "resolvedCountAll": all_resolved,
        "new": [compact(v) for v in _sorted(new)[:cap]],
        "resolved": [compact(v) for v in _sorted(resolved)[:cap]],
    }


def _iso(ts: Any) -> Optional[str]:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------


def parse_detail_params(params: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """Validate ``detail`` and ``limit``. ``limit`` defaults to 20 for summary
    and to "all" for items/full (unless given)."""
    detail = (params.get("detail") or "summary").lower()
    if detail not in DETAIL_LEVELS:
        raise ValueError(f"detail must be one of {', '.join(DETAIL_LEVELS)}")
    raw = params.get("limit")
    if raw is None:
        limit: Optional[int] = DEFAULT_LIMIT if detail == "summary" else None
    else:
        try:
            limit = max(0, int(raw))
        except (TypeError, ValueError):
            raise ValueError("limit must be a non-negative integer")
    return detail, limit


def build_check_response(
    kind: str,
    target_path: str,
    violations: List[Dict[str, Any]],
    params: Dict[str, Any],
    record: bool = True,
) -> Dict[str, Any]:
    """Counts + first ``limit`` items + delta, with filters applied.

    ``violations`` are normalised dicts (``normalize_violation``) for the whole
    design; the history always stores the unfiltered set so a filtered call
    never corrupts the next delta.
    """
    detail, limit = parse_detail_params(params)
    previous = load_last_run(kind, target_path)
    view, filter_info = filter_violations(violations, params)
    delta = compute_delta(previous, violations, params, limit)
    history_file = record_run(kind, target_path, violations) if record else None

    view_sorted = _sorted(view)
    shown = view_sorted if limit is None else view_sorted[:limit]
    view_counts = counts(view)
    out: Dict[str, Any] = {
        "detail": detail,
        "counts": view_counts,
        "items": [compact(v, with_items=detail != "summary") for v in shown],
        "itemsShown": len(shown),
        "itemsTruncated": len(shown) < len(view),
        "delta": delta,
    }
    if filter_info:
        out["filters"] = filter_info
        out["unfilteredTotal"] = len(violations)
    if history_file:
        out["historyFile"] = history_file
    if detail == "full":
        out["items"] = [dict(v) for v in shown]
    return out
