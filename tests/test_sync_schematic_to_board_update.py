"""B1: sync_schematic_to_board as a full "Update PCB from Schematic".

Covers the netlist parser (net-name escaping, schematic link, fields, DNP /
exclude-from-BOM), the per-footprint field sync, and the handler's dryRun,
removeExtra and re-net behaviour against a mocked board.
"""

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils.netlist_sync import (  # noqa: E402
    escape_generated_net_name,
    parse_kicad_netlist,
    sync_footprint_fields,
)

NETLIST = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="U1">
      <value>LMR33640</value>
      <footprint>Package_SO:HSOP-8</footprint>
      <datasheet>http://x/ds.pdf</datasheet>
      <fields>
        <field name="LCSC">C1852154</field>
        <field name="Footprint">Package_SO:HSOP-8</field>
        <field name="Datasheet">http://x/ds.pdf</field>
        <field name="Description">Buck</field>
      </fields>
      <property name="LCSC" value="C1852154"/>
      <property name="dnp"/>
      <sheetpath names="/Power/" tstamps="/aaaa/"/>
      <tstamps>uuid-u1</tstamps>
    </comp>
    <comp ref="R1">
      <value>10k</value>
      <footprint>Resistor_SMD:R_0603</footprint>
      <fields>
        <field name="Datasheet"/>
      </fields>
      <property name="exclude_from_bom"/>
      <sheetpath names="/" tstamps="/"/>
      <tstamps>uuid-r1</tstamps>
    </comp>
  </components>
  <nets>
    <net code="1" name="GND">
      <node ref="U1" pin="2"/>
      <node ref="R1" pin="2"/>
    </net>
    <net code="2" name="/Power/VIN">
      <node ref="U1" pin="1"/>
    </net>
    <net code="3" name="unconnected-(U1-GPIO/MFP5-Pad3)">
      <node ref="U1" pin="3"/>
    </net>
    <net code="4" name="Net-(R1-Pad1)">
      <node ref="R1" pin="1"/>
    </net>
  </nets>
</export>
"""


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


@pytest.mark.unit
class TestParseNetlist:
    def test_components(self) -> None:
        comps = {c["reference"]: c for c in parse_kicad_netlist(NETLIST)["components"]}
        u1, r1 = comps["U1"], comps["R1"]
        assert u1["value"] == "LMR33640"
        assert u1["footprint"] == "Package_SO:HSOP-8"
        assert u1["uuid"] == "uuid-u1"
        assert u1["path"] == "/aaaa/uuid-u1"
        assert u1["fields"] == {
            "LCSC": "C1852154",
            "Datasheet": "http://x/ds.pdf",
            "Description": "Buck",
        }
        assert u1["dnp"] is True and u1["exclude_from_bom"] is False
        assert r1["path"] == "/uuid-r1"
        assert r1["fields"] == {"Datasheet": ""}
        assert r1["dnp"] is False and r1["exclude_from_bom"] is True

    def test_pin_nets_escape_slash_in_generated_names_only(self) -> None:
        pin_net = parse_kicad_netlist(NETLIST)["pin_net"]
        assert pin_net[("U1", "3")] == "unconnected-(U1-GPIO{slash}MFP5-Pad3)"
        assert pin_net[("U1", "1")] == "/Power/VIN"  # a sheet path stays a path
        assert pin_net[("R1", "2")] == "GND"

    def test_escape_helper(self) -> None:
        assert escape_generated_net_name("unconnected-(J1-A/B-Pad1)") == (
            "unconnected-(J1-A{slash}B-Pad1)"
        )
        assert escape_generated_net_name("/Sub/A/B") == "/Sub/A/B"


# --------------------------------------------------------------------------- #
# per-footprint field sync
# --------------------------------------------------------------------------- #


class _FakeField:
    def __init__(self) -> None:
        self.visible = True
        self.layer = None

    def SetVisible(self, v: bool) -> None:
        self.visible = v

    def SetLayer(self, layer: Any) -> None:
        self.layer = layer


class _FakeFp:
    def __init__(self, ref: str, value: str, fields: Dict[str, str], path: str = "") -> None:
        self.ref = ref
        self.value = value
        self.fields = dict(fields)
        self.field_objs: Dict[str, _FakeField] = {}
        self.dnp = False
        self.nobom = False
        self.path = path

    def GetReference(self) -> str:
        return self.ref

    def GetValue(self) -> str:
        return self.value

    def SetValue(self, v: str) -> None:
        self.value = v

    def HasField(self, n: str) -> bool:
        return n in self.fields

    def GetFieldText(self, n: str) -> str:
        return self.fields[n]

    def SetField(self, n: str, v: str) -> None:
        self.fields[n] = v

    def GetField(self, n: str) -> _FakeField:
        return self.field_objs.setdefault(n, _FakeField())

    def GetLayer(self) -> int:
        return 0

    def IsDNP(self) -> bool:
        return self.dnp

    def SetDNP(self, v: bool) -> None:
        self.dnp = v

    def IsExcludedFromBOM(self) -> bool:
        return self.nobom

    def SetExcludedFromBOM(self, v: bool) -> None:
        self.nobom = v

    def GetPath(self) -> Any:
        return MagicMock(AsString=MagicMock(return_value=self.path))

    def SetPath(self, p: Any) -> None:
        self.path = p


@pytest.mark.unit
class TestSyncFootprintFields:
    def _u1(self) -> Dict[str, Any]:
        return {c["reference"]: c for c in parse_kicad_netlist(NETLIST)["components"]}["U1"]

    def test_reports_without_applying(self) -> None:
        fp = _FakeFp("U1", "OLD", {"Datasheet": "", "Description": "Buck"}, "/aaaa/uuid-u1")
        changes = sync_footprint_fields(fp, self._u1(), apply=False)
        assert {c["what"] for c in changes} == {"Value", "LCSC", "Datasheet", "DNP"}
        assert fp.value == "OLD" and "LCSC" not in fp.fields and fp.dnp is False

    def test_applies_and_hides_new_fields(self) -> None:
        fp = _FakeFp("U1", "OLD", {"Datasheet": "", "Description": "Buck"})
        with patch.dict(sys.modules["pcbnew"].__dict__, {"KIID_PATH": lambda s: s}):
            changes = sync_footprint_fields(fp, self._u1(), apply=True)
        assert fp.value == "LMR33640"
        assert fp.fields["LCSC"] == "C1852154"
        assert fp.field_objs["LCSC"].visible is False
        assert fp.dnp is True
        assert fp.path == "/aaaa/uuid-u1"
        assert any(c["what"] == "SchematicLink" for c in changes)
        # Idempotent: nothing left to change.
        fp.path = "/aaaa/uuid-u1"
        assert sync_footprint_fields(fp, self._u1(), apply=False) == []

    def test_empty_and_tilde_are_the_same_and_empty_fields_are_not_created(self) -> None:
        r1 = {c["reference"]: c for c in parse_kicad_netlist(NETLIST)["components"]}["R1"]
        fp = _FakeFp("R1", "10k", {"Datasheet": "~"}, "/uuid-r1")
        fp.nobom = True
        assert sync_footprint_fields(fp, r1, apply=False) == []
        fp2 = _FakeFp("R1", "10k", {}, "/uuid-r1")
        fp2.nobom = True
        assert sync_footprint_fields(fp2, r1, apply=True) == []
        assert "Datasheet" not in fp2.fields


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #


class _Net:
    def __init__(self, name: str) -> None:
        self.name = name


class _Pad:
    def __init__(self, number: str, net: str) -> None:
        self.number = number
        self.net = net

    def GetNumber(self) -> str:
        return self.number

    def GetNetname(self) -> str:
        return self.net

    def SetNet(self, net: Any) -> None:
        self.net = net.name


class _BoardFp(_FakeFp):
    def __init__(self, ref: str, lib_id: str, pads: List[_Pad], board_only: bool = False):
        super().__init__(ref, "", {})
        self.lib_id = lib_id
        self.pads = pads
        self.board_only = board_only

    def Pads(self) -> List[_Pad]:
        return self.pads

    def GetFPIDAsString(self) -> str:
        return self.lib_id

    def IsBoardOnly(self) -> bool:
        return self.board_only


class _NetsByName(dict):
    def has_key(self, k: str) -> bool:
        return k in self


class _Board:
    def __init__(self, fps: List[_BoardFp], nets: List[str]) -> None:
        self.fps = fps
        self.nets = _NetsByName({n: _Net(n) for n in nets})
        self.saved: Optional[str] = None
        self.removed: List[str] = []

    def GetFootprints(self) -> List[_BoardFp]:
        return list(self.fps)

    def GetNetInfo(self) -> Any:
        board = self
        return MagicMock(
            NetsByName=lambda: board.nets, OrphanedItem=MagicMock(return_value=_Net(""))
        )

    def Add(self, item: Any) -> None:
        if isinstance(item, _Net):
            self.nets[item.name] = item

    def Delete(self, fp: _BoardFp) -> None:
        self.fps.remove(fp)
        self.removed.append(fp.ref)

    def Save(self, path: str) -> None:
        self.saved = path

    def GetFileName(self) -> str:
        return "board.kicad_pcb"


def _run(board: _Board, tmp_path: Path, **params: Any) -> Dict[str, Any]:
    from kicad_interface import KiCADInterface

    sch = tmp_path / "p.kicad_sch"
    sch.write_text("(kicad_sch)\n")
    iface = KiCADInterface.__new__(KiCADInterface)
    iface.board = board
    iface._update_command_handlers = lambda: None
    lm = MagicMock()
    lm.libraries = {}
    iface._get_project_library_manager = lambda d: lm
    netlist = parse_kicad_netlist(NETLIST)
    with (
        patch.object(KiCADInterface, "_export_schematic_netlist", return_value=netlist),
        patch.object(KiCADInterface, "_build_hierarchical_pad_net_map", return_value=({}, set())),
        patch("commands.schematic_handlers.pcbnew") as pcb,
        patch("utils.netlist_sync.sync_footprint_fields", return_value=[]),
        patch("commands.schematic_handlers.preserve_project_settings"),
    ):
        pcb.NETINFO_ITEM.side_effect = lambda b, name: _Net(name)
        return iface._handle_sync_schematic_to_board(
            {"schematicPath": str(sch), "boardPath": None, **params}
        )


def _board() -> _Board:
    u1 = _BoardFp(
        "U1",
        "Package_SO:HSOP-8",
        [_Pad("1", "GND"), _Pad("2", "GND"), _Pad("3", ""), _Pad("9", "GND"), _Pad("", "")],
    )
    r1 = _BoardFp("R1", "Resistor_SMD:R_0603", [_Pad("1", ""), _Pad("2", "GND")])
    gone = _BoardFp("R7", "Resistor_SMD:R_0603", [_Pad("1", "GND")])
    logo = _BoardFp("LOGO1", "Logo:X", [], board_only=True)
    return _Board([u1, r1, gone, logo], ["GND"])


@pytest.mark.unit
class TestSyncHandler:
    def test_dry_run_reports_and_changes_nothing(self, tmp_path: Path) -> None:
        board = _board()
        r = _run(board, tmp_path, dryRun=True)
        assert r["success"] is True and r["dryRun"] is True
        assert board.saved is None
        assert [e["reference"] for e in r["footprints_without_symbol"]] == ["R7"]
        assert r["footprints_removed"] == []
        changes = {c["pad"]: (c["from"], c["to"]) for c in r["pad_net_changes"]}
        assert changes == {
            "U1/1": ("GND", "/Power/VIN"),
            "U1/3": ("", "unconnected-(U1-GPIO{slash}MFP5-Pad3)"),
            "R1/1": ("", "Net-(R1-Pad1)"),
        }
        assert r["pads_cleared"] == ["U1/9 (GND)"]
        assert sorted(r["nets_added"]) == [
            "/Power/VIN",
            "Net-(R1-Pad1)",
            "unconnected-(U1-GPIO{slash}MFP5-Pad3)",
        ]
        # Untouched board.
        u1 = board.fps[0]
        assert [p.net for p in u1.pads] == ["GND", "GND", "", "GND", ""]
        assert "GND" in board.nets and "/Power/VIN" not in board.nets
        assert any("removeExtra" in w for w in r["warnings"])

    def test_apply_renets_every_pad_and_keeps_extra_by_default(self, tmp_path: Path) -> None:
        board = _board()
        r = _run(board, tmp_path)
        assert r["success"] is True and r["dryRun"] is False
        assert board.saved == "board.kicad_pcb"
        u1, r1, r7, logo = board.fps
        assert [p.net for p in u1.pads] == [
            "/Power/VIN",
            "GND",
            "unconnected-(U1-GPIO{slash}MFP5-Pad3)",
            "",  # pad 9 has no pin in the symbol: disconnected, like F8
            "",  # unnumbered pad untouched
        ]
        assert [p.net for p in r1.pads] == ["Net-(R1-Pad1)", "GND"]
        assert [p.net for p in r7.pads] == ["GND"]  # not in the schematic: left alone
        assert board.removed == []
        assert [e["reference"] for e in r["footprints_without_symbol"]] == ["R7"]

    def test_remove_extra_removes_only_symbolless_non_board_only(self, tmp_path: Path) -> None:
        board = _board()
        r = _run(board, tmp_path, removeExtra=True)
        assert board.removed == ["R7"]
        assert [e["reference"] for e in r["footprints_removed"]] == ["R7"]
        assert r["footprints_without_symbol"] == []
        assert "LOGO1" in [fp.ref for fp in board.fps]

    def test_remove_extra_in_a_dry_run_removes_nothing(self, tmp_path: Path) -> None:
        board = _board()
        r = _run(board, tmp_path, removeExtra=True, dryRun=True)
        assert board.removed == []
        assert r["footprints_removed"] == []
        assert [e["reference"] for e in r["footprints_without_symbol"]] == ["R7"]

    def test_dry_run_skips_auto_save(self) -> None:
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
        iface.use_ipc = False
        iface.ipc_backend = None
        iface.board = MagicMock()
        iface._auto_save_board = MagicMock(return_value={"saved": True})
        iface.command_routes = {
            "sync_schematic_to_board": lambda p: {"success": True, "dryRun": bool(p.get("dryRun"))}
        }
        with patch.object(KiCADInterface, "_refuse_if_schematic_open", return_value=None):
            iface.handle_command("sync_schematic_to_board", {"dryRun": True})
            iface._auto_save_board.assert_not_called()
            iface.handle_command("sync_schematic_to_board", {})
            iface._auto_save_board.assert_called_once()


@pytest.mark.unit
class TestDryRunHelpers:
    """Dry runs check the library files instead of loading or swapping anything."""

    def _iface(self, lib_dir: Path) -> Any:
        from kicad_interface import KiCADInterface

        iface = KiCADInterface.__new__(KiCADInterface)
        lm = MagicMock()
        lm.libraries = {"Resistor_SMD": str(lib_dir)}
        iface._get_project_library_manager = lambda d: lm
        return iface

    def test_swap_is_reported_not_performed(self, tmp_path: Path) -> None:
        lib = tmp_path / "Resistor_SMD.pretty"
        lib.mkdir()
        (lib / "R_0805.kicad_mod").write_text("(footprint)")
        old = MagicMock(name="R1")
        old.GetReference.return_value = "R1"
        old.GetFPIDAsString.return_value = "Resistor_SMD:R_0603"
        board = MagicMock()
        board.GetFootprints.return_value = [old]
        sch = str(tmp_path / "x.kicad_sch")
        with (
            patch("utils.footprint_swap.load_library_footprint") as load,
            patch("utils.footprint_swap.swap_footprint") as swap,
        ):
            out = self._iface(lib)._sync_changed_footprints(
                board, sch, [{"reference": "R1", "footprint": "Resistor_SMD:R_0805"}], dry_run=True
            )
            missing = self._iface(lib)._sync_changed_footprints(
                board, sch, [{"reference": "R1", "footprint": "Resistor_SMD:R_9999"}], dry_run=True
            )
        load.assert_not_called()
        swap.assert_not_called()
        assert out["replaced"][0]["dryRun"] is True
        assert "not in 'Resistor_SMD'" in missing["mismatched"][0]["reason"]

    def test_add_is_reported_not_performed(self, tmp_path: Path) -> None:
        lib = tmp_path / "Resistor_SMD.pretty"
        lib.mkdir()
        (lib / "R_0805.kicad_mod").write_text("(footprint)")
        board = MagicMock()
        board.GetFootprints.return_value = []
        comps = [
            {"reference": "R1", "value": "1k", "footprint": "Resistor_SMD:R_0805", "uuid": "u1"},
            {"reference": "R2", "value": "1k", "footprint": "Resistor_SMD:R_9999", "uuid": "u2"},
            {
                "reference": "R3",
                "value": "1k",
                "footprint": "Resistor_SMD:R_0805",
                "uuid": "u3",
                "exclude_from_board": True,
            },
        ]
        with patch("commands.schematic_handlers.pcbnew") as pcb:
            added, skipped = self._iface(lib)._add_missing_footprints_from_schematic(
                board, str(tmp_path / "x.kicad_sch"), components=comps, apply=False
            )
        pcb.FootprintLoad.assert_not_called()
        board.Add.assert_not_called()
        assert added == [{"reference": "R1", "footprint": "Resistor_SMD:R_0805"}]
        reasons = {s["reference"]: s["reason"] for s in skipped}
        assert "not in 'Resistor_SMD'" in reasons["R2"]
        assert reasons["R3"] == "excluded from board"
