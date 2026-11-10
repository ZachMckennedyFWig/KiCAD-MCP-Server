"""apply_property_map (MCP_ISSUES S4): rules over a hierarchy, idempotent, atomic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bom_fixtures import make_project  # noqa: E402
from commands.bom_tools import apply_property_map, collect_parts  # noqa: E402

pytestmark = pytest.mark.unit

RULES = [
    {"match": {"value": "10k", "footprint_contains": "r_0603"}, "set": {"LCSC": "C25804"}},
    {"match": {"value": "100nF", "footprint_contains": "C_0402"}, "set": {"LCSC": "C307331"}},
    {"match": {"valueRegex": "100n.?", "footprint_contains": "0603"}, "set": {"LCSC": "C14663"}},
    {"match": {"ref_regex": "J\\d+"}, "set": {"dnp": True, "in_bom": False, "MPN": "B2B-XH"}},
    {"match": {"lib_id": "Device:L"}, "set": {"LCSC": "C1"}},
]


def test_dry_run_reports_without_writing(tmp_path):
    root = make_project(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")}
    r = apply_property_map({"schematicPath": str(root), "rules": RULES, "dryRun": True})
    assert r["success"], r
    assert r["dryRun"] is True
    assert r["changedParts"] == 5  # R1, R2, C1, C2 (existing LCSC Part # updated), J1
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")} == before
    hits = {x["index"]: x["matched"] for x in r["rules"]}
    assert hits == {0: 2, 1: 1, 2: 1, 3: 1, 4: 0}
    assert r["rules"][4]["warning"] == "matched no part"


def test_apply_is_idempotent_and_hierarchy_wide(tmp_path):
    root = make_project(tmp_path)
    r = apply_property_map({"schematicPath": str(root), "rules": RULES})
    assert r["success"], r
    assert len(r["sheetsChanged"]) == 2
    parts = {p["ref"]: p for p in collect_parts(root)}
    assert parts["R1"]["lcsc"] == "C25804"
    assert parts["R2"]["lcsc"] == "C25804"
    assert parts["C1"]["lcsc"] == "C307331"
    # An existing LCSC-like field is updated in place, not duplicated.
    assert parts["C2"]["lcscField"] == "LCSC Part #"
    assert parts["C2"]["lcsc"] == "C14663"
    assert "LCSC" not in parts["C2"]["fields"]
    assert parts["J1"]["dnp"] is True and parts["J1"]["in_bom"] is False
    assert parts["J1"]["fields"]["MPN"] == "B2B-XH"
    assert "#PWR01" not in parts
    assert r["missingLcscCount"] == 0

    snapshot = {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")}
    again = apply_property_map({"schematicPath": str(root), "rules": RULES})
    assert again["success"] and again["changedParts"] == 0
    assert again["sheetsChanged"] == []
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")} == snapshot


def test_new_property_copies_block_indentation(tmp_path):
    root = make_project(tmp_path)
    apply_property_map({"schematicPath": str(root), "rules": RULES[:1]})
    text = (tmp_path / "power.kicad_sch").read_text(encoding="utf-8")
    assert '\n\t\t(property "LCSC" "C25804"\n\t\t\t(at 10 50 0)\n\t\t\t(hide yes)\n' in text
    assert text.count("(kicad_sch") == 1


def test_unmatched_and_missing_lcsc_reports(tmp_path):
    root = make_project(tmp_path)
    r = apply_property_map({"schematicPath": str(root), "rules": RULES[:1], "dryRun": True})
    unmatched = {u["ref"] for u in r["unmatched"]}
    assert unmatched == {"C1", "C2", "J1"}
    missing = {m["ref"] for m in r["missingLcsc"]}
    # C2 already has an LCSC Part #; J1 is in the BOM with none.
    assert missing == {"C1", "J1"}


def test_first_match_only(tmp_path):
    root = make_project(tmp_path)
    rules = [
        {"match": {"value": "10k"}, "set": {"LCSC": "C1"}},
        {"match": {"value": "10k"}, "set": {"LCSC": "C2"}},
    ]
    r = apply_property_map({"schematicPath": str(root), "rules": rules, "firstMatchOnly": True})
    assert r["success"]
    assert {p["ref"]: p["lcsc"] for p in collect_parts(root)}["R1"] == "C1"
    r = apply_property_map({"schematicPath": str(root), "rules": rules})
    assert {p["ref"]: p["lcsc"] for p in collect_parts(root)}["R1"] == "C2"


@pytest.mark.parametrize(
    "rules, fragment",
    [
        ([{"match": {}, "set": {"LCSC": "C1"}}], "match must be"),
        ([{"match": {"value": "1"}, "set": {}}], "set must be"),
        ([{"match": {"bogus": "1"}, "set": {"LCSC": "C1"}}], "unknown key"),
        ([{"match": {"ref_regex": "("}, "set": {"LCSC": "C1"}}], "bad regex"),
        ([{"match": {"value": "1"}, "set": {"dnp": "yes"}}], "true or false"),
        ([{"match": {"value": "1"}, "set": {"Reference": "X"}}], "Reference"),
    ],
)
def test_rule_validation(tmp_path, rules, fragment):
    root = make_project(tmp_path)
    r = apply_property_map({"schematicPath": str(root), "rules": rules})
    assert not r["success"]
    assert fragment in r["message"]


def test_accepts_project_file_and_utf8(tmp_path):
    root = make_project(tmp_path)
    rules = [{"match": {"value": "PWR"}, "set": {"Manufacturer": "XKB Connection(中国星坤)"}}]
    r = apply_property_map({"schematicPath": str(tmp_path / "proj.kicad_pro"), "rules": rules})
    assert r["success"], r
    parts = {p["ref"]: p for p in collect_parts(root)}
    assert parts["J1"]["fields"]["Manufacturer"] == "XKB Connection(中国星坤)"


def test_failed_write_restores_every_sheet(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")}
    import commands.bom_tools as bt

    calls = {"n": 0}
    real = bt.write_text_atomic

    def flaky(path, text, newline):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(path, text, newline)

    monkeypatch.setattr(bt, "write_text_atomic", flaky)
    r = apply_property_map({"schematicPath": str(root), "rules": RULES})
    assert not r["success"]
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.kicad_sch")} == before
