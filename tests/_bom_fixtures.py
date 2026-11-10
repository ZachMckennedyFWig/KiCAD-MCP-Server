"""Tiny two-sheet schematic project shared by the BOM, sourcing and snapshot tests.

Root ``proj.kicad_sch`` holds R1 and a sheet ``power.kicad_sch`` with R2, C1,
C2 (already carrying an ``LCSC Part #`` field), J1 and a GND power symbol.
"""

from __future__ import annotations

from pathlib import Path

ROOT_UUID = "11111111-1111-1111-1111-111111111111"
SHEET_UUID = "22222222-2222-2222-2222-222222222222"


def symbol(lib_id, ref, value, fp, x, uuid, extra="", flags=None, path=None):
    flags = flags or {}
    path = path or f"/{ROOT_UUID}/{SHEET_UUID}"
    flag_txt = "".join(
        f"\t\t({k} {'yes' if flags.get(k, d) else 'no'})\n"
        for k, d in (
            ("exclude_from_sim", False),
            ("in_bom", True),
            ("on_board", True),
            ("dnp", False),
        )
    )
    return (
        f'\t(symbol\n\t\t(lib_id "{lib_id}")\n\t\t(at {x} 50 0)\n\t\t(unit 1)\n{flag_txt}'
        f'\t\t(uuid "{uuid}")\n'
        f'\t\t(property "Reference" "{ref}"\n\t\t\t(at {x} 47 0)\n\t\t\t(effects\n'
        f"\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        f'\t\t(property "Value" "{value}"\n\t\t\t(at {x} 53 0)\n\t\t\t(effects\n'
        f"\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        f'\t\t(property "Footprint" "{fp}"\n\t\t\t(at {x} 50 0)\n\t\t\t(hide yes)\n'
        f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n"
        f"{extra}"
        f'\t\t(instances\n\t\t\t(project "proj"\n\t\t\t\t(path "{path}"\n'
        f'\t\t\t\t\t(reference "{ref}")\n\t\t\t\t\t(unit 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n'
    )


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj.kicad_sch"
    sub = tmp_path / "power.kicad_sch"
    (tmp_path / "proj.kicad_pro").write_text("{}", encoding="utf-8")
    root.write_text(
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n'
        f'\t(uuid "{ROOT_UUID}")\n\t(paper "A4")\n\t(lib_symbols\n\t)\n'
        + symbol(
            "Device:R",
            "R1",
            "10k",
            "Resistor_SMD:R_0603_1608Metric",
            20,
            "a1",
            path=f"/{ROOT_UUID}",
        )
        + "\t(sheet\n\t\t(at 50 50)\n\t\t(size 20 10)\n"
        f'\t\t(uuid "{SHEET_UUID}")\n'
        '\t\t(property "Sheetname" "Power"\n\t\t\t(at 50 49 0)\n\t\t)\n'
        '\t\t(property "Sheetfile" "power.kicad_sch"\n\t\t\t(at 50 61 0)\n\t\t)\n\t)\n'
        ")\n",
        encoding="utf-8",
    )
    sub.write_text(
        '(kicad_sch\n\t(version 20250114)\n\t(generator "eeschema")\n'
        '\t(uuid "33333333-3333-3333-3333-333333333333")\n\t(paper "A4")\n\t(lib_symbols\n\t)\n'
        + symbol("Device:R", "R2", "10k", "Resistor_SMD:R_0603_1608Metric", 10, "b1")
        + symbol("Device:C", "C1", "100nF", "Capacitor_SMD:C_0402_1005Metric", 30, "b2")
        + symbol(
            "Device:C",
            "C2",
            "100nF",
            "Capacitor_SMD:C_0603_1608Metric",
            40,
            "b3",
            extra=(
                '\t\t(property "LCSC Part #" "C1111"\n\t\t\t(at 40 50 0)\n\t\t\t(hide yes)\n'
                "\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n"
                "\t\t)\n"
            ),
        )
        + symbol("Connector:Conn_01x02", "J1", "PWR", "Connector:JST_XH_1x02", 50, "b4")
        + symbol("power:GND", "#PWR01", "GND", "", 60, "b5")
        + ")\n",
        encoding="utf-8",
    )
    return root
