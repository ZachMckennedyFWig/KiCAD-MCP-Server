"""Regression tests for schematic connection/label/wire behaviour.

Covers the user report items:

* #5f  batch_connect(replace=true) removes every label kind on the pin.
* #5g  delete_schematic_component trims wires left dangling at deleted pins.
* #6   the PWR_FLAG warning only names nets with a power_in pin and no
       power_out pin, evaluated over the whole hierarchy.
* #7   facing-label auto-wiring is orthogonal (never diagonal); labels on
       two-pin parts move off Reference/Value text.
* S2   assert_connectivity against kicad-cli's netlist.
* S3   relabel_net across the hierarchy.

Fixtures are hand-built schematics with embedded lib_symbols (see
_connectivity_fixtures.py). Tests that need kicad-cli skip when it is absent.
"""

import json
import sys
import types
from pathlib import Path

import pytest
import sexpdata

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))
sys.path.insert(0, str(Path(__file__).parent))

import _connectivity_fixtures as fx  # noqa: E402
from commands.schematic_batch import (  # noqa: E402
    SchematicBatchCommands,
    _orthogonal_route,
    _route_conflict,
    _sheet_geometry,
)
from commands.schematic_connectivity import (  # noqa: E402
    HierarchyNets,
    assert_connectivity,
    find_root_schematic,
    net_names_match,
    parse_netlist_xml,
    power_driver_warnings,
    relabel_net,
)
from utils.kicad_cli import resolve_kicad_cli  # noqa: E402

needs_cli = pytest.mark.skipif(resolve_kicad_cli() is None, reason="kicad-cli not installed")


def _items(path: Path, tag: str):
    data = sexpdata.loads(path.read_text(encoding="utf-8"))
    return [i for i in data if isinstance(i, list) and i and str(i[0]) == tag]


def _wires(path: Path):
    out = []
    for w in _items(path, "wire"):
        pts = next(p for p in w[1:] if isinstance(p, list) and str(p[0]) == "pts")
        a, b = [(float(xy[1]), float(xy[2])) for xy in pts[1:]]
        out.append((a, b))
    return out


def _labels(path: Path):
    out = []
    for kind in ("label", "global_label", "hierarchical_label"):
        for item in _items(path, kind):
            at = next(p for p in item[1:] if isinstance(p, list) and str(p[0]) == "at")
            out.append((kind, str(item[1]), float(at[1]), float(at[2])))
    return out


# ──────────────────────────────────────────────────────────────────────────
#  #7 — orthogonal facing-label routes
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestOrthogonalRoute:
    def test_aligned_is_one_straight_segment(self):
        assert _orthogonal_route([10, 20], 0, [20.16, 20]) == [[10, 20], [20.16, 20]]

    def test_offset_horizontal_pins_get_a_z_route(self):
        route = _orthogonal_route([55.88, 50.8], 0, [66.04, 53.34])
        assert route == [[55.88, 50.8], [60.96, 50.8], [60.96, 53.34], [66.04, 53.34]]
        for (x1, y1), (x2, y2) in zip(route, route[1:]):
            assert x1 == x2 or y1 == y2, "diagonal segment"

    def test_offset_vertical_pins_jog_horizontally(self):
        route = _orthogonal_route([10, 10], 270, [12.54, 20.16])  # pin points down
        assert route[0] == [10, 10] and route[-1] == [12.54, 20.16]
        assert route[1][0] == 10 and route[2][1] == route[1][1]
        for (x1, y1), (x2, y2) in zip(route, route[1:]):
            assert x1 == x2 or y1 == y2

    def test_label_behind_the_pin_is_not_wired(self):
        assert _orthogonal_route([10, 10], 0, [5, 12.54]) is None

    def test_no_room_for_a_jog_falls_back(self):
        # 1.27 mm apart along the pin: the grid-snapped jog cannot fit between.
        assert _orthogonal_route([10.16, 10.16], 0, [11.43, 12.7]) is None

    def test_route_crossing_a_symbol_body_is_rejected(self, tmp_path):
        f = tmp_path / "s.kicad_sch"
        # R1 body hull spans x 99..101, y 46.19..53.81: a horizontal run at y=50 crosses it.
        f.write_text(fx.schematic(fx.symbol("Device:R", "R1", "1k", 100, 50)), encoding="utf-8")
        geom = _sheet_geometry(f)
        assert _route_conflict(geom, [[90, 50], [110, 50]]) == "segment crosses a symbol body"
        assert _route_conflict(geom, [[90, 60], [110, 60]]) is None


# ──────────────────────────────────────────────────────────────────────────
#  #6 — power driver warnings
# ──────────────────────────────────────────────────────────────────────────
def _power_project(tmp_path: Path) -> Path:
    root_body = (
        # U1: VDD on VCC_A (only power_in) -> undriven; EN on DRV_PWR_EN (signal).
        fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
        + fx.label("VCC_A", 45.72, 50.8)
        + fx.label("DRV_PWR_EN", 55.88, 50.8)
        # U2: VDD on +3V3 power symbol; the PWR_FLAG for +3V3 lives on the sub-sheet.
        + fx.symbol("test:IC", "U2", "IC", 50.8, 76.2)
        + fx.symbol("power:+3V3", "#PWR01", "+3V3", 45.72, 76.2)
        + fx.label("SIG", 55.88, 76.2)
        # U3 regulator: VIN undriven; VOUT (power_out) drives U4's VDD on VREG.
        + fx.symbol("test:REG", "U3", "REG", 101.6, 50.8)
        + fx.label("VIN_RAW", 96.52, 50.8)
        + fx.label("VREG", 106.68, 50.8)
        + fx.symbol("test:IC", "U4", "IC", 101.6, 76.2)
        + fx.label("VREG", 96.52, 76.2)
        + fx.label("SIG", 106.68, 76.2)
        # U7: VDD on PWR_LOCAL, flagged by a PWR_FLAG reached through a bent wire.
        + fx.symbol("test:IC", "U7", "IC", 50.8, 127)
        + fx.label("PWR_LOCAL", 45.72, 127)
        + fx.wire(45.72, 127, 35.56, 127)
        + fx.wire(35.56, 127, 35.56, 137.16)
        + fx.symbol("power:PWR_FLAG", "#FLG02", "PWR_FLAG", 35.56, 137.16)
        + fx.sheet("Sub", "sub.kicad_sch", 127, 100)
    )
    sub_body = fx.symbol("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 30.48, 30.48) + fx.label(
        "+3V3", 30.48, 30.48, kind="global_label"
    )
    return fx.write_project(tmp_path, "proj", root_body, {"sub.kicad_sch": sub_body})


def _row(net, x, y):
    return {"net": net, "position": {"x": x, "y": y}}


@pytest.mark.unit
class TestPowerDriverWarnings:
    def test_only_undriven_power_in_nets_are_reported(self, tmp_path):
        root = _power_project(tmp_path)
        placed = [
            _row("VCC_A", 45.72, 50.8),
            _row("DRV_PWR_EN", 55.88, 50.8),  # signal net with a "power-ish" name
            _row("+3V3", 45.72, 76.2),  # PWR_FLAG on another sheet
            _row("VIN_RAW", 96.52, 50.8),
            _row("VREG", 96.52, 76.2),  # driven by the regulator's power_out
            _row("PWR_LOCAL", 45.72, 127),  # flag reached through a bent wire
            _row("SIG", 55.88, 76.2),
        ]
        warnings = power_driver_warnings(root, placed)
        assert len(warnings) == 1
        text = warnings[0]
        assert text.startswith("Power nets without PWR_FLAG")
        assert "VCC_A" in text and "VIN_RAW" in text
        for quiet in ("DRV_PWR_EN", "+3V3", "VREG", "PWR_LOCAL", "SIG"):
            assert quiet not in text

    def test_evaluated_for_a_sub_sheet_against_the_whole_hierarchy(self, tmp_path):
        root = _power_project(tmp_path)
        sub = tmp_path / "sub.kicad_sch"
        assert find_root_schematic(sub) == root.resolve()
        # The flag's own pin is on the +3V3 net, which is driven: no warning.
        assert power_driver_warnings(sub, [_row("+3V3", 30.48, 30.48)]) == []

    def test_no_placed_rows_no_warning(self, tmp_path):
        assert power_driver_warnings(_power_project(tmp_path), []) == []

    def test_hierarchical_label_joins_parent_net(self, tmp_path):
        root_body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.wire(45.72, 50.8, 40.64, 50.8)
            + fx.sheet("Sub", "sub.kicad_sch", 20.32, 45.72, pins=[("RAIL", 40.64, 50.8)])
        )
        sub_body = fx.label("RAIL", 25.4, 25.4, kind="hierarchical_label") + fx.symbol(
            "power:PWR_FLAG", "#FLG01", "PWR_FLAG", 25.4, 25.4
        )
        root = fx.write_project(tmp_path, "h", root_body, {"sub.kicad_sch": sub_body})
        nets = HierarchyNets(root)
        rail = [n for n in nets.nets.values() if "RAIL" in n["names"]]
        assert len(rail) == 1
        assert {p["type"] for p in rail[0]["pins"]} == {"power_in", "power_out"}
        assert power_driver_warnings(root, [_row("RAIL", 45.72, 50.8)]) == []

    def test_batch_connect_signal_net_has_no_warning(self, tmp_path):
        root = fx.write_project(tmp_path, "p", fx.symbol("test:IC", "U1", "IC", 50.8, 50.8))
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "DRV_PWR_EN"}}}
        )
        assert r["success"] is True
        assert "warnings" not in r
        r = c.batch_connect({"schematicPath": str(root), "connections": {"U1": {"1": "VMAIN"}}})
        assert any("VMAIN" in w for w in r.get("warnings", []))


# ──────────────────────────────────────────────────────────────────────────
#  #5f / #7 — batch_connect with the real pin locator
# ──────────────────────────────────────────────────────────────────────────
@pytest.mark.unit
class TestBatchConnectLabels:
    def test_replace_removes_global_and_hierarchical_labels(self, tmp_path):
        body = (
            fx.symbol("test:IC", "U6", "IC", 50.8, 101.6)
            + fx.label("OLD_GLOBAL", 45.72, 101.6, kind="global_label", angle=180)
            + fx.label("OLD_HIER", 45.72, 101.6, kind="hierarchical_label", angle=180)
            + fx.label("OLD_LOCAL", 45.72, 101.6)
            + fx.label("ELSEWHERE", 10.16, 10.16, kind="global_label")
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {
                "schematicPath": str(root),
                "connections": {"U6": {"1": "NEW_LOCAL"}},
                "replace": True,
            }
        )
        assert r["success"] is True
        assert {x["type"] for x in r["placed"][0]["replaced"]} == {
            "label",
            "global_label",
            "hierarchical_label",
        }
        names = {(k, n) for k, n, *_ in _labels(root)}
        assert names == {("label", "NEW_LOCAL"), ("global_label", "ELSEWHERE")}

    def test_replace_removes_label_at_end_of_bare_stub(self, tmp_path):
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.wire(45.72, 50.8, 40.64, 50.8)
            + fx.label("OLD", 40.64, 50.8, kind="global_label", angle=180)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"1": "NEW"}}, "replace": True}
        )
        assert r["success"] is True
        assert [n for _k, n, *_ in _labels(root)] == ["NEW"]
        assert _wires(root) == []

    def test_without_replace_existing_labels_stay(self, tmp_path):
        body = fx.symbol("test:IC", "U6", "IC", 50.8, 101.6) + fx.label(
            "OLD_GLOBAL", 45.72, 101.6, kind="global_label", angle=180
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        c.batch_connect({"schematicPath": str(root), "connections": {"U6": {"1": "NEW"}}})
        assert {n for _k, n, *_ in _labels(root)} == {"OLD_GLOBAL", "NEW"}

    def test_facing_label_is_wired_orthogonally(self, tmp_path):
        body = fx.symbol("test:IC", "U1", "IC", 50.8, 50.8) + fx.symbol(
            "test:IC", "U5", "IC", 71.12, 53.34
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {
                "schematicPath": str(root),
                "connections": {"U5": {"1": "NETX"}, "U1": {"2": "NETX"}},
            }
        )
        assert r["success"] is True
        wired = r["placed"][1]
        assert "wired to existing label" in wired["note"]
        wires = _wires(root)
        assert len(wires) == 3
        for (x1, y1), (x2, y2) in wires:
            assert abs(x1 - x2) < 1e-9 or abs(y1 - y2) < 1e-9, "diagonal wire drawn"
        # Only one label: the wire joins U1/2 to U5's label.
        assert [n for _k, n, *_ in _labels(root)] == ["NETX"]
        nets = HierarchyNets(root)
        netx = [n for n in nets.nets.values() if "NETX" in n["names"]][0]
        assert {(p["ref"], p["pin"]) for p in netx["pins"]} == {("U1", "2"), ("U5", "1")}

    def test_blocked_route_falls_back_to_a_label(self, tmp_path):
        # R9 sits right where the Z route's vertical jog would run.
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.symbol("test:IC", "U5", "IC", 71.12, 55.88)
            + fx.symbol("Device:R", "R9", "1k", 60.96, 53.34, hide_fields=True)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {
                "schematicPath": str(root),
                "connections": {"U5": {"1": "NETX"}, "U1": {"2": "NETX"}},
            }
        )
        assert r["success"] is True
        assert "auto-wire skipped" in r["placed"][1]["note"]
        assert _wires(root) == []
        assert [n for _k, n, *_ in _labels(root)] == ["NETX", "NETX"]

    def test_label_moves_off_a_reference_field_on_a_passive(self, tmp_path):
        # R1's Reference text sits just above its top pin, where the label would go.
        body = fx.symbol(
            "Device:R", "R1", "10k", 127, 50.8, ref_at=(127, 45.72), val_at=(129.54, 50.8)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"R1": {"1": "A_LONG_NAME", "2": "B"}}}
        )
        assert r["success"] is True
        top, bottom = r["placed"]
        assert top["label_position"] == {"x": 127.0, "y": 44.45}
        assert "label_position" not in bottom  # nothing in the way below
        assert ((127.0, 46.99), (127.0, 44.45)) in [
            (tuple(round(v, 2) for v in a), tuple(round(v, 2) for v in b)) for a, b in _wires(root)
        ]
        # Still connected: the stub joins the pin to the moved label.
        nets = HierarchyNets(root)
        net = [n for n in nets.nets.values() if "A_LONG_NAME" in n["names"]][0]
        assert {(p["ref"], p["pin"]) for p in net["pins"]} == {("R1", "1")}

    def test_avoid_field_overlap_can_be_disabled(self, tmp_path):
        body = fx.symbol(
            "Device:R", "R1", "10k", 127, 50.8, ref_at=(127, 45.72), val_at=(129.54, 50.8)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {
                "schematicPath": str(root),
                "connections": {"R1": {"1": "A_LONG_NAME"}},
                "avoidFieldOverlap": False,
            }
        )
        assert "label_position" not in r["placed"][0]
        assert _wires(root) == []


# ──────────────────────────────────────────────────────────────────────────
#  #5g — dangling wires after delete_schematic_component
# ──────────────────────────────────────────────────────────────────────────
def _delete(params):
    from kicad_interface import KiCADInterface

    iface = KiCADInterface.__new__(KiCADInterface)
    return iface._handle_delete_schematic_component(params)


@pytest.mark.unit
class TestDeleteAttachedWires:
    def test_pwr_flag_auto_wire_is_removed(self, tmp_path):
        # PWR_FLAG at (35.56, 60.96) joined by an auto-drawn L wire to U1's
        # labelled VDD pin (45.72, 50.8).
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.label("VCC", 45.72, 50.8, angle=180)
            + fx.symbol("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 35.56, 60.96)
            + fx.wire(35.56, 60.96, 35.56, 50.8)
            + fx.wire(35.56, 50.8, 45.72, 50.8)
        )
        root = fx.write_project(tmp_path, "p", body)
        r = _delete(
            {"schematicPath": str(root), "reference": "#FLG01", "deleteAttachedLabels": True}
        )
        assert r["success"] is True
        assert r["deleted_wire_count"] == 2
        assert _wires(root) == []
        assert [n for _k, n, *_ in _labels(root)] == ["VCC"]  # still on U1's pin

    def test_trim_stops_at_a_junction(self, tmp_path):
        # Flag stub joins a through wire between U1/1 and a label: only the stub goes.
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.wire(45.72, 50.8, 30.48, 50.8)
            + fx.label("VCC", 30.48, 50.8, angle=180)
            + fx.wire(38.1, 50.8, 38.1, 60.96)
            + fx.symbol("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 38.1, 60.96)
        )
        root = fx.write_project(tmp_path, "p", body)
        r = _delete(
            {"schematicPath": str(root), "reference": "#FLG01", "deleteAttachedWires": True}
        )
        assert r["deleted_wire_count"] == 1
        remaining = _wires(root)
        assert remaining == [((45.72, 50.8), (30.48, 50.8))]
        assert _items(root, "junction") == []  # the T is gone, so is its dot

    def test_stub_label_at_far_end_is_removed_with_labels_flag(self, tmp_path):
        body = (
            fx.symbol("Device:R", "R1", "1k", 50.8, 50.8)
            + fx.wire(50.8, 46.99, 50.8, 41.91)
            + fx.label("TOP", 50.8, 41.91, angle=90)
            + fx.label("KEEP", 10.16, 10.16)
        )
        root = fx.write_project(tmp_path, "p", body)
        r = _delete({"schematicPath": str(root), "reference": "R1", "deleteAttachedLabels": True})
        assert r["deleted_wire_count"] == 1
        assert [d["text"] for d in r["deleted_labels"]] == ["TOP"]
        assert [n for _k, n, *_ in _labels(root)] == ["KEEP"]

    def test_wire_between_two_remaining_wires_is_kept(self, tmp_path):
        # Two wires meet at R1's pin: removing R1 leaves them still joined.
        body = (
            fx.symbol("Device:R", "R1", "1k", 50.8, 50.8)
            + fx.symbol("test:IC", "U1", "IC", 76.2, 46.99)
            + fx.wire(50.8, 46.99, 71.12, 46.99)
            + fx.wire(50.8, 46.99, 50.8, 30.48)
            + fx.label("X", 50.8, 30.48, angle=90)
        )
        root = fx.write_project(tmp_path, "p", body)
        r = _delete({"schematicPath": str(root), "reference": "R1", "deleteAttachedLabels": True})
        assert r["deleted_wire_count"] == 0
        assert len(_wires(root)) == 2

    def test_default_keeps_wires(self, tmp_path):
        body = fx.symbol("power:PWR_FLAG", "#FLG01", "PWR_FLAG", 35.56, 60.96) + fx.wire(
            35.56, 60.96, 35.56, 50.8
        )
        root = fx.write_project(tmp_path, "p", body)
        r = _delete({"schematicPath": str(root), "reference": "#FLG01"})
        assert r["success"] is True
        assert "deleted_wires" not in r
        assert len(_wires(root)) == 1


# ──────────────────────────────────────────────────────────────────────────
#  S2 — assert_connectivity
# ──────────────────────────────────────────────────────────────────────────
_NETLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="U1"><value>IC</value><libsource lib="test" part="IC"/></comp>
  </components>
  <nets>
    <net code="1" name="/Power/+5V"><node ref="U1" pin="1" pinfunction="VDD" pintype="power_in"/></net>
  </nets>
</export>"""


@pytest.mark.unit
class TestNetNameMatching:
    def test_last_component_or_full(self):
        assert net_names_match("+5V", "/Power/+5V")
        assert net_names_match("/Power/+5V", "/Power/+5V")
        assert net_names_match("/Other/+5V", "/Power/+5V")
        assert not net_names_match("+3V3", "/Power/+5V")

    def test_parse_netlist(self):
        nl = parse_netlist_xml(_NETLIST_XML)
        assert nl["components"]["U1"]["lib"] == "test:IC"
        assert nl["nets"]["/Power/+5V"][0]["pintype"] == "power_in"


def _assert_project(tmp_path: Path, lonely: bool = True) -> Path:
    body = (
        fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
        + fx.symbol("test:IC", "U5", "IC", 71.12, 50.8)
        + fx.label("NETX", 55.88, 50.8)
        + fx.label("NETX", 66.04, 50.8, angle=180)
        + (fx.label("LONELY", 45.72, 50.8, angle=180) if lonely else "")
        + fx.symbol("test:IC", "U6", "IC", 50.8, 76.2)
        + fx.sheet("Power", "power.kicad_sch", 127, 100, pins=[])
    )
    sub = fx.symbol("test:IC", "U9", "IC", 50.8, 50.8) + fx.label("+5V", 45.72, 50.8, angle=180)
    sub += fx.label("+5V", 30.48, 30.48)
    sub += fx.symbol("Device:R", "R1", "1k", 30.48, 34.29)
    return fx.write_project(tmp_path, "proj", body, {"power.kicad_sch": sub})


@needs_cli
class TestAssertConnectivity:
    def test_reports_only_problems(self, tmp_path):
        root = _assert_project(tmp_path)
        r = assert_connectivity(
            {
                "schematicPath": str(root),
                "expectations": {
                    "U1": {"2": "NETX", "1": "WRONG"},
                    "U5": {"1": "NETX", "2": "NC"},
                    "U6": {"1": "VDD_RAIL"},
                    "U9": {"VDD": "+5V"},  # by pin name, hierarchical net
                    "U99": {"1": "X"},
                },
            }
        )
        assert r["success"] is True and r["passed"] is False
        assert r["checkedPins"] == 6
        mism = {(m["ref"], m["pin"]): m for m in r["mismatches"]}
        assert set(mism) == {("U1", "1"), ("U6", "1")}
        assert mism[("U1", "1")]["actual"].endswith("LONELY")
        assert mism[("U6", "1")]["actual"] == "unconnected"
        assert r["missing"] == [{"ref": "U99", "reason": "component not in netlist"}]
        assert [(s["ref"], s["pin"]) for s in r["singlePinNets"]] == [("U1", "1")]

    def test_pass_and_stored_set(self, tmp_path):
        root = _assert_project(tmp_path, lonely=False)
        good = {"U1": {"2": "NETX"}, "U5": {"1": "/NETX"}, "U9": {"1": "/Power/+5V"}}
        r = assert_connectivity({"schematicPath": str(root), "expectations": good, "saveAs": "s"})
        assert r["passed"] is True, r
        store = tmp_path / ".kicad-mcp" / "connectivity.json"
        assert json.loads(store.read_text(encoding="utf-8"))["sets"]["s"]["U1"] == {"2": "NETX"}
        r2 = assert_connectivity(
            {"projectPath": str(tmp_path / "proj.kicad_pro"), "useStored": "s"}
        )
        assert r2["passed"] is True and r2["checkedPins"] == 3
        # A sub-sheet path still runs on the root schematic.
        r3 = assert_connectivity(
            {"schematicPath": str(tmp_path / "power.kicad_sch"), "useStored": "s"}
        )
        assert Path(r3["rootSchematic"]) == root.resolve()

    def test_missing_stored_set(self, tmp_path):
        root = _assert_project(tmp_path)
        r = assert_connectivity({"schematicPath": str(root), "useStored": "nope"})
        assert r["success"] is False


@pytest.mark.unit
def test_assert_connectivity_requires_expectations(tmp_path):
    root = _assert_project(tmp_path)
    assert assert_connectivity({"schematicPath": str(root)})["success"] is False
    assert assert_connectivity({})["success"] is False


# ──────────────────────────────────────────────────────────────────────────
#  S3 — relabel_net
# ──────────────────────────────────────────────────────────────────────────
def _relabel_project(tmp_path: Path) -> Path:
    root_body = (
        fx.label("VBUS", 10.16, 10.16)
        + fx.label("VBUS", 20.32, 10.16, kind="global_label")
        + fx.label("VBUS_SENSE", 30.48, 10.16)
        + fx.sheet("Sub", "sub.kicad_sch", 50.8, 50.8, pins=[("VBUS", 50.8, 53.34)])
    )
    sub_body = fx.label("VBUS", 25.4, 25.4, kind="hierarchical_label") + fx.label(
        "VBUS", 25.4, 35.56, kind="global_label"
    )
    return fx.write_project(tmp_path, "proj", root_body, {"sub.kicad_sch": sub_body})


@pytest.mark.unit
class TestRelabelNet:
    def test_renames_every_label_kind_and_sheet_pin(self, tmp_path):
        root = _relabel_project(tmp_path)
        r = relabel_net(
            {
                "schematicPath": str(tmp_path / "sub.kicad_sch"),
                "oldName": "/Sub/VBUS",
                "newName": "VBUS_5V",
            }
        )
        assert r["success"] is True, r
        assert r["renamed"] == 5
        assert {n for _k, n, *_ in _labels(root)} == {"VBUS_5V", "VBUS_SENSE"}
        assert {n for _k, n, *_ in _labels(tmp_path / "sub.kicad_sch")} == {"VBUS_5V"}
        assert '(pin "VBUS_5V" bidirectional' in root.read_text(encoding="utf-8")
        nets = HierarchyNets(root)
        assert not any("VBUS" in n["names"] for n in nets.nets.values())

    def test_dry_run_writes_nothing(self, tmp_path):
        root = _relabel_project(tmp_path)
        before = root.read_text(encoding="utf-8")
        r = relabel_net(
            {"schematicPath": str(root), "oldName": "VBUS", "newName": "X", "dryRun": True}
        )
        assert r["success"] is True and r["dryRun"] is True and r["renamed"] == 5
        assert root.read_text(encoding="utf-8") == before

    def test_sheet_only(self, tmp_path):
        root = _relabel_project(tmp_path)
        r = relabel_net(
            {"schematicPath": str(root), "oldName": "VBUS", "newName": "X", "sheetOnly": True}
        )
        assert r["renamed"] == 2
        assert {n for _k, n, *_ in _labels(tmp_path / "sub.kicad_sch")} == {"VBUS"}

    def test_unknown_name_fails(self, tmp_path):
        root = _relabel_project(tmp_path)
        r = relabel_net({"schematicPath": str(root), "oldName": "NOPE", "newName": "X"})
        assert r["success"] is False
