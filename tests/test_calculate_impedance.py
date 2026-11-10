"""calculate_impedance and the stackup geometry it reads.

The unit tests write a small .kicad_pcb with a stackup section and give the
command a board whose only stackup access is that file, as get_stackup does.
The real-pcbnew test loads a copy of CD_Mainboard (skipped without
KICAD_USE_REAL_PCBNEW=1 or the test board).
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands.impedance import (  # noqa: E402
    DEFAULT_STACKUP_4_LAYER,
    GeometryError,
    ImpedanceCommands,
    layer_geometry,
    read_layer_geometry,
)

from utils import impedance as imp  # noqa: E402

_SIX_LAYER = """\
(kicad_pcb (version 20240108) (generator "pcbnew")
  (general
    (thickness 1.6)
  )
  (setup
    (stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (epsilon_r 3.3))
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "2116") (epsilon_r 4.2))
      (layer "In1.Cu" (type "copper") (thickness 0.0175))
      (layer "dielectric 2" (type "core") (thickness 0.2) (material "FR4") (epsilon_r 4.5))
      (layer "In2.Cu" (type "copper") (thickness 0.0175))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2) (material "FR4") (epsilon_r 4.3))
      (layer "In3.Cu" (type "copper") (thickness 0.0175))
      (layer "dielectric 4" (type "core") (thickness 0.6)
        (material "FR4") (epsilon_r 4.5)
        (thickness 0.4) (material "FR4") (epsilon_r 4.1))
      (layer "In4.Cu" (type "copper") (thickness 0.0175))
      (layer "dielectric 5" (type "prepreg") (thickness 0.1) (material "2116") (epsilon_r 4.2))
      (layer "B.Cu" (type "copper") (thickness 0.035))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (copper_finish "None")
    )
  )
)
"""

_NO_STACKUP = """\
(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (setup (pad_to_mask_clearance 0))
)
"""


class _Settings:
    m_TrackMinWidth = 100_000
    m_MinClearance = 100_000


class _Board:
    def __init__(self, path, copper=None):
        self._path = str(path)
        self._copper = copper

    def GetFileName(self):
        return self._path

    def GetCopperLayerCount(self):
        return self._copper

    def GetDesignSettings(self):
        return _Settings()


@pytest.fixture
def six_layer(tmp_path):
    path = tmp_path / "six.kicad_pcb"
    path.write_text(_SIX_LAYER, encoding="utf-8")
    return _Board(path, 6)


def _cmd(board):
    return ImpedanceCommands(lambda: board)


# --- geometry --------------------------------------------------------------------


@pytest.mark.unit
def test_outer_layer_is_microstrip_over_the_next_copper(six_layer):
    geo = read_layer_geometry(six_layer, "F.Cu")
    assert geo["structure"] == "microstrip"
    assert geo["heightMm"] == pytest.approx(0.1)
    assert geo["epsilonR"] == pytest.approx(4.2)
    assert geo["copperThicknessMm"] == pytest.approx(0.035)
    assert geo["referencePlanes"] == ["In1.Cu"]
    # get_stackup keeps only the thickness of a mask row
    assert geo["solderMask"] == {"thicknessMm": 0.01}
    assert geo["stackupSource"] == "board"


@pytest.mark.unit
def test_bottom_layer_looks_upwards(six_layer):
    geo = read_layer_geometry(six_layer, "B.Cu")
    assert geo["structure"] == "microstrip"
    assert geo["referencePlanes"] == ["In4.Cu"]
    assert geo["heightMm"] == pytest.approx(0.1)


@pytest.mark.unit
def test_inner_layer_is_stripline_between_neighbours(six_layer):
    geo = read_layer_geometry(six_layer, "In2.Cu")
    assert geo["structure"] == "stripline"
    assert geo["heightAboveMm"] == pytest.approx(0.2)
    assert geo["heightBelowMm"] == pytest.approx(0.2)
    # thickness-weighted: (0.2 * 4.5 + 0.2 * 4.3) / 0.4
    assert geo["epsilonR"] == pytest.approx(4.4)
    assert geo["referencePlanes"] == ["In1.Cu", "In3.Cu"]


@pytest.mark.unit
def test_dielectric_sublayers_are_weighted(six_layer):
    geo = read_layer_geometry(six_layer, "In4.Cu")
    assert geo["heightAboveMm"] == pytest.approx(1.0)  # 0.6 + 0.4 sublayers
    expected = (0.6 * 4.5 + 0.4 * 4.1 + 0.1 * 4.2) / 1.1
    assert geo["epsilonR"] == pytest.approx(expected)


@pytest.mark.unit
def test_reference_layers_skip_a_signal_neighbour(six_layer):
    geo = read_layer_geometry(six_layer, "In2.Cu", ["In1.Cu", "In4.Cu"])
    assert geo["structure"] == "stripline"
    assert geo["heightAboveMm"] == pytest.approx(0.2)
    # dielectric 3 + In3.Cu copper + dielectric 4
    assert geo["heightBelowMm"] == pytest.approx(0.2 + 0.0175 + 1.0)
    assert geo["referencePlanes"] == ["In1.Cu", "In4.Cu"]


@pytest.mark.unit
def test_one_sided_reference_on_an_inner_layer_is_embedded(six_layer):
    geo = read_layer_geometry(six_layer, "In1.Cu", ["In2.Cu"])
    assert geo["structure"] == "embedded_microstrip"
    assert geo["heightMm"] == pytest.approx(0.2)
    assert geo["coverMm"] == pytest.approx(0.1 + 0.035)


@pytest.mark.unit
def test_geometry_errors(six_layer):
    with pytest.raises(GeometryError, match="not in the stackup"):
        read_layer_geometry(six_layer, "In9.Cu")
    with pytest.raises(GeometryError, match="not a copper layer"):
        read_layer_geometry(six_layer, "F.Mask")
    with pytest.raises(GeometryError, match="Reference layers not in the stackup"):
        read_layer_geometry(six_layer, "F.Cu", ["X.Cu"])
    with pytest.raises(GeometryError, match="own reference"):
        read_layer_geometry(six_layer, "F.Cu", ["F.Cu"])


@pytest.mark.unit
def test_board_without_stackup_falls_back_to_default(tmp_path):
    path = tmp_path / "bare.kicad_pcb"
    path.write_text(_NO_STACKUP, encoding="utf-8")
    geo = read_layer_geometry(_Board(path, 4), "F.Cu")
    assert geo["stackupSource"] == "default"
    assert "JLC" in geo["stackupNote"]
    assert "no (stackup" in geo["stackupNote"]
    assert geo["heightMm"] == pytest.approx(0.2104)

    two = read_layer_geometry(_Board(path, 2), "F.Cu")
    assert two["heightMm"] == pytest.approx(1.51)
    assert "2-layer" in two["stackupNote"]

    with pytest.raises(GeometryError, match="no default for a 6-layer"):
        read_layer_geometry(_Board(path, 6), "F.Cu")


@pytest.mark.unit
def test_unsaved_board_uses_the_default(tmp_path):
    geo = read_layer_geometry(_Board(tmp_path / "missing.kicad_pcb"), "In1.Cu")
    assert geo["stackupSource"] == "default"
    assert geo["structure"] == "stripline"
    assert "not saved" in geo["stackupNote"]


@pytest.mark.unit
def test_default_stackup_rows_are_well_formed():
    geo = layer_geometry(DEFAULT_STACKUP_4_LAYER, "In2.Cu")
    assert geo["heightAboveMm"] == pytest.approx(1.065)
    assert geo["heightBelowMm"] == pytest.approx(0.2104)


# --- calculate_impedance -------------------------------------------------------------


@pytest.mark.unit
def test_single_ended_analysis(six_layer):
    result = _cmd(six_layer).calculate_impedance({"layer": "F.Cu", "width": 0.18})
    assert result["success"], result
    assert result["mode"] == "single"
    assert result["z0"] == pytest.approx(imp.microstrip(0.18, 0.1, 0.035, 4.2)["z0"], abs=0.01)
    assert result["model"] == imp.MICROSTRIP_MODEL
    assert result["geometry"]["structure"] == "microstrip"
    assert "accuracyNote" in result
    assert any("assumed to be solid reference planes" in n for n in result["notes"])


@pytest.mark.unit
def test_single_ended_solve(six_layer):
    result = _cmd(six_layer).calculate_impedance(
        {"layer": "In2.Cu", "mode": "single", "targetImpedance": 50}
    )
    assert result["success"], result
    assert result["solvedFor"] == "width"
    assert result["z0"] == pytest.approx(50.0, abs=0.02)
    width = result["widthMm"]
    assert imp.stripline(width, 0.2, 0.0175, 4.4, h2=0.2)["z0"] == pytest.approx(50, abs=0.05)


@pytest.mark.unit
def test_differential_analysis(six_layer):
    result = _cmd(six_layer).calculate_impedance({"layer": "F.Cu", "width": 0.12, "gap": 0.15})
    assert result["success"], result
    assert result["mode"] == "differential"
    ref = imp.coupled_microstrip(0.12, 0.15, 0.1, 0.035, 4.2)
    assert result["zDiff"] == pytest.approx(ref["zDiff"], abs=0.01)
    assert result["zOdd"] == pytest.approx(ref["zOdd"], abs=0.01)
    assert result["zEven"] == pytest.approx(ref["zEven"], abs=0.01)
    assert result["model"] == imp.COUPLED_MICROSTRIP_MODEL
    assert "solvedFor" not in result


@pytest.mark.unit
def test_differential_solves_width_with_fixed_gap(six_layer):
    result = _cmd(six_layer).calculate_impedance(
        {"layer": "In2.Cu", "targetDiffImpedance": 100, "gap": 0.2}
    )
    assert result["success"], result
    assert result["solvedFor"] == "width"
    assert result["gapMm"] == 0.2
    assert result["zDiff"] == pytest.approx(100.0, abs=0.05)


@pytest.mark.unit
def test_differential_solves_gap_with_fixed_width(six_layer):
    result = _cmd(six_layer).calculate_impedance(
        {"layer": "F.Cu", "targetDiffImpedance": 90, "width": 0.15}
    )
    assert result["success"], result
    assert result["solvedFor"] == "gap"
    assert result["widthMm"] == 0.15
    assert result["zDiff"] == pytest.approx(90.0, abs=0.05)


@pytest.mark.unit
def test_differential_default_gap(six_layer):
    result = _cmd(six_layer).calculate_impedance({"layer": "F.Cu", "targetDiffImpedance": 90})
    assert result["success"], result
    assert result["gapMm"] == 0.2


@pytest.mark.unit
def test_unreachable_target_reports_the_reachable_range(six_layer):
    result = _cmd(six_layer).calculate_impedance(
        {"layer": "F.Cu", "targetDiffImpedance": 200, "gap": 0.15, "minWidth": 0.1}
    )
    assert not result["success"]
    assert result["message"] == "Target impedance is out of reach"
    low, high = result["reachableOhms"]
    assert high < 200
    assert result["bounds"]["minWidth"] == 0.1


@pytest.mark.unit
def test_board_minimum_width_bounds_the_solver(six_layer):
    # 0.1 mm board minimum; a very high single-ended target would need narrower
    result = _cmd(six_layer).calculate_impedance(
        {"layer": "F.Cu", "mode": "single", "targetImpedance": 90}
    )
    assert not result["success"]
    assert result["bounds"]["minWidth"] == pytest.approx(0.1)


@pytest.mark.unit
@pytest.mark.parametrize(
    "params, message",
    [
        ({}, "layer is required"),
        ({"layer": "F.Cu", "mode": "quad"}, "Invalid mode"),
        ({"layer": "F.Cu", "width": -1}, "Invalid parameter"),
        ({"layer": "F.Cu", "width": "wide"}, "Invalid parameter"),
        ({"layer": "F.Cu"}, "width is required"),
        ({"layer": "F.Cu", "mode": "differential", "width": 0.2}, "width and gap are required"),
        ({"layer": "F.Cu", "mode": "single", "targetDiffImpedance": 90}, "needs mode"),
        ({"layer": "F.Cu", "mode": "differential", "targetImpedance": 50}, "targetDiffImpedance"),
        ({"layer": "F.Cu", "width": 0.2, "referenceLayers": "In1.Cu"}, "Invalid parameter"),
        ({"layer": "Nope.Cu", "width": 0.2}, "Cannot derive the layer geometry"),
    ],
)
def test_parameter_errors(six_layer, params, message):
    result = _cmd(six_layer).calculate_impedance(params)
    assert not result["success"]
    assert message in result["message"]


@pytest.mark.unit
def test_no_board():
    result = ImpedanceCommands(lambda: None).calculate_impedance({"layer": "F.Cu", "width": 0.2})
    assert not result["success"]
    assert result["message"] == "No board is loaded"


@pytest.mark.unit
def test_strong_offset_is_flagged(tmp_path):
    path = tmp_path / "bare.kicad_pcb"
    path.write_text(_NO_STACKUP, encoding="utf-8")
    result = _cmd(_Board(path, 4)).calculate_impedance(
        {"layer": "In1.Cu", "width": 0.15, "gap": 0.15}
    )
    assert result["success"], result
    assert result["geometry"]["stackupSource"] == "default"
    assert any("JLC" in n for n in result["notes"])
    assert any("offset-stripline" in n for n in result["notes"])


# --- real pcbnew on CD_Mainboard --------------------------------------------------------

_CD_MAINBOARD = Path(r"D:\KiCadMCPTests\CD_Mainboard")


@pytest.mark.integration
@pytest.mark.real_pcbnew
def test_cd_mainboard_stackup_and_usb_pair(tmp_path):
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")
    source = _CD_MAINBOARD / "CD_Mainboard.kicad_pcb"
    if not source.is_file():
        pytest.skip("CD_Mainboard test board not found")
    import pcbnew

    for suffix in (".kicad_pcb", ".kicad_pro"):
        src = _CD_MAINBOARD / f"CD_Mainboard{suffix}"
        if src.is_file():
            shutil.copy2(src, tmp_path / src.name)
    board = pcbnew.LoadBoard(str(tmp_path / "CD_Mainboard.kicad_pcb"))

    top = read_layer_geometry(board, "F.Cu")
    assert top["stackupSource"] == "board"
    assert top["structure"] == "microstrip"
    assert top["heightMm"] == pytest.approx(0.2104)
    assert top["epsilonR"] == pytest.approx(4.4)
    assert top["copperThicknessMm"] == pytest.approx(0.035)
    assert top["referencePlanes"] == ["In1.Cu"]

    inner = read_layer_geometry(board, "In2.Cu")
    assert inner["structure"] == "stripline"
    assert inner["heightAboveMm"] == pytest.approx(1.065)
    assert inner["heightBelowMm"] == pytest.approx(0.2104)
    assert inner["copperThicknessMm"] == pytest.approx(0.0152)

    cmd = ImpedanceCommands(lambda: board)
    usb = cmd.calculate_impedance({"layer": "F.Cu", "targetDiffImpedance": 90, "gap": 0.15})
    assert usb["success"], usb
    assert usb["zDiff"] == pytest.approx(90.0, abs=0.05)
    # Kirschning-Jansen on 0.2104 mm 7628: about 0.29 mm for a 0.15 mm gap
    assert 0.25 < usb["widthMm"] < 0.33
