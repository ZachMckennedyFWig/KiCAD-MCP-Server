"""fab_package (MCP_ISSUES F3): JLC BOM/CPL/hand-solder logic and a kicad-cli run."""

from __future__ import annotations

import csv
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from commands.fab_package import (  # noqa: E402
    build_cpl,
    copper_layers,
    fab_package,
    parse_netlist_parts,
    plan_assembly,
    rotation_rules,
)

pytestmark = pytest.mark.unit

NETLIST = """<?xml version="1.0" encoding="utf-8"?>
<export version="E">
  <components>
    <comp ref="R1"><value>10k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields><field name="LCSC">C25804</field></fields></comp>
    <comp ref="R2"><value>10k</value><footprint>Resistor_SMD:R_0603_1608Metric</footprint>
      <fields><field name="LCSC Part #">C25804</field></fields></comp>
    <comp ref="U1"><value>LDO</value><footprint>Package_TO_SOT_SMD:SOT-23-5</footprint>
      <fields><field name="LCSC">C485517</field></fields></comp>
    <comp ref="J1"><value>PWR</value><footprint>Conn:JST_XH</footprint>
      <fields><field name="LCSC">C144395</field></fields></comp>
    <comp ref="C9"><value>1uF</value><footprint>Capacitor_SMD:C_0402</footprint>
      <property name="dnp"/></comp>
    <comp ref="H1"><value>Hole</value><footprint>MountingHole:M3</footprint>
      <property name="exclude_from_bom"/></comp>
    <comp ref="C1"><value>1uF</value><footprint>Capacitor_SMD:C_0402</footprint></comp>
  </components>
</export>
"""

POS = (
    "Ref,Val,Package,PosX,PosY,Rot,Side\n"
    '"R1","10k","R_0603_1608Metric",1.000000,-2.000000,90.000000,top\n'
    '"R2","10k","R_0603_1608Metric",3.000000,-2.000000,0.000000,bottom\n'
    '"U1","LDO","SOT-23-5",5.000000,-5.000000,90.000000,top\n'
    '"J1","PWR","JST_XH",9.000000,-9.000000,0.000000,top\n'
    '"U9","x","SOT-23-5",9.000000,-9.000000,0.000000,bottom\n'
)


def test_parse_netlist_and_plan():
    parts = {p["ref"]: p for p in parse_netlist_parts(NETLIST)}
    assert parts["R2"]["lcsc"] == "C25804"
    assert parts["C9"]["dnp"] and not parts["H1"]["in_bom"]
    plan = plan_assembly(list(parts.values()), hand=["J1"], excluded=["U1", "X99"])
    assert plan["groups"] == {("10k", "R_0603_1608Metric", "C25804"): ["R1", "R2"]}
    assert plan["handGroups"] == {("PWR", "JST_XH", "C144395"): ["J1"]}
    assert plan["placed"] == ["R1", "R2"]
    assert [m["ref"] for m in plan["missingLcsc"]] == ["C1"]
    assert plan["dnp"] == ["C9"] and plan["notInBom"] == ["H1"]
    assert plan["unknownRefs"] == ["X99"]


def test_cpl_rotation_table_and_overrides():
    rules = rotation_rules("default")
    rows, corrections, missing = build_cpl(POS, ["R1", "R2", "U1", "X5"], rules, {"R2": 90})
    by_ref = {r[0]: r for r in rows}
    assert by_ref["R1"] == ["R1", "1.000000mm", "-2.000000mm", "Top", "90.000000"]
    # Override on a bottom part is subtracted (mirrored view).
    assert by_ref["R2"][3:] == ["Bottom", "270.000000"]
    # SOT-23 family: +180.
    assert by_ref["U1"][4] == "270.000000"
    assert {c["ref"] for c in corrections} == {"R2", "U1"}
    assert missing == ["X5"]
    rows, corrections, _ = build_cpl(POS, ["U1"], rotation_rules("none"), {})
    assert rows[0][4] == "90.000000" and not corrections
    custom = rotation_rules([{"pattern": "^R_0603", "degrees": 45}])
    rows, _, _ = build_cpl(POS, ["R1"], custom, {})
    assert rows[0][4] == "135.000000"
    with pytest.raises(ValueError):
        rotation_rules("sideways")


def test_copper_layers():
    text = (
        '(kicad_pcb\n\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" power "GND")\n'
        '\t\t(6 "In2.Cu" signal)\n\t\t(2 "B.Cu" signal)\n\t\t(5 "F.SilkS" user)\n\t)\n)'
    )
    assert copper_layers(text) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    assert copper_layers("(kicad_pcb)") == ["F.Cu", "B.Cu"]


def test_argument_errors(tmp_path):
    assert not fab_package({"boardPath": "x.kicad_pcb"})["success"]
    r = fab_package({"fab": "pcbway", "boardPath": "x", "outputDir": str(tmp_path)})
    assert "not supported" in r["message"]
    r = fab_package({"boardPath": str(tmp_path / "none.kicad_pcb"), "outputDir": str(tmp_path)})
    assert not r["success"]


def _demo(tmp_path: Path):
    from utils.kicad_cli import resolve_kicad_cli
    from utils.kicad_roots import kicad_install_roots

    if not resolve_kicad_cli():
        return None
    for root in kicad_install_roots():
        demo = root / "share" / "kicad" / "demos" / "ecc83"
        if (demo / "ecc83-pp.kicad_pcb").is_file():
            proj = tmp_path / "ecc83"
            proj.mkdir()
            for p in demo.iterdir():
                if p.name.startswith("ecc83-pp.") or p.name.endswith("-lib-table"):
                    shutil.copy(p, proj / p.name)
                elif p.is_dir():
                    shutil.copytree(p, proj / p.name)
            return proj
    return None


@pytest.mark.integration
def test_fab_package_on_kicad_demo(tmp_path):
    proj = _demo(tmp_path)
    if proj is None:
        pytest.skip("kicad-cli or KiCad's ecc83 demo not available")
    from commands.bom_tools import apply_property_map, collect_parts
    from test_jlc_sourcing import make_v2_db

    sch = proj / "ecc83-pp.kicad_sch"
    r = apply_property_map(
        {
            "schematicPath": str(sch),
            "rules": [{"match": {"valueRegex": ".*"}, "set": {"LCSC": "C25804"}}],
        }
    )
    assert r["success"], r
    parts = [p for p in collect_parts(sch) if p["in_bom"] and not p["dnp"]]
    hand = parts[0]["ref"]
    db = make_v2_db(tmp_path / "cdfer.sqlite3")
    out = tmp_path / "fab"
    res = fab_package(
        {
            "boardPath": str(proj / "ecc83-pp.kicad_pcb"),
            "outputDir": str(out),
            "options": {"handSolder": [hand], "boards": 2, "dbPath": str(db)},
            "timeout": 300,
        }
    )
    assert res["success"], res
    files = res["files"]
    names = set(zipfile.ZipFile(files["gerberZip"]).namelist())
    assert any(n.endswith(".gtl") for n in names) and any(n.endswith(".drl") for n in names)
    assert any(n.endswith(".gbrjob") for n in names) and any("drl_map" in n for n in names)
    with open(files["bom"], newline="", encoding="utf-8") as fh:
        bom = list(csv.reader(fh))
    assert bom[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]
    designators = [d for row in bom[1:] for d in row[1].split(",")]
    assert hand not in designators
    assert len(designators) == len(parts) - 1
    with open(files["cpl"], newline="", encoding="utf-8") as fh:
        cpl = list(csv.reader(fh))
    assert cpl[0] == ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]
    assert {row[0] for row in cpl[1:]} <= set(designators)
    with open(files["handSolder"], newline="", encoding="utf-8") as fh:
        assert list(csv.reader(fh))[1][1] == hand
    assert res["fees"]["totals"]["basicLines"] == 1
    assert res["checks"]["drc"]["ran"] is True
    assert res["checks"]["erc"]["ran"] is True
    assert "ok" in res["checks"]["parity"]
    assert Path(files["report"]).is_file()
    # A second run replaces the outputs instead of piling up stale files.
    again = fab_package(
        {
            "boardPath": str(proj / "ecc83-pp.kicad_pcb"),
            "outputDir": str(out),
            "options": {"runChecks": False, "dbPath": str(db)},
        }
    )
    assert again["success"]
    assert sorted(again["files"]["gerberFiles"]) == sorted(files["gerberFiles"])
