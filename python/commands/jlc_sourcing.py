"""Cost-aware JLCPCB sourcing: BOM fee/stock report and parametric alternatives (S5).

Two tools live here:

* ``jlc_bom_report`` -- per BOM line (grouped by LCSC number): basic /
  preferred / extended, stock against quantity x boards, the extended-part
  loading fee, unit price at the order quantity, and suggested basic or
  preferred parts in the same package and parametric class. Totals give the
  loading-fee estimate the user otherwise works out by hand.
* ``jlc_find_alternative`` -- parametric search (value, tolerance, voltage,
  package, stock, basic-only) instead of free-text ``LIKE``.

Both read whichever catalog is present, always through a read-only SQLite
connection that is closed before returning (MCP_ISSUES #1: an open handle is
what blocked the database swap on Windows):

* the server's own ``jlcpcb_parts.db`` (``components`` table, ``library_type``
  Basic/Extended/Preferred, ``price_json``; no attribute column, so attributes
  are parsed from the description);
* the downloader's source cache ``jlcparts_cache/cdfer.sqlite3`` -- either the
  yaqwsx ``source-db-v2`` layout (``jlc_components`` with ``library_type``
  base/expand, ``preferred``, ``attributes`` JSON) or CDFER's own
  ``v_components``/``components`` layout (``basic``/``preferred`` flags).

When both exist the one with attributes and the preferred flag (the cache) is
used unless ``dbPath`` names a file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("kicad_interface")

#: JLCPCB's per-unique-part loading fee for extended parts (economic assembly).
DEFAULT_EXTENDED_FEE = 3.0
DEFAULT_ALTERNATIVES = 3
_FEE_FREE = ("basic", "preferred")


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #

_SI = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}
_NUM_UNIT = re.compile(r"^\s*[±+]?\s*(\d+(?:\.\d+)?|\.\d+)\s*([pnuµμmkKMG]?)")
# "4k7", "2R2", "0R1", "4n7", "1M5": the multiplier letter stands for the point.
_INFIX = re.compile(r"^\s*(\d+)([pnuµμmkKMGR])(\d+)\s*([A-Za-zΩ]*)\s*$")


def parse_value(text: Any) -> Optional[float]:
    """Numeric value of ``10kΩ``, ``100nF``, ``4.7uH``, ``4k7``, ``±1%``, ``1/10W``.

    Ranges and conditions keep their first number (``600Ω@100MHz`` -> 600).
    Returns None when nothing numeric leads the string.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).strip().replace(",", "")
    if not s:
        return None
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*([pnuµμmkKMG]?)", s)
    if m and float(m.group(2)):
        return float(m.group(1)) / float(m.group(2)) * _SI[m.group(3)]
    m = _INFIX.match(s)
    if m:
        mult = 1.0 if m.group(2) == "R" else _SI[m.group(2)]
        return float(f"{m.group(1)}.{m.group(3)}") * mult
    m = _NUM_UNIT.match(s)
    if not m:
        return None
    unit_char = m.group(2)
    rest = s[m.end() :]
    # "100mW" is milli; "1M" is mega; but a bare "m" before "m"(eter)/"in" is
    # still milli, which is right for every attribute this module compares.
    value = float(m.group(1)) * _SI.get(unit_char, 1.0)
    if unit_char == "" and rest[:1] in ("R", "r") and not rest[1:2].isalpha():
        return float(m.group(1))
    return value


def _close(a: Optional[float], b: Optional[float], rel: float = 1e-3) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b))


# --------------------------------------------------------------------------- #
# Parametric classes
# --------------------------------------------------------------------------- #
#
# key: must be equal (the part's "value"); ge: candidate >= reference;
# le: candidate <= reference; same: string-equal (case-insensitive).

PARAM_CLASSES: List[Dict[str, Any]] = [
    {
        "name": "resistor",
        "match": r"chip resistor|resistor networks|resistors? array",
        "key": ["Resistance"],
        "ge": ["Power(Watts)"],
        "le": ["Tolerance"],
        "voltage": "Voltage-Supply(Max)",
    },
    {
        "name": "mlcc",
        "match": r"multilayer ceramic",
        "key": ["Capacitance"],
        "ge": ["Voltage Rating"],
        "le": ["Tolerance"],
        "dielectric": "Temperature Coefficient",
        "voltage": "Voltage Rating",
    },
    {
        "name": "capacitor",
        "match": r"capacitor",
        "key": ["Capacitance"],
        "ge": ["Voltage Rating"],
        "le": ["Tolerance"],
        "voltage": "Voltage Rating",
    },
    {
        "name": "ferrite",
        "match": r"ferrite",
        "key": ["Impedance @ Frequency"],
        "ge": ["Current Rating"],
        "le": ["DC Resistance(DCR)"],
    },
    {
        "name": "inductor",
        "match": r"inductor",
        "key": ["Inductance"],
        "ge": ["Current Rating", "Current - Saturation(Isat)"],
        "le": ["DC Resistance(DCR)"],
    },
    {"name": "led", "match": r"\bled\b", "same": ["Illumination Color"]},
    {
        "name": "crystal",
        "match": r"crystal",
        "key": ["Frequency"],
        "same": ["Load Capacitance"],
        "le": ["Frequency Stability"],
    },
    {
        "name": "diode",
        "match": r"schottky|diodes - general|fast recovery|switching diode|rectifier",
        "ge": ["Voltage - DC Reverse(Vr)", "Current - Rectified"],
        "voltage": "Voltage - DC Reverse(Vr)",
    },
    {
        "name": "tvs",
        "match": r"tvs|esd",
        "key": ["Reverse Stand-Off Voltage (Vrwm)"],
        "same": ["Polarity"],
        "le": ["Clamping Voltage"],
        "voltage": "Reverse Stand-Off Voltage (Vrwm)",
    },
]

# MLCC dielectrics: a candidate may use the same or a better class.
_DIELECTRIC_RANK = {"C0G": 3, "NP0": 3, "X8R": 2, "X7R": 2, "X7S": 2, "X6S": 1, "X5R": 1}

# Description fallbacks for a catalog without an attributes column.
_DESC_ATTRS = [
    ("Resistance", re.compile(r"(\d+(?:\.\d+)?\s*[mkKM]?)\s*Ω")),
    ("Capacitance", re.compile(r"(\d+(?:\.\d+)?\s*[pnuµμ]F)\b")),
    ("Inductance", re.compile(r"(\d+(?:\.\d+)?\s*[pnuµμm]H)\b")),
    ("Voltage Rating", re.compile(r"(\d+(?:\.\d+)?\s*[k]?V)\b")),
    ("Tolerance", re.compile(r"(±\s*\d+(?:\.\d+)?\s*%)")),
    ("Power(Watts)", re.compile(r"(\d+(?:\.\d+)?\s*m?W)\b")),
    ("Temperature Coefficient", re.compile(r"\b(C0G|NP0|X[5-8][RSTP])\b")),
]


def param_class(subcategory: str) -> Optional[Dict[str, Any]]:
    sub = (subcategory or "").lower()
    for cls in PARAM_CLASSES:
        if re.search(cls["match"], sub, re.I):
            return cls
    return None


def attrs_from_description(description: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, rx in _DESC_ATTRS:
        m = rx.search(description or "")
        if m:
            out[name] = m.group(1).replace(" ", "")
    return out


def _dielectric_ok(ref: str, cand: str) -> bool:
    r = _DIELECTRIC_RANK.get((ref or "").upper())
    c = _DIELECTRIC_RANK.get((cand or "").upper())
    if r is None or c is None:
        return (ref or "").upper() == (cand or "").upper()
    return c >= r


def compare_parametric(
    ref: Dict[str, Any], cand: Dict[str, Any], cls: Optional[Dict[str, Any]]
) -> Tuple[bool, List[str]]:
    """(compatible, notes) of *cand* as a drop-in for *ref* within *cls*."""
    if cls is None:
        return True, ["no parametric class for this subcategory: check the datasheet"]
    ra, ca = ref.get("attributes") or {}, cand.get("attributes") or {}
    notes: List[str] = []
    for name in cls.get("key", []):
        rv, cv = parse_value(ra.get(name)), parse_value(ca.get(name))
        if rv is None:
            notes.append(f"reference has no {name}")
            continue
        if not _close(rv, cv):
            return False, [f"{name} {ca.get(name)} != {ra.get(name)}"]
    for name in cls.get("ge", []):
        rv, cv = parse_value(ra.get(name)), parse_value(ca.get(name))
        if rv is None:
            continue
        if cv is None or cv < rv * (1 - 1e-6):
            return False, [f"{name} {ca.get(name)} < {ra.get(name)}"]
    for name in cls.get("le", []):
        rv, cv = parse_value(ra.get(name)), parse_value(ca.get(name))
        if rv is None:
            continue
        if cv is None or cv > rv * (1 + 1e-6):
            return False, [f"{name} {ca.get(name)} > {ra.get(name)}"]
    for name in cls.get("same", []):
        rv, cv = ra.get(name), ca.get(name)
        if rv and (cv or "").strip().lower() != rv.strip().lower():
            return False, [f"{name} {cv} != {rv}"]
    diel = cls.get("dielectric")
    if diel and ra.get(diel) and not _dielectric_ok(ra.get(diel), ca.get(diel)):
        return False, [f"{diel} {ca.get(diel)} worse than {ra.get(diel)}"]
    return True, notes


# --------------------------------------------------------------------------- #
# Catalog access
# --------------------------------------------------------------------------- #


def _data_dir() -> Path:
    from utils.platform_helper import PlatformHelper

    return PlatformHelper.get_data_dir()


def candidate_db_paths() -> List[Path]:
    """Catalog files to try, richest first."""
    data = _data_dir()
    env = os.environ.get("KICAD_MCP_JLC_DB")
    out = [Path(env)] if env else []
    out += [data / "jlcparts_cache" / "cdfer.sqlite3", data / "jlcpcb_parts.db"]
    return out


class PartsDB:
    """Read-only, schema-agnostic view of a JLC parts catalog."""

    def __init__(self, path: Path):
        self.path = Path(path)
        uri = "file:" + self.path.resolve().as_posix() + "?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        try:
            self._detect()
        except Exception:
            self.close()
            raise

    # -- lifecycle --------------------------------------------------------- #

    @classmethod
    def open(cls, db_path: Optional[str] = None) -> "PartsDB":
        """Open ``db_path`` or the best catalog present; raises FileNotFoundError."""
        if db_path:
            p = Path(db_path).expanduser()
            if not p.is_file():
                raise FileNotFoundError(f"JLC parts database not found: {p}")
            return cls(p)
        errors = []
        for p in candidate_db_paths():
            if not p.is_file() or p.stat().st_size == 0:
                continue
            try:
                db = cls(p)
            except Exception as exc:
                errors.append(f"{p}: {exc}")
                continue
            if db.count_hint() > 0:
                return db
            db.close()
            errors.append(f"{p}: empty")
        raise FileNotFoundError(
            "No usable JLCPCB parts database. Run download_jlcpcb_database first"
            + (f" ({'; '.join(errors)})" if errors else f" (looked in {_data_dir()})")
        )

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    def __enter__(self) -> "PartsDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- schema ------------------------------------------------------------ #

    def _columns(self, relation: str) -> List[str]:
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info([{relation}])")]

    def _detect(self) -> None:
        names = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if "jlc_components" in names:
            rel, kind = "jlc_components", "source-db-v2"
        elif "v_components" in names:
            rel, kind = "v_components", "cdfer"
        elif "components" in names:
            rel = "components"
            kind = "mcp" if "price_json" in self._columns("components") else "cdfer"
        else:
            raise ValueError("no components table (not a JLC parts database)")
        cols = set(self._columns(rel))
        self.relation, self.kind, self.cols = rel, kind, cols

        def pick(*names: str) -> Optional[str]:
            return next((n for n in names if n in cols), None)

        self.c_mfr = pick("mfr", "mfr_part") or "''"
        self.c_price = pick("price", "price_json") or "NULL"
        self.c_attrs = pick("attributes", "extra") or "NULL"
        self.c_assembly = pick("assembly_process") or "NULL"
        self.c_manufacturer = pick("manufacturer") or "''"
        self.c_datasheet = pick("datasheet") or "''"
        if kind == "source-db-v2":
            self.basic_sql = "library_type = 'base'"
            self.pref_sql = "preferred = 1"
        elif kind == "mcp":
            self.basic_sql = "library_type = 'Basic'"
            self.pref_sql = "library_type = 'Preferred'"
        else:
            self.basic_sql = "basic = 1" if "basic" in cols else "0"
            self.pref_sql = "preferred = 1" if "preferred" in cols else "0"
        self.present_sql = "present = 1" if "present" in cols else "1 = 1"
        row = self.conn.execute(f"SELECT typeof(lcsc) FROM [{rel}] LIMIT 1").fetchone()
        self.lcsc_int = bool(row and row[0] == "integer")

    def count_hint(self) -> int:
        row = self.conn.execute(f"SELECT 1 FROM [{self.relation}] LIMIT 1").fetchone()
        return 1 if row else 0

    def describe(self) -> Dict[str, Any]:
        return {"path": str(self.path), "kind": self.kind, "hasAttributes": self.c_attrs != "NULL"}

    # -- queries ----------------------------------------------------------- #

    def _select(self) -> str:
        return (
            f"SELECT lcsc, category, subcategory, {self.c_mfr} AS mfr, package, "
            f"{self.c_manufacturer} AS manufacturer, description, stock, "
            f"{self.c_price} AS price, {self.c_attrs} AS attrs, "
            f"{self.c_assembly} AS assembly, {self.c_datasheet} AS datasheet, "
            f"CASE WHEN ({self.basic_sql}) THEN 'basic' WHEN ({self.pref_sql}) THEN "
            f"'preferred' ELSE 'extended' END AS libtype FROM [{self.relation}]"
        )

    def _key(self, lcsc: str) -> Any:
        s = str(lcsc).strip().upper()
        if self.lcsc_int:
            digits = s[1:] if s.startswith("C") else s
            return int(digits) if digits.isdigit() else -1
        return s if s.startswith("C") else f"C{s}"

    def _row(self, row: sqlite3.Row) -> Dict[str, Any]:
        lcsc = row["lcsc"]
        lcsc = f"C{lcsc}" if isinstance(lcsc, int) else str(lcsc)
        attrs: Dict[str, str] = {}
        raw = row["attrs"]
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    data = data.get("attributes", data)
                    attrs = {
                        str(k): (v if isinstance(v, str) else json.dumps(v))
                        for k, v in data.items()
                    }
            except (ValueError, TypeError):
                attrs = {}
        if not attrs:
            attrs = attrs_from_description(row["description"] or "")
        return {
            "lcsc": lcsc,
            "mfr": row["mfr"] or "",
            "manufacturer": row["manufacturer"] or "",
            "package": row["package"] or "",
            "category": row["category"] or "",
            "subcategory": row["subcategory"] or "",
            "libraryType": row["libtype"],
            "stock": int(row["stock"] or 0),
            "prices": parse_prices(row["price"]),
            "description": row["description"] or "",
            "attributes": attrs,
            "assembly": row["assembly"] or None,
        }

    def get(self, lcsc: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"{self._select()} WHERE lcsc = ? AND ({self.present_sql}) LIMIT 1", (self._key(lcsc),)
        ).fetchone()
        return self._row(row) if row else None

    def get_many(self, lcscs: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for lcsc in lcscs:
            part = self.get(lcsc)
            if part:
                out[str(lcsc).strip().upper()] = part
        return out

    def query(
        self,
        subcategory: Optional[str] = None,
        package: Optional[str] = None,
        library: Optional[str] = None,
        min_stock: int = 0,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Filtered catalog rows. ``library``: None, ``"basic"`` or ``"nofee"``.

        Every OR group is parenthesised so it can never swallow the AND terms
        around it (the precedence trap MCP_ISSUES S5 mentions).
        """
        where = [f"({self.present_sql})"]
        args: List[Any] = []
        if subcategory:
            where.append("(subcategory = ?)")
            args.append(subcategory)
        if package:
            if any(ch in package for ch in "*?"):
                where.append("(package GLOB ?)")
            else:
                where.append("(package = ?)")
            args.append(package)
        if library == "basic":
            where.append(f"({self.basic_sql})")
        elif library == "nofee":
            where.append(f"(({self.basic_sql}) OR ({self.pref_sql}))")
        if min_stock > 0:
            where.append("(stock >= ?)")
            args.append(int(min_stock))
        sql = f"{self._select()} WHERE {' AND '.join(where)}"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [self._row(r) for r in self.conn.execute(sql, args)]


def parse_prices(raw: Any) -> List[Dict[str, float]]:
    from commands.jlcpcb_downloader import normalize_price_json

    try:
        if isinstance(raw, str) and raw.strip().startswith("[") and '"qty"' in raw:
            data = json.loads(raw)
        else:
            data = json.loads(normalize_price_json(raw))
    except (ValueError, TypeError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append({"qty": int(item.get("qty", 1) or 1), "price": float(item["price"])})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return sorted(out, key=lambda b: b["qty"])


def unit_price(prices: List[Dict[str, float]], qty: int) -> Optional[float]:
    best = None
    for brk in prices:
        if brk["qty"] <= max(qty, 1):
            best = brk["price"]
    if best is None and prices:
        best = prices[0]["price"]
    return best


# --------------------------------------------------------------------------- #
# Alternatives
# --------------------------------------------------------------------------- #


def _rank(part: Dict[str, Any], qty: int) -> Tuple[int, int, float]:
    lib = {"basic": 0, "preferred": 1}.get(part["libraryType"], 2)
    price = unit_price(part["prices"], qty)
    return (lib, -part["stock"], price if price is not None else math.inf)


def _compact(part: Dict[str, Any], qty: int, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    out = {
        "lcsc": part["lcsc"],
        "libraryType": part["libraryType"],
        "mfr": part["mfr"],
        "package": part["package"],
        "stock": part["stock"],
        "unitPrice": unit_price(part["prices"], qty),
        "attributes": _key_attrs(part),
    }
    if notes:
        out["notes"] = notes
    return out


def _key_attrs(part: Dict[str, Any]) -> Dict[str, str]:
    cls = param_class(part["subcategory"])
    attrs = part.get("attributes") or {}
    if not cls:
        return dict(list(attrs.items())[:4])
    names = []
    for k in ("key", "ge", "le", "same"):
        names += cls.get(k, [])
    if cls.get("dielectric"):
        names.append(cls["dielectric"])
    return {n: attrs[n] for n in names if n in attrs}


def suggest_from_pool(
    ref: Dict[str, Any], pool: List[Dict[str, Any]], qty: int, limit: int
) -> List[Dict[str, Any]]:
    """Fee-free parts from *pool* that can replace *ref* (same subcategory/package).

    Only for subcategories with a parametric class: "same package, same
    subcategory" is not a drop-in for an IC, so those get no suggestion here
    (``jlc_find_alternative`` still lists them, marked unverified).
    """
    cls = param_class(ref["subcategory"])
    if cls is None:
        return []
    found = []
    for cand in pool:
        if cand["lcsc"] == ref["lcsc"]:
            continue
        if cand["subcategory"] != ref["subcategory"] or cand["package"] != ref["package"]:
            continue
        if cand["stock"] < qty:
            continue
        ok, notes = compare_parametric(ref, cand, cls)
        if ok:
            found.append((cand, notes))
    found.sort(key=lambda cn: _rank(cn[0], qty))
    return [_compact(c, qty, n) for c, n in found[:limit]]


# --------------------------------------------------------------------------- #
# jlc_find_alternative
# --------------------------------------------------------------------------- #


def _constraint_filter(
    part: Dict[str, Any], cons: Dict[str, Any], cls: Optional[Dict[str, Any]]
) -> Tuple[bool, List[str]]:
    attrs = part.get("attributes") or {}
    notes: List[str] = []
    if cons.get("value") not in (None, ""):
        want = parse_value(cons["value"])
        keys = (cls or {}).get("key") or []
        if keys:
            have = parse_value(attrs.get(keys[0]))
            if want is None or not _close(want, have):
                return False, notes
        else:
            text = str(cons["value"]).lower()
            hay = " ".join([part["mfr"], part["description"], *attrs.values()]).lower()
            if text not in hay:
                return False, notes
    if cons.get("tolerance") not in (None, ""):
        limit = abs(parse_value(str(cons["tolerance"]).replace("%", "")) or 0)
        have = parse_value(str(attrs.get("Tolerance", "")).replace("%", ""))
        if have is None or abs(have) > limit + 1e-9:
            return False, notes
    if cons.get("voltage") not in (None, ""):
        need = parse_value(cons["voltage"])
        name = (cls or {}).get("voltage")
        names = [name] if name else [k for k in attrs if "voltage" in k.lower()]
        have = None
        for n in names:
            have = parse_value(attrs.get(n))
            if have is not None:
                break
        if need is None or have is None or have < need:
            return False, notes
    for name, val in (cons.get("attributes") or {}).items():
        have = attrs.get(name)
        if have is None:
            return False, notes
        pv, hv = parse_value(val), parse_value(have)
        if pv is not None and hv is not None:
            if not _close(pv, hv):
                return False, notes
        elif str(val).strip().lower() != str(have).strip().lower():
            return False, notes
    return True, notes


def jlc_find_alternative(params: Dict[str, Any]) -> Dict[str, Any]:
    """Parametric search for a replacement or a new part (S5)."""
    lcsc = (params.get("lcsc") or "").strip().upper() or None
    cons = params.get("constraints") or {}
    if not isinstance(cons, dict):
        return {"success": False, "message": "constraints must be an object"}
    try:
        limit = max(1, min(int(params.get("limit", cons.get("limit", 10)) or 10), 100))
    except (TypeError, ValueError):
        limit = 10
    qty = int(params.get("quantity", cons.get("quantity", 1)) or 1)
    try:
        db = PartsDB.open(params.get("dbPath"))
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        return {"success": False, "message": str(exc)}
    with db:
        ref = db.get(lcsc) if lcsc else None
        if lcsc and ref is None:
            return {
                "success": False,
                "message": f"{lcsc} is not in the parts database ({db.path})",
                "database": db.describe(),
            }
        subcategory = cons.get("subcategory") or (ref or {}).get("subcategory")
        package = cons.get("package") or (ref or {}).get("package")
        if not subcategory and not package:
            return {
                "success": False,
                "message": "Give lcsc, or constraints.subcategory / constraints.package",
            }
        library = "basic" if cons.get("basicOnly") else None
        if cons.get("noFeeOnly"):
            library = library or "nofee"
        min_stock = int(cons.get("minStock", 0) or 0)
        rows = db.query(subcategory, package, library, min_stock)
        cls = param_class(subcategory or "")
        explicit = any(
            cons.get(k) not in (None, "") for k in ("value", "tolerance", "voltage", "attributes")
        )
        matches = []
        for part in rows:
            if ref and part["lcsc"] == ref["lcsc"]:
                continue
            ok, notes = _constraint_filter(part, cons, cls)
            if not ok:
                continue
            if ref is not None and not explicit:
                ok, notes = compare_parametric(ref, part, cls)
                if not ok:
                    continue
            elif ref is not None and cons.get("value") in (None, ""):
                # Value not overridden: keep the reference's value.
                for name in (cls or {}).get("key", []):
                    if not _close(
                        parse_value((ref["attributes"] or {}).get(name)),
                        parse_value(part["attributes"].get(name)),
                    ):
                        ok = False
                if not ok:
                    continue
            matches.append((part, notes))
        matches.sort(key=lambda pn: _rank(pn[0], qty))
        result = {
            "success": True,
            "message": (
                f"{len(matches)} match(es) in {subcategory or 'any subcategory'}"
                f" / {package or 'any package'}"
                + (f"; showing {min(limit, len(matches))}" if len(matches) > limit else "")
            ),
            "database": db.describe(),
            "filters": {
                "subcategory": subcategory,
                "package": package,
                "library": library or "any",
                "minStock": min_stock,
                "parametricClass": cls["name"] if cls else None,
            },
            "count": len(matches),
            "alternatives": [_compact(p, qty, n) for p, n in matches[:limit]],
        }
        if ref is not None:
            result["reference"] = _compact(ref, qty)
            result["reference"]["subcategory"] = ref["subcategory"]
        return result


# --------------------------------------------------------------------------- #
# jlc_bom_report
# --------------------------------------------------------------------------- #


def _split_refs(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(r).strip() for r in raw if str(r).strip()]
    if isinstance(raw, str):
        return [r.strip() for r in re.split(r"[,\s;]+", raw) if r.strip()]
    return []


def bom_lines_from_rows(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], List]:
    """Group caller-supplied rows ``{lcsc, refs?, qty?, value?, footprint?}`` by LCSC."""
    lines: Dict[str, Dict[str, Any]] = {}
    missing = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"bom[{i}] must be an object")
        lcsc = str(row.get("lcsc") or row.get("LCSC") or row.get("LCSC Part #") or "").strip()
        refs = _split_refs(row.get("refs") or row.get("designator") or row.get("Designator"))
        qty = row.get("qty") or row.get("quantity") or len(refs) or 1
        if not lcsc:
            missing.append({"refs": refs, "value": row.get("value", "")})
            continue
        key = lcsc.upper()
        line = lines.setdefault(
            key,
            {
                "lcsc": key,
                "refs": [],
                "qtyPerBoard": 0,
                "value": row.get("value") or row.get("comment") or "",
                "footprint": row.get("footprint") or "",
            },
        )
        line["refs"].extend(refs)
        line["qtyPerBoard"] += int(qty)
    return lines, missing


def bom_lines_from_parts(
    parts: List[Dict[str, Any]], exclude: Iterable[str] = ()
) -> Tuple[Dict[str, Dict[str, Any]], List, List]:
    """Group schematic parts (``bom_tools.collect_parts``) by LCSC.

    DNP / not-in-BOM parts are skipped; ``exclude`` refs (hand-soldered) are
    returned separately.
    """
    from commands.bom_tools import natural_key

    excluded = set(exclude)
    lines: Dict[str, Dict[str, Any]] = {}
    missing, skipped = [], []
    for part in parts:
        if part["dnp"] or not part["in_bom"]:
            continue
        if part["ref"] in excluded:
            skipped.append(part)
            continue
        lcsc = (part.get("lcsc") or "").strip().upper()
        if not lcsc or lcsc == "DNP":
            if not lcsc:
                missing.append(
                    {"ref": part["ref"], "value": part["value"], "footprint": part["footprint"]}
                )
            continue
        line = lines.setdefault(
            lcsc,
            {
                "lcsc": lcsc,
                "refs": [],
                "qtyPerBoard": 0,
                "value": part["value"],
                "footprint": part["footprint"].split(":")[-1],
            },
        )
        line["refs"].append(part["ref"])
        line["qtyPerBoard"] += 1
    for line in lines.values():
        line["refs"].sort(key=natural_key)
    return lines, missing, skipped


def build_report(
    lines: Dict[str, Dict[str, Any]],
    boards: int,
    fee: float,
    n_alternatives: int,
    db: PartsDB,
    detail: str = "summary",
) -> Dict[str, Any]:
    """Annotate BOM lines with catalog data and compute totals."""
    pool = db.query(library="nofee", min_stock=1) if n_alternatives > 0 else []
    out_lines = []
    fee_total = 0.0
    parts_cost = 0.0
    priced_all = True
    extended_lines = stock_short = not_found = with_alt = 0
    savings = 0.0
    for lcsc, line in lines.items():
        qty_total = line["qtyPerBoard"] * boards
        part = db.get(lcsc)
        entry: Dict[str, Any] = {
            "lcsc": lcsc,
            "refs": line["refs"],
            "qtyPerBoard": line["qtyPerBoard"],
            "qtyTotal": qty_total,
            "value": line.get("value", ""),
            "footprint": line.get("footprint", ""),
        }
        if part is None:
            not_found += 1
            priced_all = False
            entry.update(
                libraryType=None,
                fee=None,
                status="not_found",
                notes=["not in the parts database (discontinued, or the catalog is stale)"],
            )
            out_lines.append(entry)
            continue
        lib = part["libraryType"]
        line_fee = fee if lib == "extended" else 0.0
        price = unit_price(part["prices"], qty_total)
        cost = price * qty_total if price is not None else None
        if cost is None:
            priced_all = False
        else:
            parts_cost += cost
        fee_total += line_fee
        stock_ok = part["stock"] >= qty_total
        entry.update(
            libraryType=lib,
            fee=line_fee,
            stock=part["stock"],
            stockOk=stock_ok,
            unitPrice=price,
            lineCost=round(cost, 4) if cost is not None else None,
            mfr=part["mfr"],
            package=part["package"],
            subcategory=part["subcategory"],
        )
        if part.get("assembly"):
            entry["assembly"] = part["assembly"]
        issues = []
        if lib == "extended":
            extended_lines += 1
            issues.append("extended")
        if not stock_ok:
            stock_short += 1
            issues.append("stock")
        if part.get("assembly") == "THT":
            issues.append("tht")
        if lib == "extended" and n_alternatives > 0:
            alts = suggest_from_pool(part, pool, qty_total, n_alternatives)
            if alts:
                with_alt += 1
                savings += line_fee
            entry["alternatives"] = alts
            if param_class(part["subcategory"]) is None:
                entry["notes"] = [
                    "no parametric class for this subcategory, so no drop-in is suggested; "
                    "jlc_find_alternative lists same-package candidates to check by hand"
                ]
        elif not stock_ok and n_alternatives > 0:
            entry["alternatives"] = suggest_from_pool(part, pool, qty_total, n_alternatives)
        entry["status"] = ",".join(issues) if issues else "ok"
        out_lines.append(entry)

    totals = {
        "lines": len(lines),
        "parts": sum(l["qtyPerBoard"] for l in lines.values()),
        "boards": boards,
        "basicLines": sum(1 for l in out_lines if l.get("libraryType") == "basic"),
        "preferredLines": sum(1 for l in out_lines if l.get("libraryType") == "preferred"),
        "extendedLines": extended_lines,
        "extendedFeeEach": fee,
        "loadingFee": round(fee_total, 2),
        "partsCost": round(parts_cost, 2),
        "partsCostComplete": priced_all,
        "stockShort": stock_short,
        "notFound": not_found,
        "extendedWithFeeFreeAlternative": with_alt,
        "potentialFeeSaving": round(savings, 2),
    }
    if detail == "summary":
        shown = [l for l in out_lines if l.get("status") != "ok"]
    else:
        shown = out_lines
    return {"totals": totals, "lines": shown, "linesShown": len(shown)}


def jlc_bom_report(params: Dict[str, Any]) -> Dict[str, Any]:
    """BOM fee/stock report against the JLC catalog (S5)."""
    from commands.bom_tools import collect_parts, resolve_root_schematic

    try:
        boards = max(1, int(params.get("boards", 1) or 1))
        fee = float(params.get("extendedFee", DEFAULT_EXTENDED_FEE))
        n_alt = max(0, min(int(params.get("alternatives", DEFAULT_ALTERNATIVES)), 10))
    except (TypeError, ValueError):
        return {"success": False, "message": "boards, extendedFee and alternatives must be numbers"}
    detail = params.get("detail", "summary")
    if detail not in ("summary", "full"):
        return {"success": False, "message": "detail must be 'summary' or 'full'"}
    exclude = _split_refs(params.get("exclude") or [])
    bom = params.get("bom")
    schematic_path = params.get("schematicPath")
    missing: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    source: Dict[str, Any]
    try:
        if bom is not None:
            if not isinstance(bom, list):
                return {"success": False, "message": "bom must be a list of rows"}
            lines, missing = bom_lines_from_rows(bom)
            source = {"bomRows": len(bom)}
        elif schematic_path:
            root = resolve_root_schematic(str(schematic_path))
            parts = collect_parts(root)
            lines, missing, skipped = bom_lines_from_parts(parts, exclude)
            source = {"rootSchematic": str(root), "partsInDesign": len(parts)}
        else:
            return {"success": False, "message": "Give schematicPath or bom"}
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {"success": False, "message": str(exc)}
    try:
        db = PartsDB.open(params.get("dbPath"))
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        return {"success": False, "message": str(exc)}
    with db:
        report = build_report(lines, boards, fee, n_alt, db, detail)
        database = db.describe()
    t = report["totals"]
    message = (
        f"{t['lines']} BOM line(s), {t['parts']} part(s)/board x {boards}: "
        f"{t['basicLines']} basic, {t['preferredLines']} preferred, {t['extendedLines']} "
        f"extended -> loading fee ${t['loadingFee']:.2f}"
        + (f" (save up to ${t['potentialFeeSaving']:.2f})" if t["potentialFeeSaving"] else "")
        + f"; parts ~${t['partsCost']:.2f}"
        + (f"; {t['stockShort']} short on stock" if t["stockShort"] else "")
        + (f"; {t['notFound']} not in catalog" if t["notFound"] else "")
        + (f"; {len(missing)} without LCSC" if missing else "")
    )
    out = {
        "success": True,
        "message": message,
        "database": database,
        "source": source,
        "detail": detail,
        **report,
        "missingLcscCount": len(missing),
        "missingLcsc": missing[:100],
        "notes": [
            "Loading fee assumes JLCPCB economic assembly: basic and preferred parts carry no "
            f"fee, each unique extended part costs ${fee:.2f}. Attrition and assembly labour "
            "are not included.",
        ],
    }
    if skipped:
        out["excluded"] = sorted({p["ref"] for p in skipped})
    return out
