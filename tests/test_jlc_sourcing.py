"""jlc_bom_report / jlc_find_alternative (MCP_ISSUES S5) against tiny catalogs.

Both catalog layouts the server meets are covered: the downloader's source
cache (yaqwsx ``source-db-v2``: ``jlc_components`` with base/expand,
``preferred`` and an ``attributes`` JSON column) and the server's own
``jlcpcb_parts.db`` (``components`` with Basic/Extended/Preferred and no
attributes, so attributes come from the description).
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from commands import jlc_sourcing as js  # noqa: E402
from commands.jlc_sourcing import (  # noqa: E402
    PartsDB,
    jlc_bom_report,
    jlc_find_alternative,
    parse_value,
)

pytestmark = pytest.mark.unit

RES = "Chip Resistor - Surface Mount"
MLCC = "Multilayer Ceramic Capacitors MLCC - SMD/SMT"
MCU = "Microcontrollers (MCU/MPU/SOC)"

# lcsc, subcategory, package, lib (base/expand), preferred, stock, price, attrs, description
PARTS = [
    (
        25804,
        RES,
        "0603",
        "base",
        0,
        1_000_000,
        "1-999:0.002,1000-:0.001",
        {"Resistance": "10kΩ", "Tolerance": "±1%", "Power(Watts)": "100mW"},
        "10kΩ ±1% 100mW 0603",
    ),
    (
        99001,
        RES,
        "0603",
        "expand",
        0,
        500,
        "1-:0.01",
        {"Resistance": "10kΩ", "Tolerance": "±0.1%", "Power(Watts)": "100mW"},
        "10kΩ ±0.1% 0603",
    ),
    (
        99002,
        RES,
        "0603",
        "expand",
        1,
        50_000,
        "1-:0.004",
        {"Resistance": "10kΩ", "Tolerance": "±1%", "Power(Watts)": "125mW"},
        "10kΩ ±1% 125mW 0603",
    ),
    (
        99003,
        RES,
        "0603",
        "base",
        0,
        900,
        "1-:0.002",
        {"Resistance": "4.7kΩ", "Tolerance": "±1%", "Power(Watts)": "100mW"},
        "4.7kΩ ±1% 0603",
    ),
    (
        99004,
        RES,
        "0603",
        "base",
        0,
        2_000,
        "1-:0.002",
        {"Resistance": "10kΩ", "Tolerance": "±5%", "Power(Watts)": "100mW"},
        "10kΩ ±5% 0603",
    ),
    (
        99010,
        MLCC,
        "0402",
        "expand",
        0,
        10,
        "1-:0.05",
        {
            "Capacitance": "100nF",
            "Voltage Rating": "16V",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
        },
        "100nF 16V X7R 0402",
    ),
    (
        99011,
        MLCC,
        "0402",
        "base",
        0,
        9_000_000,
        "1-:0.001",
        {
            "Capacitance": "100nF",
            "Voltage Rating": "50V",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
        },
        "100nF 50V X7R 0402",
    ),
    (
        99012,
        MLCC,
        "0402",
        "base",
        0,
        9_000_000,
        "1-:0.001",
        {
            "Capacitance": "100nF",
            "Voltage Rating": "25V",
            "Temperature Coefficient": "X5R",
            "Tolerance": "±10%",
        },
        "100nF 25V X5R 0402",
    ),
    (
        99013,
        MLCC,
        "0402",
        "base",
        0,
        9_000_000,
        "1-:0.001",
        {
            "Capacitance": "100nF",
            "Voltage Rating": "10V",
            "Temperature Coefficient": "X7R",
            "Tolerance": "±10%",
        },
        "100nF 10V X7R 0402",
    ),
    (99020, MCU, "QFN-80-EP(10x10)", "expand", 0, 3_000, "1-:1.2", {"CPU Core": "M33"}, "MCU"),
    (99021, MCU, "QFN-80-EP(10x10)", "base", 0, 5_000, "1-:0.9", {"CPU Core": "M0"}, "MCU"),
]
DELISTED = (
    99030,
    RES,
    "0603",
    "base",
    0,
    99,
    "1-:0.001",
    {"Resistance": "10kΩ", "Tolerance": "±1%", "Power(Watts)": "100mW"},
    "gone",
)


def make_v2_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE jlc_components (lcsc INTEGER PRIMARY KEY, present INTEGER, category TEXT,"
        " subcategory TEXT, mfr TEXT, package TEXT, manufacturer TEXT, library_type TEXT,"
        " preferred INTEGER, description TEXT, datasheet TEXT, stock INTEGER, price TEXT,"
        " attributes TEXT, assembly_process TEXT)"
    )
    for present, row in [(1, p) for p in PARTS] + [(0, DELISTED)]:
        lcsc, sub, pkg, lib, pref, stock, price, attrs, desc = row
        conn.execute(
            "INSERT INTO jlc_components VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lcsc,
                present,
                "cat",
                sub,
                f"MFR{lcsc}",
                pkg,
                "maker",
                lib,
                pref,
                desc,
                "",
                stock,
                price,
                json.dumps(attrs),
                "SMT",
            ),
        )
    conn.commit()
    conn.close()
    return path


def make_mcp_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE components (lcsc TEXT PRIMARY KEY, category TEXT, subcategory TEXT,"
        " mfr_part TEXT, package TEXT, solder_joints INTEGER, manufacturer TEXT,"
        " library_type TEXT, description TEXT, datasheet TEXT, stock INTEGER,"
        " price_json TEXT, last_updated INTEGER)"
    )
    for lcsc, sub, pkg, lib, pref, stock, price, attrs, desc in PARTS:
        lib_type = "Basic" if lib == "base" else ("Preferred" if pref else "Extended")
        first = float(price.split(":")[1].split(",")[0])
        conn.execute(
            "INSERT INTO components VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"C{lcsc}",
                "cat",
                sub,
                f"MFR{lcsc}",
                pkg,
                2,
                "maker",
                lib_type,
                desc,
                "",
                stock,
                json.dumps([{"qty": 1, "price": first}]),
                0,
            ),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture(params=["v2", "mcp"])
def db_path(request, tmp_path):
    if request.param == "v2":
        return str(make_v2_db(tmp_path / "cdfer.sqlite3"))
    return str(make_mcp_db(tmp_path / "jlcpcb_parts.db"))


def test_parse_value():
    assert parse_value("10kΩ") == pytest.approx(10_000)
    assert parse_value("4k7") == pytest.approx(4_700)
    assert parse_value("2R2") == pytest.approx(2.2)
    assert parse_value("100nF") == pytest.approx(100e-9)
    assert parse_value("4.7uH") == pytest.approx(4.7e-6)
    assert parse_value("±1%") == pytest.approx(1)
    assert parse_value("100mW") == pytest.approx(0.1)
    assert parse_value("1/10W") == pytest.approx(0.1)
    assert parse_value("600Ω@100MHz") == pytest.approx(600)
    assert parse_value("12MHz") == pytest.approx(12e6)
    assert parse_value("abc") is None


def test_db_is_read_only_and_closed(tmp_path):
    path = make_v2_db(tmp_path / "cdfer.sqlite3")
    with PartsDB.open(str(path)) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.conn.execute("DELETE FROM jlc_components")
        assert db.get("C25804")["libraryType"] == "basic"
        assert db.get("C99002")["libraryType"] == "preferred"
        assert db.get("C99030") is None  # delisted (present = 0)
    assert db.conn is None
    gc.collect()
    # No handle left behind: the file can be replaced (MCP_ISSUES #1).
    os.replace(path, tmp_path / "moved.sqlite3")


def test_open_prefers_attribute_cache_and_skips_empty(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "jlcparts_cache").mkdir(parents=True)
    # An empty server DB (the state the user's machine is in) must not be chosen.
    sqlite3.connect(data / "jlcpcb_parts.db").execute(
        "CREATE TABLE components (lcsc TEXT, subcategory TEXT, package TEXT, description TEXT,"
        " stock INTEGER, price_json TEXT, library_type TEXT, category TEXT)"
    ).connection.close()
    monkeypatch.setattr(js, "_data_dir", lambda: data)
    monkeypatch.delenv("KICAD_MCP_JLC_DB", raising=False)
    with pytest.raises(FileNotFoundError):
        PartsDB.open()
    make_v2_db(data / "jlcparts_cache" / "cdfer.sqlite3")
    with PartsDB.open() as db:
        assert db.kind == "source-db-v2"


def test_find_alternative_keeps_value_and_ranks_fee_free_first(db_path):
    r = jlc_find_alternative({"lcsc": "C99001", "dbPath": db_path})
    assert r["success"], r
    lcscs = [a["lcsc"] for a in r["alternatives"]]
    # 10k only; 4.7k excluded; ±5% fails the tolerance (<= ±0.1%) check.
    assert "C99003" not in lcscs and "C99004" not in lcscs
    assert "C25804" not in lcscs  # ±1% is worse than the reference's ±0.1%


def test_find_alternative_with_constraints(db_path):
    r = jlc_find_alternative(
        {
            "dbPath": db_path,
            "constraints": {
                "subcategory": RES,
                "package": "0603",
                "value": "10k",
                "tolerance": "1%",
                "noFeeOnly": True,
            },
        }
    )
    assert r["success"], r
    assert [a["lcsc"] for a in r["alternatives"]] == ["C25804", "C99002"]
    r = jlc_find_alternative(
        {
            "dbPath": db_path,
            "constraints": {
                "subcategory": RES,
                "package": "0603",
                "value": "4k7",
                "basicOnly": True,
            },
        }
    )
    assert [a["lcsc"] for a in r["alternatives"]] == ["C99003"]


def test_find_alternative_voltage_and_dielectric(db_path):
    r = jlc_find_alternative({"lcsc": "C99010", "dbPath": db_path})
    lcscs = [a["lcsc"] for a in r["alternatives"]]
    # >= 16 V and X7R or better: 50 V X7R yes; 25 V X5R (worse dielectric) no; 10 V no.
    assert lcscs == ["C99011"]
    r = jlc_find_alternative(
        {"lcsc": "C99010", "dbPath": db_path, "constraints": {"voltage": "25V"}}
    )
    assert {a["lcsc"] for a in r["alternatives"]} == {"C99011", "C99012"}


def test_find_alternative_unknown_part(db_path):
    r = jlc_find_alternative({"lcsc": "C123456789", "dbPath": db_path})
    assert not r["success"]
    assert "not in the parts database" in r["message"]


def test_bom_report_from_rows(db_path):
    bom = [
        {"lcsc": "C25804", "refs": "R1,R2,R3"},
        {"lcsc": "C99001", "refs": ["R4"]},
        {"lcsc": "C99010", "refs": ["C1", "C2"]},
        {"lcsc": "C99002", "refs": ["R5"]},
        {"lcsc": "C99020", "refs": ["U1"]},
        {"lcsc": "C424242", "refs": ["U2"]},
        {"refs": ["J1"], "value": "conn"},
    ]
    r = jlc_bom_report({"bom": bom, "boards": 10, "dbPath": db_path, "extendedFee": 3})
    assert r["success"], r
    t = r["totals"]
    assert t["lines"] == 6
    assert t["basicLines"] == 1 and t["preferredLines"] == 1
    assert t["extendedLines"] == 3
    assert t["loadingFee"] == 9.0
    assert t["notFound"] == 1
    assert t["stockShort"] == 1  # C99010: 20 needed, 10 in stock
    assert r["missingLcscCount"] == 1
    lines = {l["lcsc"]: l for l in r["lines"]}
    assert "C25804" not in lines  # summary shows only lines needing attention
    assert lines["C99010"]["status"] == "extended,stock"
    assert [a["lcsc"] for a in lines["C99010"]["alternatives"]] == ["C99011"]
    # No parametric class for MCUs: no drop-in suggested.
    assert lines["C99020"]["alternatives"] == []
    assert "no parametric class" in lines["C99020"]["notes"][0]
    assert t["potentialFeeSaving"] == 3.0
    full = jlc_bom_report({"bom": bom, "boards": 10, "dbPath": db_path, "detail": "full"})
    assert len(full["lines"]) == 6


def test_bom_report_custom_fee_and_price(db_path):
    r = jlc_bom_report(
        {
            "bom": [{"lcsc": "C25804", "qty": 1000}, {"lcsc": "C99001", "qty": 1}],
            "boards": 1,
            "extendedFee": 1.5,
            "dbPath": db_path,
            "detail": "full",
        }
    )
    t = r["totals"]
    assert t["loadingFee"] == 1.5
    line = {l["lcsc"]: l for l in r["lines"]}["C25804"]
    if "v2" in Path(db_path).name or Path(db_path).name.startswith("cdfer"):
        assert line["unitPrice"] == pytest.approx(0.001)  # 1000+ price break


def test_bom_report_from_schematic(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _bom_fixtures import make_project

    from commands.bom_tools import apply_property_map

    root = make_project(tmp_path)
    apply_property_map(
        {
            "schematicPath": str(root),
            "rules": [
                {"match": {"value": "10k"}, "set": {"LCSC": "C25804"}},
                {"match": {"value": "100nF"}, "set": {"LCSC": "C99010"}},
            ],
        }
    )
    db = make_v2_db(tmp_path / "cdfer.sqlite3")
    r = jlc_bom_report(
        {"schematicPath": str(root), "boards": 2, "dbPath": str(db), "exclude": ["J1"]}
    )
    assert r["success"], r
    assert r["totals"]["lines"] == 2
    assert r["totals"]["parts"] == 4
    assert r["excluded"] == ["J1"]
    assert r["missingLcscCount"] == 0
