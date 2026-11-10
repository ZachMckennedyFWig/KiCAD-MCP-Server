"""Custom DRC rules with feedback (MCP_ISSUES C2).

``add_drc_rule`` appends KiCad custom rules to the project's ``.kicad_dru``
and says whether they did anything. Two rules in the reporter's project
(``zone_connection``, ``min_resolved_spokes``) silently did nothing, and
kicad-cli gives no sign when it rejects a rules file: a file with a syntax
error is dropped and DRC runs with no custom rules at all (verified on KiCad
10.0.8). So each call:

1. checks the text (balanced expressions, rule structure, constraint names
   and values) with ``utils.kicad_dru``;
2. runs ``kicad-cli pcb drc`` on two temporary copies of the project -- the
   rules before and after the change -- each with a *canary* rule that must
   fire on a known pad. No canary violation means KiCad rejected the file;
3. adds an ``assertion`` *probe* with the new rule's condition, which fires
   once per item the condition matches (KiCad caps a violation type at 199
   reports, so larger counts are lower bounds);
4. writes the file atomically only when everything passed (``dryRun`` writes
   nothing), and reports the violations the change added or resolved and how
   many cite the new rule.

``list_drc_rules`` and ``remove_drc_rule`` read and edit the same file.
"""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from utils import check_summary
from utils.kicad_cli import kicad_cli_not_found_message, resolve_kicad_cli
from utils.kicad_dru import (
    DruSyntaxError,
    append_rule_text,
    parse_rules_file,
    read_dru,
    remove_rule_spans,
    write_dru_atomic,
)

logger = logging.getLogger("kicad_interface")

PROBE_PREFIX = "__mcp_probe_"
CANARY_NAME = "__mcp_canary"
# kicad-cli stops reporting a violation type after this many items.
KICAD_REPORT_CAP = 199
_NEVER = "A.Type == '__mcp_never__'"


def _fail(message: str, details: Any = None, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": False, "message": message}
    if details is not None:
        out["errorDetails"] = details
    out.update(extra)
    return out


class DrcRuleCommands:
    """add_drc_rule / list_drc_rules / remove_drc_rule."""

    def __init__(self, board: Optional[Any] = None):
        self.board = board

    # ------------------------------------------------------------------ paths

    def _board_file(self, params: Dict[str, Any]) -> Optional[str]:
        path = params.get("boardPath")
        if not path and self.board is not None:
            try:
                path = self.board.GetFileName()
            except Exception:
                path = None
        return os.path.abspath(path) if path else None

    def _project(
        self, params: Dict[str, Any]
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """(board file, .kicad_pro, .kicad_dru) that kicad-cli would use for this board."""
        board = self._board_file(params)
        project = params.get("projectPath")
        pro = None
        if project:
            project = os.path.abspath(os.path.expanduser(str(project)))
            if os.path.isdir(project):
                found = sorted(glob.glob(os.path.join(project, "*.kicad_pro")))
                pro = found[0] if len(found) == 1 else None
            elif project.endswith(".kicad_pro"):
                pro = project
        elif board:
            own = os.path.splitext(board)[0] + ".kicad_pro"
            if os.path.isfile(own):
                pro = own
            else:
                found = sorted(glob.glob(os.path.join(os.path.dirname(board), "*.kicad_pro")))
                pro = found[0] if len(found) == 1 else None
        if pro:
            dru = os.path.splitext(pro)[0] + ".kicad_dru"
        elif board:
            dru = os.path.splitext(board)[0] + ".kicad_dru"
        else:
            dru = None
        return board, pro, dru

    # ------------------------------------------------------------------ list

    def list_drc_rules(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """List the custom rules in the project's ``.kicad_dru``."""
        _, _, dru = self._project(params)
        if not dru:
            return _fail("No board or project is loaded", "Open a board or pass projectPath")
        text = read_dru(dru)
        try:
            parsed = parse_rules_file(text)
        except DruSyntaxError as exc:
            return _fail(
                "The rules file has a syntax error; KiCad ignores the whole file",
                exc.as_dict(),
                druFile=dru,
            )
        with_text = bool(params.get("includeText", False))
        rules = []
        for r in parsed["rules"]:
            entry = {k: r[k] for k in ("name", "line", "condition", "layer", "severity")}
            entry["constraints"] = r["constraints"]
            if with_text:
                entry["text"] = text[r["start"] : r["end"]]
            rules.append(entry)
        message = f"{len(rules)} custom rule(s) in {os.path.basename(dru)}"
        if parsed["errors"]:
            message += (
                f"; {len(parsed['errors'])} problem(s) make KiCad ignore the whole file "
                "(see errors)"
            )
        return {
            "success": True,
            "message": message,
            "fileRejected": bool(parsed["errors"]),
            "druFile": dru,
            "exists": os.path.isfile(dru),
            "version": parsed["version"],
            "rules": rules,
            "errors": parsed["errors"],
            "warnings": parsed["warnings"],
        }

    # ------------------------------------------------------------------ remove

    def remove_drc_rule(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Remove rules by name (``name`` or ``names``); atomic write."""
        names = params.get("names") or ([params["name"]] if params.get("name") else [])
        if isinstance(names, str):
            names = [names]
        if not names:
            return _fail("name (or names) is required")
        _, _, dru = self._project(params)
        if not dru or not os.path.isfile(dru):
            return _fail("No .kicad_dru rules file found", f"looked for {dru}")
        text = read_dru(dru)
        try:
            parsed = parse_rules_file(text)
        except DruSyntaxError as exc:
            return _fail("The rules file has a syntax error", exc.as_dict(), druFile=dru)
        wanted = set(str(n) for n in names)
        spans = [(r["start"], r["end"]) for r in parsed["rules"] if r["name"] in wanted]
        found = {r["name"] for r in parsed["rules"] if r["name"] in wanted}
        missing = sorted(wanted - found)
        if missing:
            return _fail(
                f"Rule(s) not found: {', '.join(missing)}",
                f"rules in {os.path.basename(dru)}: "
                + ", ".join(r["name"] for r in parsed["rules"]),
                druFile=dru,
            )
        new_text = remove_rule_spans(text, spans)
        dry_run = bool(params.get("dryRun", False))
        if not dry_run:
            write_dru_atomic(dru, new_text)
        return {
            "success": True,
            "message": (
                f"{'Would remove' if dry_run else 'Removed'} {len(spans)} rule(s) from "
                f"{os.path.basename(dru)}"
            ),
            "druFile": dru,
            "removed": sorted(found),
            "remaining": len(parsed["rules"]) - len(spans),
            "dryRun": dry_run,
        }

    # ------------------------------------------------------------------ add

    def add_drc_rule(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Validate, measure and append custom rule(s) to the project's ``.kicad_dru``."""
        rule_text = params.get("ruleText") or params.get("rule_text") or params.get("rule")
        if not rule_text or not str(rule_text).strip():
            return _fail(
                'ruleText is required, e.g. (rule "name" (condition "...") (constraint ...))'
            )
        rule_text = str(rule_text).strip()
        board, pro, dru = self._project(params)
        if not dru:
            return _fail("No board or project is loaded", "Open a board or pass projectPath")

        # 1. Text checks on the new rule(s).
        try:
            new = parse_rules_file(rule_text)
        except DruSyntaxError as exc:
            return _fail("Rule syntax error", exc.as_dict(), syntaxOk=False)
        if new["errors"]:
            return _fail("Rule is not valid", new["errors"], syntaxOk=False)
        if not new["rules"]:
            return _fail("No (rule ...) found in ruleText", syntaxOk=False)
        snippet = "\n\n".join(rule_text[r["start"] : r["end"]] for r in new["rules"])
        new_names = [r["name"] for r in new["rules"]]
        reserved = [n for n in new_names if n.startswith(PROBE_PREFIX) or n == CANARY_NAME]
        if reserved:
            return _fail(f"Rule names starting with '{PROBE_PREFIX}' are reserved: {reserved}")

        existing_text = read_dru(dru)
        try:
            existing = parse_rules_file(existing_text) if existing_text.strip() else None
        except DruSyntaxError as exc:
            return _fail(
                f"The existing {os.path.basename(dru)} has a syntax error, so KiCad ignores it",
                exc.as_dict(),
                druFile=dru,
            )
        existing_rules = existing["rules"] if existing else []
        clashes = [r for r in existing_rules if r["name"] in new_names]
        replace = bool(params.get("replace", False))
        if clashes and not replace:
            return _fail(
                f"Rule(s) already exist: {', '.join(sorted({r['name'] for r in clashes}))}",
                "Pass replace=true to replace them, or choose another name",
                druFile=dru,
            )
        base_text = remove_rule_spans(existing_text, [(r["start"], r["end"]) for r in clashes])
        new_text = append_rule_text(base_text, snippet)
        try:
            check = parse_rules_file(new_text)
        except DruSyntaxError as exc:  # pragma: no cover - both halves parsed
            return _fail("Combined rules file does not parse", exc.as_dict())

        if check["errors"]:
            # Rules this call does not replace are broken: KiCad would drop the
            # whole file, so the new rule could never take effect.
            return _fail(
                f"The existing {os.path.basename(dru)} has rules KiCad rejects, so it ignores "
                "the whole file (no custom rule applies). Fix them first: remove_drc_rule, or "
                "add a corrected rule of the same name with replace=true",
                check["errors"],
                druFile=dru,
                existingErrors=check["errors"],
            )
        warnings = list(new["warnings"])

        result: Dict[str, Any] = {
            "success": True,
            "druFile": dru,
            "syntaxOk": True,
            "rules": [
                {k: r[k] for k in ("name", "condition", "layer", "severity", "constraints")}
                for r in new["rules"]
            ],
            "replaced": sorted({r["name"] for r in clashes}),
            "ruleCount": len(check["rules"]),
            "warnings": warnings,
        }

        # 2/3. Measure with kicad-cli.
        measure = bool(params.get("measure", True))
        if measure:
            measured = self._measure(params, board, pro, existing_text, new_text, new["rules"])
            if measured.get("error"):
                warnings.append(f"Could not measure the rule with kicad-cli: {measured['error']}")
                result["kicadAccepted"] = None
            else:
                result.update(measured)
                if measured.get("kicadAccepted") is False:
                    result.update(
                        success=False,
                        written=False,
                        message=(
                            "KiCad rejected the rules file with this rule (its DRC silently "
                            "dropped every custom rule), so nothing was written. Check "
                            "condition expressions, property names and layer names."
                        ),
                    )
                    return result
        else:
            result["kicadAccepted"] = None

        # 4. Write.
        dry_run = bool(params.get("dryRun", False))
        if not dry_run:
            write_dru_atomic(dru, new_text)
        result["written"] = not dry_run
        result["dryRun"] = dry_run
        names = ", ".join(new_names)
        verb = "Would add" if dry_run else ("Replaced" if clashes else "Added")
        msg = f"{verb} rule(s) {names} in {os.path.basename(dru)}"
        drc = result.get("drc")
        if drc:
            msg += (
                f"; DRC {drc['before']} -> {drc['after']} violations "
                f"({drc['newCount']} new, {drc['resolvedCount']} resolved)"
            )
        result["message"] = msg
        return result

    # ------------------------------------------------------------------ measurement

    def _canary_net(self) -> Optional[str]:
        """The net with the fewest pads (>= 1), for a canary that fires a few times."""
        if self.board is None:
            return None
        counts: Dict[str, int] = {}
        try:
            for fp in self.board.GetFootprints():
                for pad in fp.Pads():
                    net = pad.GetNetname()
                    if net and "'" not in net and '"' not in net and "\\" not in net:
                        counts[net] = counts.get(net, 0) + 1
        except Exception:
            return None
        if not counts:
            return None
        return min(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

    @staticmethod
    def _probe_rules(rules: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
        """Assertion probes that fire once per item a new rule's condition matches."""
        text = []
        info: Dict[str, Dict[str, Any]] = {}
        for i, r in enumerate(rules):
            cond = r.get("condition")
            if not cond:
                info[r["name"]] = {"count": None, "reason": "no condition: applies to every item"}
                continue
            if re.search(r"\bB\.", cond):
                info[r["name"]] = {
                    "count": None,
                    "reason": "the condition tests a second item (B.*), which a "
                    "single-item probe cannot count; see citingRule instead",
                }
                continue
            name = f"{PROBE_PREFIX}{i}"
            cond_q = cond.replace("\\", "\\\\").replace('"', '\\"')
            layer = f' (layer "{r["layer"]}")' if r.get("layer") else ""
            text.append(
                f'(rule "{name}" (condition "{cond_q}"){layer} (constraint assertion "{_NEVER}"))'
            )
            info[r["name"]] = {"probe": name}
        return "\n".join(text), info

    def _stage(self, board: str, pro: Optional[str], dru_text: str) -> Tuple[str, str]:
        stage = tempfile.mkdtemp(prefix="kicad-mcp-dru-")
        stem = os.path.splitext(os.path.basename(pro or board))[0]
        staged_board = os.path.join(stage, stem + ".kicad_pcb")
        shutil.copyfile(board, staged_board)
        if pro and os.path.isfile(pro):
            shutil.copyfile(pro, os.path.join(stage, stem + ".kicad_pro"))
        with open(os.path.join(stage, stem + ".kicad_dru"), "w", encoding="utf-8") as fh:
            fh.write(dru_text)
        src_dir = os.path.dirname(pro or board)
        prj_mod = src_dir.replace("\\", "/")
        for table in ("fp-lib-table", "sym-lib-table"):
            src = os.path.join(src_dir, table)
            if os.path.isfile(src):
                with open(src, "r", encoding="utf-8") as fh:
                    t = fh.read().replace("${KIPRJMOD}", prj_mod).replace("$(KIPRJMOD)", prj_mod)
                with open(os.path.join(stage, table), "w", encoding="utf-8") as fh:
                    fh.write(t)
        return stage, staged_board

    def _measure(
        self,
        params: Dict[str, Any],
        board: Optional[str],
        pro: Optional[str],
        old_text: str,
        new_text: str,
        new_rules: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from commands.design_rules import DesignRuleCommands

        if not board or not os.path.isfile(board):
            return {"error": "the board has not been saved to a file"}
        cli = resolve_kicad_cli()
        if not cli:
            return {"error": kicad_cli_not_found_message()}
        try:
            timeout = max(10, min(int(params.get("timeoutSec", 300)), 1800))
        except (TypeError, ValueError):
            timeout = 300

        canary_net = self._canary_net()
        canary = (
            f'(rule "{CANARY_NAME}" (condition "A.Type == \'Pad\' && A.NetName == '
            f'\'{canary_net}\'") (constraint assertion "{_NEVER}"))'
            if canary_net
            else ""
        )
        probes, match_info = self._probe_rules(new_rules)
        before_text = append_rule_text(old_text, canary) if canary else old_text
        after_text = new_text
        for extra in (canary, probes):
            if extra:
                after_text = append_rule_text(after_text, extra)

        refill = bool(params.get("refillZones", True))
        flags = DesignRuleCommands._drc_flags(
            {"refillZones": refill},
            DesignRuleCommands._cli_supports_refill(cli) if refill else False,
        )
        stages: List[str] = []
        runs: Dict[str, Any] = {}
        try:
            procs = {}
            for label, text in (("before", before_text), ("after", after_text)):
                stage, staged_board = self._stage(board, pro, text)
                stages.append(stage)
                out_json = os.path.join(stage, "drc.json")
                cmd = [cli, "pcb", "drc", "--format", "json", "--output", out_json]
                cmd += ["--units", "mm", *flags, staged_board]
                procs[label] = (
                    subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
                    ),
                    out_json,
                )
            for label, (proc, out_json) in procs.items():
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    for p, _ in procs.values():
                        p.kill()
                    return {"error": f"kicad-cli DRC timed out after {timeout}s"}
                if proc.returncode != 0 or not os.path.isfile(out_json):
                    return {"error": (stderr or stdout or "kicad-cli failed").strip()[-500:]}
                with open(out_json, "r", encoding="utf-8") as fh:
                    runs[label] = json.load(fh)
        finally:
            for stage in stages:
                shutil.rmtree(stage, ignore_errors=True)

        def split(data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
            real, special = [], {}
            for category in ("violations", "unconnected_items", "schematic_parity"):
                for raw in data.get(category, []) or []:
                    v = check_summary.normalize_violation(raw, category)
                    m = re.search(r"rule '((?:__mcp_)[^']*)'", v["message"])
                    if v["type"] == "assertion_failure" and m:
                        special[m.group(1)] = special.get(m.group(1), 0) + 1
                        continue
                    real.append(v)
            return real, special

        before, before_special = split(runs["before"])
        after, after_special = split(runs["after"])
        accepted: Optional[bool] = None
        notes = []
        if canary:
            accepted = after_special.get(CANARY_NAME, 0) > 0
            if not before_special.get(CANARY_NAME):
                notes.append(
                    "The rules file was already being rejected by KiCad before this change "
                    "(the canary rule did not fire), so the 'before' run used no custom rules."
                )
        else:
            notes.append("No pad with a net to anchor the canary rule; acceptance not verified.")

        before_keys = {v["key"] for v in before}
        after_keys = {v["key"] for v in after}
        new_v = [v for v in after if v["key"] not in before_keys]
        resolved = [v for v in before if v["key"] not in after_keys]
        try:
            limit = max(0, int(params.get("limit", 10)))
        except (TypeError, ValueError):
            limit = 10
        citing = {}
        for r in new_rules:
            pattern = f"rule '{r['name']}'"
            citing[r["name"]] = sum(1 for v in after if pattern in v["message"])

        matched = {}
        for name, info in match_info.items():
            if "probe" in info:
                count = after_special.get(info["probe"], 0)
                matched[name] = {
                    "count": count,
                    "capped": count >= KICAD_REPORT_CAP,
                    "method": "assertion probe (pads, tracks, vias, zones, ...)",
                }
                if count >= KICAD_REPORT_CAP:
                    matched[name]["note"] = (
                        f"kicad-cli reports at most {KICAD_REPORT_CAP} items per check; "
                        "the condition matches at least this many"
                    )
            else:
                matched[name] = info
        for r in new_rules:
            m = matched.get(r["name"], {})
            if m.get("count") == 0 and not citing.get(r["name"]):
                notes.append(
                    f"Rule '{r['name']}' matched no items: check the condition's net/property "
                    "names (hierarchical nets look like '/Sheet/NET')."
                )

        return {
            "kicadAccepted": accepted,
            "matched": matched,
            "drc": {
                "before": len(before),
                "after": len(after),
                "citingRule": citing,
                "newCount": len(new_v),
                "resolvedCount": len(resolved),
                "new": [check_summary.compact(v) for v in new_v[:limit]],
                "resolved": [check_summary.compact(v) for v in resolved[:limit]],
                "zonesRefilled": "--refill-zones" in flags,
            },
            "notes": notes,
        }
