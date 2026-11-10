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

    @staticmethod
    def _wired_pair(tmp_path):
        """U1/2 wired (by batch_connect's facing-label route) to U5/1's NETX label."""
        body = fx.symbol("test:IC", "U1", "IC", 50.8, 50.8) + fx.symbol(
            "test:IC", "U5", "IC", 71.12, 53.34
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U5": {"1": "NETX"}, "U1": {"2": "NETX"}}}
        )
        assert "wired to existing label" in r["placed"][1]["note"]
        assert _wires(root)
        return root, c

    def test_replace_removes_auto_wire_to_another_pins_label(self, tmp_path):
        root, c = self._wired_pair(tmp_path)
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "OTHER"}}, "replace": True}
        )
        assert r["success"] is True, r
        replaced = r["placed"][0]["replaced"]
        assert [x["type"] for x in replaced] == ["wire"]
        assert replaced[0]["to"] == "NETX"
        # U5 keeps its label; the wire that tied U1/2 to it is gone.
        assert _wires(root) == []
        assert sorted(n for _k, n, *_ in _labels(root)) == ["NETX", "OTHER"]
        nets = HierarchyNets(root)
        by_name = {next(iter(n["names"])): n for n in nets.nets.values() if n["names"]}
        assert {(p["ref"], p["pin"]) for p in by_name["NETX"]["pins"]} == {("U5", "1")}
        assert {(p["ref"], p["pin"]) for p in by_name["OTHER"]["pins"]} == {("U1", "2")}

    def test_replace_keeps_auto_wire_already_on_the_new_net(self, tmp_path):
        root, c = self._wired_pair(tmp_path)
        before = len(_wires(root))
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "NETX"}}, "replace": True}
        )
        assert r["success"] is True, r
        assert not any(x["type"] == "wire" for x in r["placed"][0].get("replaced", []))
        assert len(_wires(root)) >= before
        nets = HierarchyNets(root)
        netx = [n for n in nets.nets.values() if "NETX" in n["names"]][0]
        assert {(p["ref"], p["pin"]) for p in netx["pins"]} == {("U1", "2"), ("U5", "1")}

    def test_replace_keeps_a_wire_that_branches(self, tmp_path):
        # U1/2 (55.88, 50.8) -> (60.96, 50.8), which branches up to label A
        # and right to label B: the run serves more than this pin.
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.wire(55.88, 50.8, 60.96, 50.8)
            + fx.wire(60.96, 50.8, 60.96, 40.64)
            + fx.wire(60.96, 50.8, 71.12, 50.8)
            + fx.junction(60.96, 50.8)
            + fx.label("A", 60.96, 40.64)
            + fx.label("B", 71.12, 50.8)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "NEW"}}, "replace": True}
        )
        assert r["success"] is True, r
        assert "replaced" not in r["placed"][0]
        assert len(_wires(root)) == 3
        assert sorted(n for _k, n, *_ in _labels(root)) == ["A", "B", "NEW"]

    def test_replace_keeps_a_wire_to_a_bare_pin(self, tmp_path):
        # A direct pin-to-pin wire (no label at its end) is the user's, not ours.
        body = (
            fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
            + fx.symbol("test:IC", "U2", "IC", 71.12, 50.8)
            + fx.wire(55.88, 50.8, 66.04, 50.8)
        )
        root = fx.write_project(tmp_path, "p", body)
        c = SchematicBatchCommands(types.SimpleNamespace())
        c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "NEW"}}, "replace": True}
        )
        assert len(_wires(root)) == 1


def _scope_project(tmp_path: Path) -> Path:
    """Root holds U1/U2 and sheet A; A uses VBUS (global) and SDA_A (local only)."""
    root_body = (
        fx.symbol("test:IC", "U1", "IC", 50.8, 50.8)
        + fx.symbol("test:IC", "U2", "IC", 50.8, 76.2)
        + fx.sheet("A", "a.kicad_sch", 101.6, 50.8)
    )
    a_body = fx.label("VBUS", 25.4, 25.4, kind="global_label") + fx.label("SDA_A", 25.4, 50.8)
    return fx.write_project(tmp_path, "proj", root_body, {"a.kicad_sch": a_body})


@pytest.mark.unit
class TestLabelScopeAuto:
    """S3: scope="auto" picks a global label for a net already used on another sheet."""

    def test_auto_picks_global_only_for_names_used_elsewhere(self, tmp_path):
        root = _scope_project(tmp_path)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {
                "schematicPath": str(root),
                "connections": {"U1": {"1": "VBUS", "2": "LOCAL_ONLY"}, "U2": {"2": "SDA_A"}},
                "scope": "auto",
            }
        )
        assert r["success"] is True, r
        kinds = {row["net"]: row["labelType"] for row in r["placed"]}
        assert kinds == {"VBUS": "global_label", "LOCAL_ONLY": "label", "SDA_A": "global_label"}
        assert {(k, n) for k, n, *_ in _labels(root)} == {
            ("global_label", "VBUS"),
            ("label", "LOCAL_ONLY"),
            ("global_label", "SDA_A"),
        }
        # SDA_A exists on A only as a local label: the global one does not join it.
        assert any("SDA_A" in w and "a.kicad_sch" in w for w in r.get("warnings", []))
        assert not any("'VBUS'" in w for w in r.get("warnings", []))

    def test_default_scope_is_unchanged(self, tmp_path):
        root = _scope_project(tmp_path)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect({"schematicPath": str(root), "connections": {"U1": {"1": "VBUS"}}})
        assert r["success"] is True
        assert "labelType" not in r["placed"][0]
        assert [(k, n) for k, n, *_ in _labels(root)] == [("label", "VBUS")]

    def test_explicit_scope_global_and_invalid(self, tmp_path):
        root = _scope_project(tmp_path)
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"1": "X"}}, "scope": "global"}
        )
        assert r["success"] is True
        assert [(k, n) for k, n, *_ in _labels(root)] == [("global_label", "X")]
        bad = c.batch_connect(
            {"schematicPath": str(root), "connections": {"U1": {"2": "Y"}}, "scope": "sheet"}
        )
        assert bad["success"] is False and "scope" in bad["message"]

    def test_auto_from_a_sub_sheet_sees_the_root(self, tmp_path):
        root = fx.write_project(
            tmp_path,
            "proj",
            fx.label("+BATT", 10.16, 10.16, kind="global_label")
            + fx.sheet("A", "a.kicad_sch", 101.6, 50.8),
            {"a.kicad_sch": fx.symbol("test:IC", "U7", "IC", 50.8, 50.8)},
        )
        sub = tmp_path / "a.kicad_sch"
        c = SchematicBatchCommands(types.SimpleNamespace())
        r = c.batch_connect(
            {"schematicPath": str(sub), "connections": {"U7": {"1": "+BATT"}}, "scope": "auto"}
        )
        assert r["placed"][0]["labelType"] == "global_label"
        assert root.exists()

    def test_resolve_label_scope_reports_sheets(self, tmp_path):
        from commands.schematic_connectivity import resolve_label_scope

        root = _scope_project(tmp_path)
        info = resolve_label_scope(root, ["VBUS", "SDA_A", "NEW"])
        assert info["NEW"] == {"labelType": "label", "sheets": [], "localOnly": []}
        assert info["VBUS"]["labelType"] == "global_label"
        assert info["VBUS"]["localOnly"] == []
        assert [Path(f).name for f in info["SDA_A"]["localOnly"]] == ["a.kicad_sch"]

    def test_connect_to_net_scope_auto(self, tmp_path):
        from kicad_interface import KiCADInterface

        root = _scope_project(tmp_path)
        iface = KiCADInterface.__new__(KiCADInterface)
        iface.board = None
        r = iface._handle_connect_to_net(
            {
                "schematicPath": str(root),
                "componentRef": "U1",
                "pinName": "1",
                "netName": "VBUS",
                "scope": "auto",
            }
        )
        assert r["success"] is True, r
        assert r["label_type"] == "global_label"
        r = iface._handle_connect_to_net(
            {
                "schematicPath": str(root),
                "componentRef": "U1",
                "pinName": "2",
                "netName": "FRESH",
                "scope": "auto",
            }
        )
        assert r["label_type"] == "label"
        assert {(k, n) for k, n, *_ in _labels(root)} == {
            ("global_label", "VBUS"),
            ("label", "FRESH"),
        }


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
    # The root's local VBUS label is wired to the Sub sheet pin, so it is the
    # same net as Sub's hierarchical label.
    root_body = (
        fx.label("VBUS", 10.16, 53.34)
        + fx.wire(10.16, 53.34, 50.8, 53.34)
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


def _scoped_project(tmp_path: Path) -> Path:
    """Two sub-sheets with same-named local labels that are different nets.

    * root: local SDA (alone), global VCC
    * a.kicad_sch ("A"): local SDA (alone); local VCC wired to a global VCC
    * b.kicad_sch ("B"): local SDA (alone); local VCC (alone)
    """
    root_body = (
        fx.label("SDA", 10.16, 10.16)
        + fx.label("VCC", 20.32, 10.16, kind="global_label")
        + fx.sheet("A", "a.kicad_sch", 50.8, 50.8)
        + fx.sheet("B", "b.kicad_sch", 101.6, 50.8)
    )
    a_body = (
        fx.label("SDA", 25.4, 25.4)
        + fx.label("VCC", 25.4, 50.8)
        + fx.wire(25.4, 50.8, 50.8, 50.8)
        + fx.label("VCC", 50.8, 50.8, kind="global_label")
    )
    b_body = fx.label("SDA", 25.4, 25.4) + fx.label("VCC", 25.4, 50.8)
    return fx.write_project(
        tmp_path, "proj", root_body, {"a.kicad_sch": a_body, "b.kicad_sch": b_body}
    )


def _names(path: Path, kind: str = "label"):
    return sorted(n for k, n, *_ in _labels(path) if k == kind)


@pytest.mark.unit
class TestRelabelNetScoping:
    """Local labels are per sheet: a same-named local label elsewhere is another net."""

    def test_bare_name_renames_only_the_given_sheets_local_label(self, tmp_path):
        root = _scoped_project(tmp_path)
        r = relabel_net({"schematicPath": str(root), "oldName": "SDA", "newName": "I2C_SDA"})
        assert r["success"] is True, r
        assert r["renamed"] == 1
        assert _names(root) == ["I2C_SDA"]
        assert _names(tmp_path / "a.kicad_sch") == ["SDA", "VCC"]
        assert _names(tmp_path / "b.kicad_sch") == ["SDA", "VCC"]
        assert {Path(u["file"]).name for u in r["untouchedLocalLabels"]} == {
            "a.kicad_sch",
            "b.kicad_sch",
        }
        assert r["warnings"]

    def test_hierarchical_path_names_the_sheet(self, tmp_path):
        root = _scoped_project(tmp_path)
        r = relabel_net({"schematicPath": str(root), "oldName": "/B/SDA", "newName": "SDA_B"})
        assert r["success"] is True, r
        assert r["renamed"] == 1
        assert _names(tmp_path / "b.kicad_sch") == ["SDA_B", "VCC"]
        assert _names(root) == ["SDA"]
        assert _names(tmp_path / "a.kicad_sch") == ["SDA", "VCC"]

    def test_given_sub_sheet_scopes_a_bare_name(self, tmp_path):
        _scoped_project(tmp_path)
        r = relabel_net(
            {"schematicPath": str(tmp_path / "a.kicad_sch"), "oldName": "SDA", "newName": "X"}
        )
        assert r["renamed"] == 1
        assert _names(tmp_path / "a.kicad_sch") == ["VCC", "X"]
        assert _names(tmp_path / "b.kicad_sch") == ["SDA", "VCC"]

    def test_global_net_takes_local_labels_wired_to_it_only(self, tmp_path):
        root = _scoped_project(tmp_path)
        r = relabel_net({"schematicPath": str(root), "oldName": "VCC", "newName": "VCC_3V3"})
        assert r["success"] is True, r
        # Global labels on root and A, plus A's local label on the same wire.
        assert r["renamed"] == 3
        assert _names(root, "global_label") == ["VCC_3V3"]
        assert _names(tmp_path / "a.kicad_sch", "global_label") == ["VCC_3V3"]
        assert _names(tmp_path / "a.kicad_sch") == ["SDA", "VCC_3V3"]
        # B's local VCC is wired to nothing global: a separate net /B/VCC.
        assert _names(tmp_path / "b.kicad_sch") == ["SDA", "VCC"]

    def test_name_absent_from_the_named_sheet_fails_with_a_hint(self, tmp_path):
        _scoped_project(tmp_path)
        before = (tmp_path / "a.kicad_sch").read_text(encoding="utf-8")
        r = relabel_net(
            {
                "schematicPath": str(tmp_path / "b.kicad_sch"),
                "oldName": "/A/NOPE",
                "newName": "X",
            }
        )
        assert r["success"] is False
        r = relabel_net(
            {"schematicPath": str(tmp_path / "proj.kicad_sch"), "oldName": "/Z/SDA", "newName": "X"}
        )
        assert r["success"] is False and "not found" in r["message"]
        assert (tmp_path / "a.kicad_sch").read_text(encoding="utf-8") == before

    def test_local_label_only_on_another_sheet_reports_where(self, tmp_path):
        root = fx.write_project(
            tmp_path,
            "proj",
            fx.sheet("A", "a.kicad_sch", 50.8, 50.8),
            {"a.kicad_sch": fx.label("SDA", 25.4, 25.4)},
        )
        r = relabel_net({"schematicPath": str(root), "oldName": "SDA", "newName": "X"})
        assert r["success"] is False
        assert "a.kicad_sch" in r["message"] and "/A/SDA" in r["message"]
        assert _names(tmp_path / "a.kicad_sch") == ["SDA"]

    def test_hierarchical_label_follows_sheet_pin_down_and_up(self, tmp_path):
        # root: local EN wired to sheet pin EN of A; A: hierarchical EN, wired
        # to sheet pin EN of nested C; C: hierarchical EN.
        root = fx.write_project(
            tmp_path,
            "proj",
            fx.label("EN", 10.16, 53.34)
            + fx.wire(10.16, 53.34, 50.8, 53.34)
            + fx.sheet("A", "a.kicad_sch", 50.8, 50.8, pins=[("EN", 50.8, 53.34)]),
            {
                "a.kicad_sch": fx.label("EN", 25.4, 25.4, kind="hierarchical_label")
                + fx.wire(25.4, 25.4, 50.8, 25.4)
                + fx.sheet("C", "c.kicad_sch", 50.8, 22.86, pins=[("EN", 50.8, 25.4)]),
                "c.kicad_sch": fx.label("EN", 25.4, 25.4, kind="hierarchical_label"),
            },
        )
        # Start in the middle sheet: the rename must reach both ends.
        r = relabel_net({"schematicPath": str(root), "oldName": "/A/EN", "newName": "ENABLE"})
        assert r["success"] is True, r
        assert r["renamed"] == 5  # 3 labels + 2 sheet pins
        assert _names(root) == ["ENABLE"]
        assert _names(tmp_path / "a.kicad_sch", "hierarchical_label") == ["ENABLE"]
        assert _names(tmp_path / "c.kicad_sch", "hierarchical_label") == ["ENABLE"]
        assert '(pin "ENABLE" bidirectional' in root.read_text(encoding="utf-8")
        assert '(pin "ENABLE" bidirectional' in (tmp_path / "a.kicad_sch").read_text(
            encoding="utf-8"
        )
        nets = HierarchyNets(root)
        assert not any("EN" in n["names"] for n in nets.nets.values())
