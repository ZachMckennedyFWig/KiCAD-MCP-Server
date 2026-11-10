"""Schematic symbol placement fidelity and failure safety.

Regression tests for the MCP_ISSUES report items:

- #5e / #5p: the symbol embedded in a sheet's ``lib_symbols`` is the library
  text verbatim (only the top-level name gains its ``Lib:`` prefix), and pin
  positions are computed from that same text, including rotation and mirror.
  The root cause of the reported drift (QOD on the CT row, SolderJumper pins at
  ±3.81, USB-C ``SH`` -> ``S1``, ``(pin_numbers hide)``, ``Datasheet "~"``) was
  library *resolution*: KiCad 10's global sym-lib-table is a single
  ``(type "Table")`` row the loader did not follow, so it fell through to an
  older ``8.0`` table and embedded KiCad 8 symbols.
- #5 / #5b / #5o: symbols missing from the older libraries were "not found",
  and multi-level ``extends`` chains were embedded flat with no ``_1_1`` unit.
- #5c: a failed placement leaves the file byte-identical.
- #3: ``batch_add_and_connect`` reports per-component failure reasons.
- #2: non-ASCII library text round-trips (UTF-8 everywhere, kicad-skip too).
- #4: writes are atomic and keep the file's newline style.

Tests against the installed KiCad libraries (and kicad-cli as the oracle) skip
when those are not available.
"""

import os
import re
import shutil
import subprocess
import sys
import types
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands import schematic_batch as sb  # noqa: E402
from commands.dynamic_symbol_loader import DynamicSymbolLoader, SymbolLibraryError  # noqa: E402
from commands.pin_locator import PinLocator  # noqa: E402
from commands.schematic_batch import SchematicBatchCommands  # noqa: E402
from utils.file_io import FileTransaction, patch_kicad_skip_utf8  # noqa: E402

TEMPLATES = Path(__file__).parent.parent / "python" / "templates"


@pytest.fixture(autouse=True)
def _clean_loader_caches():
    DynamicSymbolLoader.clear_library_caches()
    yield
    DynamicSymbolLoader.clear_library_caches()


def _new_sheet(directory: Path, name: str = "sheet.kicad_sch") -> Path:
    """A blank KiCad 10 sheet with a real (non-nil) root uuid."""
    directory.mkdir(parents=True, exist_ok=True)
    sch = directory / name
    text = (TEMPLATES / "blank.kicad_sch").read_text(encoding="utf-8")
    text = text.replace("00000000-0000-0000-0000-000000000000", str(uuid.uuid4()))
    sch.write_bytes(text.encode("utf-8"))
    return sch


def _batch() -> SchematicBatchCommands:
    iface = types.SimpleNamespace(
        footprint_library=types.SimpleNamespace(find_footprint=lambda fp: object())
    )
    return SchematicBatchCommands(iface)


def _lib_symbols_entry(sheet_text: str, full_name: str) -> str:
    from utils.sexpr_format import match_paren

    start = sheet_text.index(f'(symbol "{full_name}"')
    return sheet_text[start : match_paren(sheet_text, start) + 1]


# --------------------------------------------------------------------------- #
# A KiCad 10 format library written to disk (no KiCad install needed)          #
# --------------------------------------------------------------------------- #

_PIN = """\t\t\t(pin {etype} line
\t\t\t\t(at {x} {y} {angle})
\t\t\t\t(length 2.54)
\t\t\t\t(name "{name}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t\t(number "{number}"
\t\t\t\t\t(effects
\t\t\t\t\t\t(font
\t\t\t\t\t\t\t(size 1.27 1.27)
\t\t\t\t\t\t)
\t\t\t\t\t)
\t\t\t\t)
\t\t\t)
"""


def _prop(name: str, value: str, hide: bool = False) -> str:
    return (
        f'\t\t(property "{name}" "{value}"\n'
        "\t\t\t(at 0 0 0)\n"
        "\t\t\t(show_name no)\n"
        "\t\t\t(do_not_autoplace no)\n"
        + ("\t\t\t(hide yes)\n" if hide else "")
        + "\t\t\t(effects\n"
        "\t\t\t\t(font\n"
        "\t\t\t\t\t(size 1.27 1.27)\n"
        "\t\t\t\t)\n"
        "\t\t\t)\n"
        "\t\t)\n"
    )


def _base_symbol(name: str, manufacturer: str = "ACME") -> str:
    pins = "".join(
        _PIN.format(etype=e, x=x, y=y, angle=a, name=n, number=num)
        for e, x, y, a, n, num in (
            ("passive", -7.62, 2.54, 0, "", "1"),
            ("passive", -7.62, 0, 0, "IN", "2"),
            ("open_collector", 7.62, -2.54, 180, "QOD", "5"),
            ("passive", 0, -7.62, 90, "SHIELD", "SH"),
        )
    )
    return (
        f'\t(symbol "{name}"\n'
        "\t\t(pin_numbers\n\t\t\t(hide yes)\n\t\t)\n"
        "\t\t(pin_names\n\t\t\t(offset 0.254)\n\t\t)\n"
        "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n"
        "\t\t(in_pos_files yes)\n\t\t(duplicate_pin_numbers_are_jumpers no)\n"
        + _prop("Reference", "U")
        + _prop("Value", name)
        + _prop("Footprint", "", hide=True)
        + _prop("Datasheet", "", hide=True)
        + _prop("Description", "Base part", hide=True)
        + _prop("Manufacturer", manufacturer, hide=True)
        + _prop("ki_keywords", "base keywords", hide=True)
        + f'\t\t(symbol "{name}_0_1"\n'
        "\t\t\t(rectangle\n\t\t\t\t(start -5.08 5.08)\n\t\t\t\t(end 5.08 -5.08)\n"
        "\t\t\t\t(stroke\n\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n"
        "\t\t\t\t(fill\n\t\t\t\t\t(type background)\n\t\t\t\t)\n\t\t\t)\n"
        "\t\t)\n"
        f'\t\t(symbol "{name}_1_1"\n' + pins + "\t\t)\n"
        "\t\t(embedded_fonts no)\n"
        "\t)\n"
    )


def _derived_symbol(name: str, parent: str, extra: str = "") -> str:
    return (
        f'\t(symbol "{name}"\n'
        f'\t\t(extends "{parent}")\n'
        + _prop("Reference", "U")
        + _prop("Value", name)
        + _prop("Footprint", "Package_SO:SOIC-8", hide=True)
        + _prop("Datasheet", f"https://example.com/{name}.pdf", hide=True)
        + _prop("Description", f"{name} description", hide=True)
        + _prop("ki_keywords", "", hide=True)
        + extra
        + "\t\t(embedded_fonts no)\n"
        "\t)\n"
    )


def _library(*symbols: str) -> str:
    return (
        "(kicad_symbol_lib\n\t(version 20251024)\n"
        '\t(generator "kicad_symbol_editor")\n\t(generator_version "10.0")\n'
        + "".join(symbols)
        + ")\n"
    )


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project dir with a project-local sym-lib-table holding library ``T``."""
    proj = tmp_path / "proj"
    (proj / "lib").mkdir(parents=True)
    # Isolate from the machine's own global tables and stock libraries.
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path / "no_stock_libs"))
    lib = proj / "lib" / "T.kicad_sym"
    lib.write_text(
        _library(
            # Child first, then its parent, then the grandparent: the order the
            # stock Memory_RAM library uses for APS6404L-3SQRx-SN.
            _derived_symbol(
                "Grandchild",
                "Child",
                extra=_prop("Grandchild_only", "gc", hide=True),
            ),
            _derived_symbol("Child", "Base", extra=_prop("Child_only", "c", hide=True)),
            _base_symbol("Base", manufacturer="XKB Connection(中国星坤)"),
            _derived_symbol("Orphan", "Ghost"),
            _derived_symbol("LoopA", "LoopB"),
            _derived_symbol("LoopB", "LoopA"),
            # A flat vendor capture: a pin directly under the symbol, no unit.
            '\t(symbol "Flat"\n'
            + _prop("Reference", "U")
            + _prop("Value", "Flat")
            + _PIN.format(etype="passive", x=0, y=0, angle=0, name="A", number="1").replace(
                "\t\t\t", "\t\t", 1
            )
            + "\t)\n",
        ),
        encoding="utf-8",
        newline="\n",
    )
    (proj / "sym-lib-table").write_text(
        "(sym_lib_table\n\t(version 7)\n"
        '\t(lib (name "T") (type "KiCad") (uri "${KIPRJMOD}/lib/T.kicad_sym") (options "")'
        ' (descr ""))\n)\n',
        encoding="utf-8",
    )
    return proj


# --------------------------------------------------------------------------- #
# #5e: verbatim embedding                                                      #
# --------------------------------------------------------------------------- #


class TestVerbatimEmbedding:
    def test_embedded_block_is_library_text_with_prefixed_name(self, project):
        lib_text = (project / "lib" / "T.kicad_sym").read_text(encoding="utf-8")
        loader = DynamicSymbolLoader(project_path=project)
        block = loader.extract_symbol_from_library("T", "Base")
        original = loader._extract_symbol_block(lib_text, "Base")
        assert block == original.replace('(symbol "Base"', '(symbol "T:Base"', 1)

    def test_sheet_entry_has_library_tokens_unchanged(self, project):
        sch = _new_sheet(project)
        r = _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": "T:Base", "reference": "U1", "position": {"x": 50, "y": 50}}
                ],
            }
        )
        assert r["success"], r
        text = sch.read_text(encoding="utf-8")
        entry = _lib_symbols_entry(text, "T:Base")
        lib_block = DynamicSymbolLoader(project_path=project)._extract_symbol_block(
            (project / "lib" / "T.kicad_sym").read_text(encoding="utf-8"), "Base"
        )
        expected = lib_block.replace('(symbol "Base"', '(symbol "T:Base"', 1)
        # Only indentation may differ; every token is the library's.
        assert entry.split() == expected.split()
        # The specific drift reported in #5e must not happen.
        assert "(pin_numbers\n" in entry or "(pin_numbers (hide yes))" in " ".join(entry.split())
        assert "(pin_numbers hide)" not in entry
        assert '(property "Datasheet" ""' in entry
        assert '(name ""' in entry
        assert '(number "SH"' in entry
        for token in ("(show_name no)", "(do_not_autoplace no)", "(in_pos_files yes)"):
            assert token in entry
        assert "(embedded_fonts no)" in entry

    def test_existing_embedded_copy_is_not_rewritten(self, project):
        sch = _new_sheet(project)
        loader = DynamicSymbolLoader(project_path=project)
        loader.add_component(sch, "T", "Base", reference="U1", x=50, y=50)
        before = _lib_symbols_entry(sch.read_text(encoding="utf-8"), "T:Base")
        loader.add_component(sch, "T", "Base", reference="U2", x=80, y=50)
        text = sch.read_text(encoding="utf-8")
        assert text.count('(symbol "T:Base"') == 1
        assert _lib_symbols_entry(text, "T:Base") == before


# --------------------------------------------------------------------------- #
# #5b / #5o: extends chains of any depth                                       #
# --------------------------------------------------------------------------- #


class TestExtendsFlattening:
    def test_two_level_chain_is_flattened_with_renamed_units(self, project):
        block = DynamicSymbolLoader(project_path=project).extract_symbol_from_library(
            "T", "Grandchild"
        )
        names = re.findall(r'\(symbol "([^"]+)"', block)
        assert names == ["T:Grandchild", "Grandchild_0_1", "Grandchild_1_1"]
        assert "(extends" not in block
        # Pins and graphics from the root ancestor.
        assert re.findall(r'\(number "([^"]+)"', block) == ["1", "2", "5", "SH"]
        # Own fields win; fields only an ancestor has are kept; fields only a
        # descendant has are added; empty keywords inherit the root's.
        assert '(property "Value" "Grandchild"' in block
        assert '(property "Datasheet" "https://example.com/Grandchild.pdf"' in block
        assert '(property "Manufacturer" "XKB Connection(中国星坤)"' in block
        assert '(property "Grandchild_only" "gc"' in block
        assert '(property "ki_keywords" "base keywords"' in block
        # Flags come from the root, as in KiCad's LIB_SYMBOL::Flatten.
        assert "(in_pos_files yes)" in block
        # Fields stay ahead of the unit sub-symbols.
        assert block.index("Grandchild_only") < block.index('"Grandchild_0_1"')
        assert block.count("(embedded_fonts no)") == 1

    def test_flattened_chain_places_and_locates_pins(self, project):
        sch = _new_sheet(project)
        r = _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": "T:Grandchild", "reference": "U1", "position": {"x": 50, "y": 50}}
                ],
            }
        )
        assert r["success"], r
        pins = PinLocator().get_all_symbol_pins(sch, "U1")
        assert set(pins) == {"1", "2", "5", "SH"}

    def test_missing_parent_is_refused(self, project):
        with pytest.raises(SymbolLibraryError, match="Ghost"):
            DynamicSymbolLoader(project_path=project).extract_symbol_from_library("T", "Orphan")

    def test_cyclic_chain_is_refused(self, project):
        with pytest.raises(SymbolLibraryError, match="Cyclic"):
            DynamicSymbolLoader(project_path=project).extract_symbol_from_library("T", "LoopA")

    def test_flat_symbol_is_refused_with_repair_hint(self, project):
        with pytest.raises(SymbolLibraryError, match="repair_flat_symbols"):
            DynamicSymbolLoader(project_path=project).extract_symbol_from_library("T", "Flat")


# --------------------------------------------------------------------------- #
# #5 / #5e root cause: KiCad 10's nested global table                         #
# --------------------------------------------------------------------------- #


def test_newest_global_table_with_nested_table_row_wins(tmp_path, monkeypatch):
    """KiCad 10's global table is one ``(type "Table")`` row; an older 8.0
    table on the same machine must not shadow it."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(tmp_path / "none"))
    for var in ("KICAD10_TEMPLATE_DIR", "KICAD9_TEMPLATE_DIR", "KICAD8_TEMPLATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    new_lib = tmp_path / "k10" / "symbols" / "Power.kicad_sym"
    old_lib = tmp_path / "k8" / "symbols" / "Power.kicad_sym"
    for lib in (new_lib, old_lib):
        lib.parent.mkdir(parents=True)
        lib.write_text(_library(_base_symbol("X")), encoding="utf-8")
    template = tmp_path / "k10" / "template"
    template.mkdir()
    (template / "sym-lib-table").write_text(
        f'(sym_lib_table (version 7) (lib (name "Power") (type "KiCad") (uri "{new_lib.as_posix()}")'
        ' (options "") (descr "")))\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("KICAD10_TEMPLATE_DIR", str(template))
    cfg = home / ("AppData/Roaming/kicad" if os.name == "nt" else ".config/kicad")
    (cfg / "10.0").mkdir(parents=True)
    (cfg / "8.0").mkdir(parents=True)
    (cfg / "10.0" / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "KiCad") (type "Table")'
        ' (uri "${KICAD10_TEMPLATE_DIR}/sym-lib-table") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    (cfg / "8.0" / "sym-lib-table").write_text(
        f'(sym_lib_table (lib (name "Power") (type "KiCad") (uri "{old_lib.as_posix()}")'
        ' (options "") (descr "")))\n',
        encoding="utf-8",
    )
    resolved = DynamicSymbolLoader().find_library_file("Power")
    assert resolved is not None and Path(resolved).resolve() == new_lib.resolve()


# --------------------------------------------------------------------------- #
# #5c: failures leave the file byte-identical                                  #
# --------------------------------------------------------------------------- #


class TestFailedPlacementWritesNothing:
    @pytest.mark.parametrize(
        "symbol, reason",
        [
            ("T:DoesNotExist", "not found"),
            ("T:Orphan", "Ghost"),
            ("T:Flat", "repair_flat_symbols"),
            ("Nope:Base", "not found"),
        ],
    )
    def test_batch_failure_leaves_file_identical(self, project, symbol, reason):
        sch = _new_sheet(project)
        # Something already on the sheet, so "identical" covers real content.
        assert _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": "T:Base", "reference": "U1", "position": {"x": 50, "y": 50}}
                ],
            }
        )["success"]
        before = sch.read_bytes()
        r = _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": symbol, "reference": "U9", "position": {"x": 90, "y": 90}}
                ],
            }
        )
        assert r["added_count"] == 0
        assert reason in r["errors"][0]["error"]
        assert sch.read_bytes() == before

    def test_failure_after_instance_write_is_rolled_back(self, project, monkeypatch):
        sch = _new_sheet(project)
        before = sch.read_bytes()

        def boom(*a, **k):
            raise RuntimeError("pin lookup exploded")

        monkeypatch.setattr(PinLocator, "get_all_symbol_pins", boom)
        r = _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": "T:Base", "reference": "U1", "position": {"x": 50, "y": 50}}
                ],
            }
        )
        assert r["added_count"] == 0
        assert "pin lookup exploded" in r["errors"][0]["error"]
        assert sch.read_bytes() == before
        assert "U1" not in sch.read_text(encoding="utf-8")

    def test_add_component_rolls_back_when_instance_step_raises(self, project, monkeypatch):
        sch = _new_sheet(project)
        before = sch.read_bytes()

        def boom(self, *a, **k):
            raise RuntimeError("instance write failed")

        monkeypatch.setattr(DynamicSymbolLoader, "create_component_instance", boom)
        with pytest.raises(RuntimeError):
            DynamicSymbolLoader(project_path=project).add_component(
                sch, "T", "Base", reference="U1", x=10, y=10
            )
        assert sch.read_bytes() == before

    def test_partial_batch_keeps_good_and_drops_bad(self, project):
        sch = _new_sheet(project)
        r = _batch().batch_add_components(
            {
                "schematicPath": str(sch),
                "components": [
                    {"symbol": "T:Base", "reference": "U1", "position": {"x": 50, "y": 50}},
                    {"symbol": "T:Orphan", "reference": "U2", "position": {"x": 90, "y": 50}},
                ],
            }
        )
        text = sch.read_text(encoding="utf-8")
        assert r["added_count"] == 1 and r["error_count"] == 1
        assert '(reference "U1")' in text
        assert '(reference "U2")' not in text
        assert "T:Orphan" not in text

    def test_file_transaction_restores_snapshot(self, tmp_path):
        f = tmp_path / "x.kicad_sch"
        f.write_bytes(b"(kicad_sch)\r\n")
        with pytest.raises(ValueError):
            with FileTransaction(f):
                f.write_bytes(b"garbage")
                raise ValueError("fail")
        assert f.read_bytes() == b"(kicad_sch)\r\n"


# --------------------------------------------------------------------------- #
# #3: batch_add_and_connect reports the reasons                                #
# --------------------------------------------------------------------------- #


def test_batch_add_and_connect_message_carries_reasons(project):
    sch = _new_sheet(project)
    r = _batch().batch_add_and_connect(
        {
            "schematicPath": str(sch),
            "components": [
                {"symbol": "T:Orphan", "reference": "U1", "nets": {"1": "A"}},
                {"symbol": "T:DoesNotExist", "reference": "U2", "nets": {"1": "B"}},
            ],
        }
    )
    assert r["success"] is False
    assert "U1 (T:Orphan)" in r["message"] and "Ghost" in r["message"]
    assert "U2 (T:DoesNotExist)" in r["message"] and "not found" in r["message"]
    assert {e["reference"] for e in r["errors"]} == {"U1", "U2"}


def test_batch_add_and_connect_surfaces_whole_batch_failure(monkeypatch):
    c = _batch()
    c.batch_add_components = lambda p: {"success": False, "message": "sheet unreadable"}
    r = c.batch_add_and_connect(
        {"schematicPath": "/x.kicad_sch", "components": [{"symbol": "T:A", "reference": "U1"}]}
    )
    assert r["success"] is False
    assert "sheet unreadable" in r["message"]


# --------------------------------------------------------------------------- #
# #2 / #4: UTF-8 and atomic, newline-preserving writes                        #
# --------------------------------------------------------------------------- #


def test_non_ascii_library_text_places_and_connects(project):
    """easyeda2kicad-style ``Manufacturer "XKB Connection(中国星坤)"`` must not
    break this or any later call on the sheet (kicad-skip included)."""
    sch = _new_sheet(project)
    r = _batch().batch_add_and_connect(
        {
            "schematicPath": str(sch),
            "components": [
                {
                    "symbol": "T:Base",
                    "reference": "U1",
                    "position": {"x": 50.8, "y": 50.8},
                    "nets": {"2": "SIG_IN", "5": "QOD_NET"},
                }
            ],
        }
    )
    assert r["success"], r
    assert r["connected_count"] == 2, r
    text = sch.read_bytes().decode("utf-8")
    assert "XKB Connection(中国星坤)" in text
    # A second call re-reads the sheet through kicad-skip.
    r2 = _batch().batch_connect({"schematicPath": str(sch), "connections": {"U1": {"1": "X"}}})
    assert r2["success"], r2


def test_kicad_skip_reads_utf8(tmp_path):
    pytest.importorskip("skip")
    from skip.sexp import util as skip_util

    if not patch_kicad_skip_utf8():
        pytest.skip("kicad-skip not installed")
    f = tmp_path / "u.kicad_sch"
    f.write_bytes('(kicad_sch (property "M" "中国星坤"))'.encode("utf-8"))
    tree = skip_util.loadTree(str(f))
    assert "中国星坤" in str(tree)


def test_writes_keep_crlf_and_leave_no_temp_file(project):
    sch = _new_sheet(project)
    sch.write_bytes(sch.read_bytes().replace(b"\n", b"\r\n"))
    loader = DynamicSymbolLoader(project_path=project)
    loader.add_component(sch, "T", "Base", reference="U1", x=50, y=50)
    raw = sch.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert not list(project.glob("*.mcp-tmp"))


# --------------------------------------------------------------------------- #
# #5e / #5p against the installed KiCad libraries                              #
# --------------------------------------------------------------------------- #

_STOCK = [
    ("Power_Management", "TPS22917DBV"),
    ("Jumper", "SolderJumper_2_Bridged"),
    ("Connector", "USB_C_Receptacle_USB2.0_16P"),
    ("MCU_RaspberryPi", "RP2350B"),
    ("Power_Management", "TPS22810DBV"),
    ("Memory_RAM", "APS6404L-3SQRx-SN"),
    ("Interface_UART", "THVD1450D"),
]


def _stock_library(lib: str) -> Path:
    path = DynamicSymbolLoader().find_library_file(lib)
    if path is None or Path(path).is_dir():
        pytest.skip(f"KiCad library {lib} not installed")
    return Path(path)


def _is_kicad10(lib_path: Path) -> bool:
    head = lib_path.read_text(encoding="utf-8")[:300]
    return '(generator_version "10.' in head


def _library_pins(lib_path: Path, symbol: str):
    """Pin (number, x, y) for *symbol* parsed straight from the library file,
    walking the extends chain by hand: an oracle independent of the loader."""
    text = lib_path.read_text(encoding="utf-8")
    name = symbol
    for _ in range(10):
        m = re.search(r'\n\t\(symbol "' + re.escape(name) + r'"\n(.*?)\n\t\)\n', text, re.DOTALL)
        assert m, f"{name} not in {lib_path}"
        ext = re.search(r'^\t\t\(extends "([^"]+)"\)', m.group(1), re.MULTILINE)
        if not ext:
            break
        name = ext.group(1)
    return [
        (num, float(x), float(y))
        for x, y, num in re.findall(
            r'\(pin \w+ \w+\s+\(at ([-\d.]+) ([-\d.]+) [-\d.]+\).*?\(number "([^"]*)"',
            m.group(1),
            re.DOTALL,
        )
    ]


def _expected_world(x, y, sx, sy, rotation, mirror_y=False):
    """KiCad placement transform written out per angle (screen Y is down)."""
    ux, uy = x, -y  # library Y-up -> screen Y-down
    rx, ry = {0: (ux, uy), 90: (uy, -ux), 180: (-ux, -uy), 270: (-uy, ux)}[rotation]
    if mirror_y:
        rx = -rx
    return sx + rx, sy + ry


@pytest.mark.integration
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("lib, symbol", _STOCK)
def test_pin_positions_match_library_at_every_rotation(tmp_path, lib, symbol, rotation):
    lib_path = _stock_library(lib)
    if not re.search(r'\(symbol "' + re.escape(symbol) + '"', lib_path.read_text(encoding="utf-8")):
        pytest.skip(f"{lib}:{symbol} not in installed library")
    sch = _new_sheet(tmp_path)
    sx, sy = 101.6, 101.6
    r = _batch().batch_add_components(
        {
            "schematicPath": str(sch),
            "components": [
                {
                    "symbol": f"{lib}:{symbol}",
                    "reference": "U1",
                    "position": {"x": sx, "y": sy},
                    "rotation": rotation,
                }
            ],
        }
    )
    assert r["success"], r
    located = PinLocator().get_all_symbol_pins(sch, "U1")
    expected = {}
    for num, x, y in _library_pins(lib_path, symbol):
        expected.setdefault(num, set()).add(_expected_world(x, y, sx, sy, rotation))
    assert set(located) == set(expected)
    for num, (px, py) in located.items():
        assert any(
            abs(px - ex) < 1e-6 and abs(py - ey) < 1e-6 for ex, ey in expected[num]
        ), f"{symbol} pin {num} at rot {rotation}: {px, py} not in {expected[num]}"


@pytest.mark.integration
def test_reported_symbols_have_their_kicad10_geometry(tmp_path):
    """The concrete #5e / #5p / SH cases, against KiCad 10's libraries.

    Runs whenever a KiCad 10 install is present — also when an older KiCad is
    installed beside it, which is exactly the setup that made the loader pick
    the KiCad 8 libraries.
    """
    from utils.kicad_roots import kicad_install_roots

    if not any(
        (root / "share" / "kicad" / "symbols" / "Jumper.kicad_sym").is_file()
        and _is_kicad10(root / "share" / "kicad" / "symbols" / "Jumper.kicad_sym")
        for root in kicad_install_roots()
    ):
        pytest.skip("no KiCad 10 install with stock symbol libraries")
    for lib in ("Power_Management", "Jumper", "Connector"):
        assert _is_kicad10(_stock_library(lib)), f"{lib} resolved to a non-KiCad-10 library"
    sch = _new_sheet(tmp_path)
    r = _batch().batch_add_components(
        {
            "schematicPath": str(sch),
            "components": [
                {"symbol": "Power_Management:TPS22917DBV", "reference": "U1",
                 "position": {"x": 101.6, "y": 101.6}},
                {"symbol": "Jumper:SolderJumper_2_Bridged", "reference": "JP1",
                 "position": {"x": 154.94, "y": 86.36}, "rotation": 90},
                {"symbol": "Connector:USB_C_Receptacle_USB2.0_16P", "reference": "J1",
                 "position": {"x": 50.8, "y": 152.4}},
            ],
        }
    )  # fmt: skip
    assert r["success"], r
    loc = PinLocator()
    u1 = loc.get_all_symbol_pins(sch, "U1")
    assert u1["4"][1] == pytest.approx(101.6)  # CT, library y 0
    assert u1["5"][1] == pytest.approx(101.6 + 2.54)  # QOD, library y -2.54
    jp = loc.get_all_symbol_pins(sch, "JP1")
    assert jp["1"] == pytest.approx([154.94, 91.44])
    assert jp["2"] == pytest.approx([154.94, 81.28])
    assert "SH" in loc.get_all_symbol_pins(sch, "J1")
    entry = _lib_symbols_entry(
        sch.read_text(encoding="utf-8"), "Connector:USB_C_Receptacle_USB2.0_16P"
    )
    assert '(number "S1"' not in entry


@pytest.mark.integration
@pytest.mark.parametrize("lib, symbol", _STOCK[3:] + [("Memory_Flash", "W25Q128JVS")])
def test_stock_symbols_new_in_kicad10_or_derived_resolve(lib, symbol):
    lib_path = _stock_library(lib)
    if not re.search(r'\(symbol "' + re.escape(symbol) + '"', lib_path.read_text(encoding="utf-8")):
        pytest.skip(f"{lib}:{symbol} not in installed library")
    block = DynamicSymbolLoader().extract_symbol_from_library(lib, symbol)
    assert block is not None
    assert "(extends" not in block
    assert re.search(r'\(symbol "' + re.escape(symbol) + r'_\d+_\d+"', block)


# --------------------------------------------------------------------------- #
# kicad-cli as the oracle: netlist binds every label to the intended pin, and  #
# ERC finds no lib_symbol_mismatch between the sheet and the library.          #
# --------------------------------------------------------------------------- #


def _kicad_cli():
    from utils.kicad_cli import resolve_kicad_cli

    cli = resolve_kicad_cli()
    if not cli:
        pytest.skip("kicad-cli not available")
    return cli


def _cli_major(cli: str) -> str:
    out = subprocess.run([cli, "version"], capture_output=True, text=True, timeout=60).stdout
    return out.strip().split(".")[0]


@pytest.mark.integration
def test_kicad_cli_netlist_and_erc_agree_with_placement(tmp_path):
    cli = _kicad_cli()
    libs = {lib for lib, _ in _STOCK}
    for lib in libs:
        lib_path = _stock_library(lib)
        head = lib_path.read_text(encoding="utf-8")[:300]
        m = re.search(r'\(generator_version "(\d+)\.', head)
        if not m or m.group(1) != _cli_major(cli):
            pytest.skip("kicad-cli and the resolved stock libraries are different KiCad versions")

    sch = _new_sheet(tmp_path)
    components = []
    expected = {}
    y = 50.8
    for i, (lib, symbol) in enumerate(_STOCK):
        rotation = (0, 90, 180, 270)[i % 4]
        ref = f"U{i + 1}"
        # Pins stacked on one spot (USB-C GND A1/A12/B1/B12) share a net by
        # construction, so label only pins whose library position is unique.
        lib_pins = _library_pins(_stock_library(lib), symbol)
        spots = {}
        for num, x, yy in lib_pins:
            spots.setdefault((x, yy), set()).add(num)
        numbers = sorted(next(iter(s)) for s in spots.values() if len(s) == 1)[:6]
        nets = {n: f"{ref}_{re.sub(r'[^A-Za-z0-9]', '_', n)}" for n in numbers}
        components.append(
            {
                "symbol": f"{lib}:{symbol}",
                "reference": ref,
                "position": {"x": 101.6 + (i % 2) * 152.4, "y": y},
                "rotation": rotation,
                "nets": nets,
            }
        )
        expected.update({(ref, n): net for n, net in nets.items()})
        y += 101.6
    r = _batch().batch_add_and_connect(
        {"schematicPath": str(sch), "components": components, "labelType": "global_label"}
    )
    assert r["added_count"] == len(_STOCK), r
    assert not r["failed_connections"], r

    net = tmp_path / "out.xml"
    subprocess.run(
        [cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(net), str(sch)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    actual = {}
    for n in ET.parse(net).getroot().findall(".//net"):
        for node in n.findall("node"):
            actual.setdefault((node.get("ref"), node.get("pin")), n.get("name", "").lstrip("/"))
    wrong = {k: (v, actual.get(k)) for k, v in expected.items() if actual.get(k) != v}
    assert not wrong, f"labels bound to the wrong pins (expected, got): {wrong}"

    erc = tmp_path / "erc.rpt"
    subprocess.run(
        [cli, "sch", "erc", "--severity-all", "-o", str(erc), str(sch)],
        capture_output=True,
        timeout=300,
    )
    report = erc.read_text(encoding="utf-8", errors="replace")
    assert "lib_symbol_mismatch" not in report and "lib_symbol_issues" not in report, report
