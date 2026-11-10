"""Compact DRC/ERC output: detail levels, filters, deltas, project rules (MCP_ISSUES Q5).

Covers ``utils.check_summary`` directly, ``run_erc``'s delta across two runs,
``run_drc``'s ``detail="full"`` back-compat and project-rule staging
(principle 9: a board copied away from its ``.kicad_pro`` must still be
checked against that project's rules, not KiCad's defaults).
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils import check_summary  # noqa: E402

pytestmark = pytest.mark.unit


def _raw(vtype, sev, desc, items):
    return {
        "type": vtype,
        "severity": sev,
        "description": desc,
        "items": [{"description": d, "uuid": u, "pos": {"x": x, "y": y}} for (d, u, x, y) in items],
    }


RAW = [
    _raw("silk_overlap", "warning", "Silkscreen clearance", [("Segment of C1", "s1", 10, 10)]),
    _raw("silk_edge_clearance", "warning", "Silk clipped", [("Segment of R2", "s2", 20, 20)]),
    _raw(
        "clearance",
        "error",
        "Clearance violation",
        [("PTH pad 1 [/Power/+5V] of U3", "p1", 50, 50), ("Track [GND] on F.Cu", "t1", 51, 50)],
    ),
    _raw(
        "unconnected_items",
        "error",
        "Missing connection",
        [("Pad 2 [VIN] of U10", "p2", 120, 80), ("Pad 1 [VIN] of C12", "p3", 125, 80)],
    ),
]


def _norm(raws=RAW):
    return [check_summary.normalize_violation(r) for r in raws]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_ignore_globs_drop_matching_types():
    kept, info = check_summary.filter_violations(_norm(), {"ignore": ["silk_*"]})
    assert [v["type"] for v in kept] == ["clearance", "unconnected_items"]
    assert info["ignoredCount"] == 2


def test_nets_filter_matches_hierarchical_suffix():
    kept, _ = check_summary.filter_violations(_norm(), {"nets": ["+5V"]})
    assert [v["type"] for v in kept] == ["clearance"]
    kept, _ = check_summary.filter_violations(_norm(), {"nets": "VIN,GND"})
    assert {v["type"] for v in kept} == {"clearance", "unconnected_items"}


def test_refs_filter_uses_word_boundaries():
    kept, _ = check_summary.filter_violations(_norm(), {"refs": ["U1"]})
    assert kept == []  # U1 must not match U10
    kept, _ = check_summary.filter_violations(_norm(), {"refs": ["U10"]})
    assert [v["type"] for v in kept] == ["unconnected_items"]


def test_region_filter_any_item_inside():
    region = {"x1": 115, "y1": 70, "x2": 130, "y2": 90}
    kept, info = check_summary.filter_violations(_norm(), {"region": region})
    assert [v["type"] for v in kept] == ["unconnected_items"]
    assert info["region"] == region


def test_bad_region_and_detail_are_rejected():
    with pytest.raises(ValueError):
        check_summary.parse_region({"x1": 1})
    with pytest.raises(ValueError):
        check_summary.parse_detail_params({"detail": "everything"})


# ---------------------------------------------------------------------------
# Detail levels + delta
# ---------------------------------------------------------------------------


def test_summary_is_counts_plus_first_items_errors_first(tmp_path):
    target = tmp_path / "b.kicad_pcb"
    target.write_text("")
    out = check_summary.build_check_response("drc", str(target), _norm(), {"limit": 2})
    assert out["detail"] == "summary"
    assert out["counts"]["total"] == 4
    assert out["counts"]["by_severity"] == {"error": 2, "warning": 2, "info": 0}
    assert out["itemsShown"] == 2 and out["itemsTruncated"] is True
    assert {i["severity"] for i in out["items"]} == {"error"}
    assert "items" not in out["items"][0]  # summary items are one-liners
    assert out["items"][0]["location"]
    assert out["delta"]["firstRun"] is True


def test_items_detail_lists_everything_and_full_keeps_uuids(tmp_path):
    target = tmp_path / "b.kicad_pcb"
    target.write_text("")
    items = check_summary.build_check_response("drc", str(target), _norm(), {"detail": "items"})
    assert items["itemsShown"] == 4 and not items["itemsTruncated"]
    assert items["items"][0]["items"]  # item descriptions included
    full = check_summary.build_check_response("drc", str(target), _norm(), {"detail": "full"})
    assert full["items"][0]["items"][0]["uuid"]


def test_delta_reports_new_and_resolved_against_previous_run(tmp_path):
    target = tmp_path / "b.kicad_pcb"
    target.write_text("")
    check_summary.build_check_response("drc", str(target), _norm(RAW[:3]), {})
    fixed_one_added_one = _norm([RAW[0], RAW[2], RAW[3]])
    out = check_summary.build_check_response("drc", str(target), fixed_one_added_one, {})
    delta = out["delta"]
    assert delta["firstRun"] is False
    assert delta["newCount"] == 1 and delta["new"][0]["type"] == "unconnected_items"
    assert delta["resolvedCount"] == 1 and delta["resolved"][0]["type"] == "silk_edge_clearance"
    # History lives next to the checked file.
    assert (tmp_path / ".kicad-mcp" / "checks" / "b.kicad_pcb.drc.json").exists()


def test_delta_ignores_position_changes(tmp_path):
    target = tmp_path / "b.kicad_pcb"
    target.write_text("")
    check_summary.build_check_response("drc", str(target), _norm(RAW), {})
    moved = json.loads(json.dumps(RAW))
    for r in moved:
        for it in r["items"]:
            it["pos"]["x"] += 5
    out = check_summary.build_check_response("drc", str(target), _norm(moved), {})
    assert out["delta"]["newCount"] == 0 and out["delta"]["resolvedCount"] == 0


def test_filtered_call_does_not_corrupt_next_delta(tmp_path):
    """History stores the unfiltered set even when the view is filtered."""
    target = tmp_path / "b.kicad_pcb"
    target.write_text("")
    check_summary.build_check_response("drc", str(target), _norm(), {"ignore": ["*"]})
    last = check_summary.load_last_run("drc", str(target))
    assert last["total"] == 4
    out = check_summary.build_check_response("drc", str(target), _norm(), {})
    assert out["delta"]["newCount"] == 0


# ---------------------------------------------------------------------------
# run_erc integration (subprocess mocked)
# ---------------------------------------------------------------------------


def _erc_run(payload):
    def _side_effect(cmd, **kwargs):
        out = cmd[cmd.index("--output") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return MagicMock(returncode=1, stderr="")

    return _side_effect


def _iface():
    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
    iface.design_rule_commands = MagicMock()
    iface.design_rule_commands._find_kicad_cli.return_value = "kicad-cli"
    return iface


def _erc_payload(*raws):
    return {"coordinate_units": "mm", "sheets": [{"path": "/", "violations": list(raws)}]}


def test_run_erc_summary_default_and_delta(tmp_path):
    sch = tmp_path / "p.kicad_sch"
    sch.write_text("(kicad_sch)")
    iface = _iface()
    pin = _raw("pin_not_connected", "error", "Pin not connected", [("Symbol U7 Pin 5", "u", 1, 2)])
    lbl = _raw("label_dangling", "error", "Label dangling", [("Label 'VOUT'", "l", 3, 4)])
    pf = _raw("lib_symbol_mismatch", "warning", "Symbol mismatch", [("Symbol R1", "r", 5, 6)])

    with patch("subprocess.run", side_effect=_erc_run(_erc_payload(pin, lbl, pf))):
        first = iface._handle_run_erc({"schematicPath": str(sch)})
    assert first["success"] and first["detail"] == "summary"
    assert "violations" not in first  # compact by default
    assert first["summary"]["total"] == 3
    assert first["summary"]["by_type"]["label_dangling"] == 1
    assert first["delta"]["firstRun"] is True

    with patch("subprocess.run", side_effect=_erc_run(_erc_payload(lbl, pf))):
        second = iface._handle_run_erc(
            {"schematicPath": str(sch), "ignore": ["lib_symbol_*"], "nets": ["VOUT"]}
        )
    # The message counts the whole design; the delta lists follow the filters
    # (the resolved U7 pin doesn't mention VOUT, so it is not listed).
    assert "0 new, 1 resolved" in second["message"]
    assert second["delta"]["resolvedCountAll"] == 1
    assert second["delta"]["resolvedCount"] == 0

    with patch("subprocess.run", side_effect=_erc_run(_erc_payload(lbl, pf))):
        third = iface._handle_run_erc({"schematicPath": str(sch), "refs": ["U7"]})
    assert third["delta"]["resolvedCountAll"] == 0  # compared with the second run
    assert second["counts"]["total"] == 1  # filtered view: only the VOUT label
    assert second["summary"]["total"] == 2  # unfiltered legacy summary
    assert second["filters"]["ignoredCount"] == 1


def test_run_erc_full_keeps_legacy_violations(tmp_path):
    sch = tmp_path / "p.kicad_sch"
    sch.write_text("(kicad_sch)")
    iface = _iface()
    pin = _raw("pin_not_connected", "error", "Pin not connected", [("Symbol U7 Pin 5", "u", 1, 2)])
    with patch("subprocess.run", side_effect=_erc_run(_erc_payload(pin))):
        res = iface._handle_run_erc({"schematicPath": str(sch), "detail": "full"})
    assert res["violations"][0]["type"] == "pin_not_connected"
    assert res["violations"][0]["location"] == {"x": 1, "y": 2}


def test_run_erc_rejects_bad_detail(tmp_path):
    sch = tmp_path / "p.kicad_sch"
    sch.write_text("(kicad_sch)")
    res = _iface()._handle_run_erc({"schematicPath": str(sch), "detail": "verbose"})
    assert res["success"] is False and "detail" in res["message"]


# ---------------------------------------------------------------------------
# run_drc: project rules (principle 9) and back-compat
# ---------------------------------------------------------------------------


def _drc_commands(tmp_path, monkeypatch, board_name="b.kicad_pcb", report=None, seen=None):
    import subprocess

    from commands.design_rules import DesignRuleCommands

    report = report or {"violations": [RAW[2]], "unconnected_items": [RAW[3]]}
    board = tmp_path / board_name
    board.write_text("(kicad_pcb)")
    DesignRuleCommands._refill_support = {}
    cmds = DesignRuleCommands.__new__(DesignRuleCommands)
    cmds.board = SimpleNamespace(GetFileName=lambda: str(board))
    monkeypatch.setattr(cmds, "_find_kicad_cli", lambda: "kicad-cli")

    def fake_run(cmd, **kw):
        if "--help" in cmd:
            return SimpleNamespace(returncode=0, stdout="--refill-zones", stderr="")
        checked = cmd[-1]
        if seen is not None:
            folder = os.path.dirname(checked)
            seen.append(
                {
                    "file": checked,
                    "listing": sorted(os.listdir(folder)),
                    "fp_table": (
                        Path(folder, "fp-lib-table").read_text(encoding="utf-8")
                        if Path(folder, "fp-lib-table").exists()
                        else None
                    ),
                }
            )
        Path(cmd[cmd.index("--output") + 1]).write_text(json.dumps(report))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return cmds, board


def test_run_drc_full_detail_keeps_legacy_list(tmp_path, monkeypatch):
    cmds, _ = _drc_commands(tmp_path, monkeypatch)
    res = cmds.run_drc({"detail": "full"})
    assert res["success"], res
    assert len(res["violations"]) == 2 and res["violations"][1]["category"] == "unconnected_items"
    summary = cmds.run_drc({})
    assert "violations" not in summary and summary["counts"]["total"] == 2
    assert summary["summary"]["by_category"]["unconnected_items"] == 1  # legacy field


def test_run_drc_uses_own_project_in_place(tmp_path, monkeypatch):
    seen = []
    cmds, board = _drc_commands(tmp_path, monkeypatch, seen=seen)
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_dru").write_text("(version 1)")
    res = cmds.run_drc({})
    assert res["rules"]["source"] == "project"
    assert res["rules"]["kicadDru"].endswith("b.kicad_dru")
    assert seen[0]["file"] == str(board)


def test_run_drc_stages_copied_board_with_project_rules(tmp_path, monkeypatch):
    seen = []
    cmds, board = _drc_commands(tmp_path, monkeypatch, board_name="copy.kicad_pcb", seen=seen)
    (tmp_path / "proj.kicad_pro").write_text("{}")
    (tmp_path / "proj.kicad_dru").write_text("(version 1)")
    (tmp_path / "fp-lib-table").write_text(
        '(fp_lib_table (lib (name "x")(uri "${KIPRJMOD}/x.pretty")))', encoding="utf-8"
    )
    res = cmds.run_drc({})
    assert res["success"], res
    assert res["rules"]["source"] == "project (staged copy)"
    staged = seen[0]
    assert os.path.basename(staged["file"]) == "proj.kicad_pcb"
    assert {"proj.kicad_pro", "proj.kicad_dru", "fp-lib-table"} <= set(staged["listing"])
    assert "${KIPRJMOD}" not in staged["fp_table"]
    assert str(tmp_path).replace("\\", "/") in staged["fp_table"]
    assert not os.path.exists(os.path.dirname(staged["file"]))  # cleaned up
    # The history and violations file still belong to the real board.
    assert res["violationsFile"].startswith(str(tmp_path))
    assert (tmp_path / ".kicad-mcp" / "checks" / "copy.kicad_pcb.drc.json").exists()


def test_run_drc_without_project_warns_about_defaults(tmp_path, monkeypatch):
    cmds, _ = _drc_commands(tmp_path, monkeypatch)
    res = cmds.run_drc({})
    assert res["rules"]["source"] == "kicad-defaults"
    assert "default design rules" in res["rules"]["warning"]


def test_run_drc_explicit_project_path(tmp_path, monkeypatch):
    seen = []
    cmds, _ = _drc_commands(tmp_path, monkeypatch, seen=seen)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "main.kicad_pro").write_text("{}")
    res = cmds.run_drc({"projectPath": str(other)})
    assert res["rules"]["kicadPro"] == str(other / "main.kicad_pro")
    assert os.path.basename(seen[0]["file"]) == "main.kicad_pcb"


def test_run_drc_rejects_bad_region(tmp_path, monkeypatch):
    cmds, _ = _drc_commands(tmp_path, monkeypatch)
    res = cmds.run_drc({"region": {"x1": 0}})
    assert res["success"] is False and "region" in res["message"]
