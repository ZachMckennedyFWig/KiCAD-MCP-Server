"""Hand-built KiCad schematics for the connectivity tests.

Each helper returns s-expression text. Library symbols are embedded in
``lib_symbols`` so the files are self-contained (KiCad, kicad-cli and the
server's own parsers all read them without any library tables).

Pin geometry (library coordinates, Y up; schematic is Y down):

* ``test:IC``  — pin 1 "VDD" power_in at (-5.08, 0) pointing right (angle 0),
  pin 2 "EN" input at (5.08, 0) pointing left (angle 180).
  Placed at (X, Y): pin 1 at (X-5.08, Y), pin 2 at (X+5.08, Y).
* ``test:REG`` — pin 1 "VIN" power_in at (-5.08, 0), pin 2 "VOUT" power_out
  at (5.08, 0). Placed at (X, Y): pin 1 at (X-5.08, Y), pin 2 at (X+5.08, Y).
* ``power:PWR_FLAG`` — pin 1 power_out at (0, 0): the pin is at the symbol's
  position.
* ``power:+3V3`` — hidden power_in pin at (0, 0) named by the Value.
* ``Device:R`` — pins at (0, +/-3.81): placed at (X, Y) the pins are at
  (X, Y-3.81) and (X, Y+3.81).
"""

import uuid as _uuid
from pathlib import Path

LIB_IC = """
    (symbol "test:IC" (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (property "Value" "IC" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "IC_0_1"
        (rectangle (start -2.54 1.27) (end 2.54 -1.27) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "IC_1_1"
        (pin power_in line (at -5.08 0 0) (length 2.54) (name "VDD" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin input line (at 5.08 0 180) (length 2.54) (name "EN" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )"""

LIB_REG = """
    (symbol "test:REG" (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
      (property "Value" "REG" (at 0 -2.54 0) (effects (font (size 1.27 1.27))))
      (symbol "REG_0_1"
        (rectangle (start -2.54 1.27) (end 2.54 -1.27) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "REG_1_1"
        (pin power_in line (at -5.08 0 0) (length 2.54) (name "VIN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin power_out line (at 5.08 0 180) (length 2.54) (name "VOUT" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )"""

LIB_FLAG = """
    (symbol "power:PWR_FLAG" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)
      (property "Reference" "#FLG" (at 0 1.905 0) (effects (font (size 1.27 1.27)) hide))
      (property "Value" "PWR_FLAG" (at 0 3.81 0) (effects (font (size 1.27 1.27))))
      (symbol "PWR_FLAG_0_0"
        (pin power_out line (at 0 0 90) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
      (symbol "PWR_FLAG_0_1"
        (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
    )"""

LIB_3V3 = """
    (symbol "power:+3V3" (power) (pin_numbers hide) (pin_names (offset 0) hide) (in_bom yes) (on_board yes)
      (property "Reference" "#PWR" (at 0 -3.81 0) (effects (font (size 1.27 1.27)) hide))
      (property "Value" "+3V3" (at 0 3.556 0) (effects (font (size 1.27 1.27))))
      (symbol "+3V3_0_1"
        (polyline (pts (xy -0.762 1.27) (xy 0 2.54)) (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0 0) (xy 0 2.54)) (stroke (width 0) (type default)) (fill (type none)))
        (polyline (pts (xy 0 2.54) (xy 0.762 1.27)) (stroke (width 0) (type default)) (fill (type none)))
      )
      (symbol "+3V3_1_1"
        (pin power_in line (at 0 0 90) (length 0) hide (name "+3V3" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      )
    )"""

LIB_R = """
    (symbol "Device:R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 2.032 0 90) (effects (font (size 1.27 1.27))))
      (property "Value" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
      (symbol "R_0_1"
        (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none)))
      )
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
      )
    )"""

ALL_LIBS = LIB_IC + LIB_REG + LIB_FLAG + LIB_3V3 + LIB_R


def uid() -> str:
    return str(_uuid.uuid4())


def symbol(lib_id, ref, value, x, y, rot=0, ref_at=None, val_at=None, hide_fields=False):
    hide = " hide" if hide_fields else ""
    rx, ry = ref_at or (x, y - 5.08)
    vx, vy = val_at or (x, y + 5.08)
    return f"""
  (symbol (lib_id "{lib_id}") (at {x} {y} {rot}) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "{uid()}")
    (property "Reference" "{ref}" (at {rx} {ry} 0) (effects (font (size 1.27 1.27)){hide}))
    (property "Value" "{value}" (at {vx} {vy} 0) (effects (font (size 1.27 1.27)){hide}))
    (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) hide))
  )"""


def label(text, x, y, kind="label", angle=0):
    shape = "(shape bidirectional) " if kind != "label" else ""
    return (
        f'\n  ({kind} "{text}" {shape}(at {x} {y} {angle}) '
        f'(effects (font (size 1.27 1.27)) (justify left)) (uuid "{uid()}"))'
    )


def wire(x1, y1, x2, y2):
    return (
        f"\n  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) "
        f'(stroke (width 0) (type default)) (uuid "{uid()}"))'
    )


def sheet(name, filename, x, y, pins=(), w=20.32, h=10.16):
    pin_text = "".join(
        f'\n    (pin "{pname}" bidirectional (at {px} {py} 180) '
        f'(effects (font (size 1.27 1.27)) (justify left)) (uuid "{uid()}"))'
        for pname, px, py in pins
    )
    return f"""
  (sheet (at {x} {y}) (size {w} {h}) (fields_autoplaced yes)
    (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000))
    (uuid "{uid()}")
    (property "Sheetname" "{name}" (at {x} {y - 0.7} 0) (effects (font (size 1.27 1.27)) (justify left bottom)))
    (property "Sheetfile" "{filename}" (at {x} {y + h + 0.6} 0) (effects (font (size 1.27 1.27)) (justify left top))){pin_text}
  )"""


def schematic(body: str, libs: str = ALL_LIBS, root_uuid: str = None) -> str:
    return f"""(kicad_sch (version 20231120) (generator "eeschema") (generator_version "8.0")
  (uuid "{root_uuid or uid()}")
  (paper "A4")
  (lib_symbols{libs}
  ){body}
)
"""


def write_project(directory: Path, name: str, root_body: str, sheets=None) -> Path:
    """Write <name>.kicad_pro + <name>.kicad_sch (+ sub-sheets); return the root path."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.kicad_pro").write_text("{}", encoding="utf-8")
    root = directory / f"{name}.kicad_sch"
    root.write_text(schematic(root_body), encoding="utf-8")
    for filename, body in (sheets or {}).items():
        (directory / filename).write_text(schematic(body), encoding="utf-8")
    return root
