"""Closed-form impedance formulas in utils/impedance.py against reference values.

Reference sources:

* Pozar, "Microwave Engineering", 4th ed.: Example 3.5 (50 ohm stripline,
  b = 0.32 cm, er = 2.2, W = 0.266 cm) and Example 3.7 (50 ohm microstrip,
  d = 0.127 cm, er = 2.2, W/d = 3.081, eeff = 1.87).
* Cohn's exact zero-thickness stripline, K(k)/K(k') with k = sech(pi w / 2b),
  is computed independently here with a numerically integrated K.
* KiCad's own coupled-stripline QA (qa/tests/pcb_calculator,
  test_coupled_stripline_offset.cpp): b = 20 mil, w = s = 8 mil, er = 4.3;
  zero-thickness offset a = 6 mil gives Z0e 53.98 / Z0o 45.10 (Shelton
  admittance sum over Cohn), and the centred line with t = 0.7 mil gives
  Z0e 55.34 / Z0o 43.59 / Zdiff 87.18.
* FIELD: a 2D non-uniform-grid finite-volume Laplace solver, run during
  development with Richardson extrapolation (it reproduces Cohn's exact
  stripline to 0.01 %). Values are quoted to 0.01 ohm; the tolerances are the
  accuracy each closed form is documented to have.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from utils import impedance as imp  # noqa: E402

pytestmark = pytest.mark.unit

MIL = 0.0254


def _pct(value: float, reference: float) -> float:
    return 100.0 * abs(value - reference) / reference


# --- elliptic integral --------------------------------------------------------


def _k_numeric(k: float, n: int = 20000) -> float:
    # Midpoint rule on K(k) = int_0^{pi/2} dt / sqrt(1 - k^2 sin^2 t)
    step = (math.pi / 2) / n
    return sum(step / math.sqrt(1.0 - (k * math.sin((i + 0.5) * step)) ** 2) for i in range(n))


@pytest.mark.parametrize("k", [0.0, 0.3, 0.7, 0.95])
def test_ellipk_matches_numeric_integral(k):
    assert imp.ellipk(k) == pytest.approx(_k_numeric(k), rel=1e-6)


# --- microstrip ----------------------------------------------------------------


def test_pozar_example_3_7_microstrip():
    """Pozar Ex. 3.7: W/d = 3.081 on er 2.2 is a 50 ohm line with eeff 1.87."""
    res = imp.microstrip(3.081 * 1.27, 1.27, 0.0, 2.2)
    assert res["z0"] == pytest.approx(50.0, rel=0.01)
    assert res["epsilonEff"] == pytest.approx(1.87, rel=0.01)


@pytest.mark.parametrize(
    "w, h, t, er, field",
    [
        (0.35, 0.2104, 0.035, 4.4, 51.71),
        (0.2, 0.1, 0.035, 4.2, 46.20),
        (1.0, 0.5, 0.035, 4.5, 47.12),
        (2.9, 1.51, 0.035, 4.5, 48.92),
        (0.3, 0.2, 0.0, 4.4, 57.54),
    ],
)
def test_microstrip_against_field_solver(w, h, t, er, field):
    """Hammerstad-Jensen with thickness correction: within 0.5 % (measured 0.1 %)."""
    assert _pct(imp.microstrip(w, h, t, er)["z0"], field) < 0.5


def test_microstrip_thicker_copper_lowers_impedance():
    thin = imp.microstrip(0.3, 0.2, 0.0, 4.4)["z0"]
    thick = imp.microstrip(0.3, 0.2, 0.07, 4.4)["z0"]
    assert thick < thin


@pytest.mark.parametrize("cover, field", [(0.1, 46.91), (0.3, 45.18)])
def test_embedded_microstrip_against_field_solver(cover, field):
    res = imp.embedded_microstrip(0.35, 0.2104, 0.035, 4.4, cover)
    assert _pct(res["z0"], field) < 3.0
    assert res["z0"] < imp.microstrip(0.35, 0.2104, 0.035, 4.4)["z0"]


# --- stripline -----------------------------------------------------------------


def test_pozar_example_3_5_stripline():
    """Pozar Ex. 3.5: W = 0.266 cm in b = 0.32 cm, er 2.2, is 50 ohm."""
    res = imp.stripline(2.66, 1.6, 0.0, 2.2)
    assert res["z0"] == pytest.approx(50.0, rel=0.01)


@pytest.mark.parametrize("w_over_b", [0.1, 0.3, 0.83, 1.0, 3.0])
def test_wheeler_matches_cohn_exact_at_zero_thickness(w_over_b):
    b = 1.0
    k = 1.0 / math.cosh(math.pi * w_over_b / 2.0)
    exact = (imp.ETA0 / 4.0) * _k_numeric(k) / _k_numeric(math.sqrt(1 - k * k))
    assert imp.stripline_exact_zero_thickness(w_over_b, b, 1.0) == pytest.approx(exact, rel=1e-5)
    assert _pct(imp.stripline(w_over_b, b / 2, 0.0, 1.0)["z0"], exact) < 0.5


@pytest.mark.parametrize(
    "w, h1, h2, t, er, field, tolerance",
    [
        (0.15, 0.2, 0.2, 0.0175, 4.3, 51.76, 1.0),
        (0.2, 0.3, 0.3, 0.035, 4.3, 53.34, 1.0),
        (0.1, 0.1, 0.15, 0.0175, 4.2, 48.01, 1.5),
        # 5:1 offset: the admittance sum reads high, documented as up to ~7 %
        (0.127, 0.2, 1.0, 0.0175, 4.5, 65.43, 8.0),
    ],
)
def test_stripline_against_field_solver(w, h1, h2, t, er, field, tolerance):
    assert _pct(imp.stripline(w, h1, t, er, h2=h2)["z0"], field) < tolerance


def test_offset_stripline_is_symmetric_in_the_two_heights():
    a = imp.stripline(0.15, 0.2, 0.0175, 4.3, h2=0.5)["z0"]
    b = imp.stripline(0.15, 0.5, 0.0175, 4.3, h2=0.2)["z0"]
    assert a == pytest.approx(b)


# --- coupled stripline -----------------------------------------------------------


def test_kicad_qa_offset_coupled_stripline_zero_thickness():
    """KiCad QA: a = 6 mil in b = 20 mil, zero thickness -> 53.98 / 45.10."""
    res = imp.coupled_stripline(8 * MIL, 8 * MIL, 6 * MIL, 0.0, 4.3, h2=14 * MIL)
    assert res["zEven"] == pytest.approx(53.98, abs=0.05)
    assert res["zOdd"] == pytest.approx(45.10, abs=0.05)


def test_kicad_qa_centred_coupled_stripline_with_thickness():
    """KiCad QA centred: 55.34 / 43.59 / 87.18; both are Cohn-based, so within 3 %."""
    h = (20 - 0.7) / 2 * MIL
    res = imp.coupled_stripline(8 * MIL, 8 * MIL, h, 0.7 * MIL, 4.3)
    assert _pct(res["zEven"], 55.34) < 3.0
    assert _pct(res["zOdd"], 43.59) < 3.0
    assert _pct(res["zDiff"], 87.18) < 3.0


@pytest.mark.parametrize(
    "w, s, h1, h2, t, er, field_odd, field_even, tol_odd",
    [
        (8 * MIL, 8 * MIL, 9.65 * MIL, 9.65 * MIL, 0.7 * MIL, 4.3, 43.85, 55.53, 3.0),
        (0.15, 0.15, 0.2, 0.2, 0.0175, 4.3, 44.08, 58.67, 3.0),
        (0.1, 0.2, 0.15, 0.15, 0.035, 4.0, 47.49, 55.58, 3.0),
        (8 * MIL, 8 * MIL, 5.65 * MIL, 13.65 * MIL, 0.7 * MIL, 4.3, 40.15, 48.38, 3.0),
        (0.127, 0.15, 0.2, 1.0, 0.0175, 4.5, 48.47, 81.18, 7.0),
    ],
)
def test_coupled_stripline_against_field_solver(
    w, s, h1, h2, t, er, field_odd, field_even, tol_odd
):
    res = imp.coupled_stripline(w, s, h1, t, er, h2=h2)
    assert _pct(res["zOdd"], field_odd) < tol_odd
    assert _pct(res["zEven"], field_even) < 5.0
    assert res["zDiff"] == pytest.approx(2 * res["zOdd"])


def test_coupled_stripline_decouples_at_wide_gap():
    single = imp.stripline(0.15, 0.2, 0.0175, 4.3)["z0"]
    res = imp.coupled_stripline(0.15, 5.0, 0.2, 0.0175, 4.3)
    assert res["zOdd"] == pytest.approx(single, rel=0.005)
    assert res["zEven"] == pytest.approx(single, rel=0.005)


# --- coupled microstrip ------------------------------------------------------------


@pytest.mark.parametrize(
    "w, s, h, t, er, field_odd, field_even",
    [
        (0.2, 0.15, 0.2104, 0.035, 4.4, 51.65, 81.95),
        (0.15, 0.2, 0.2104, 0.035, 4.4, 61.39, 89.96),
        (0.127, 0.127, 0.1, 0.035, 4.2, 49.17, 66.23),
        (0.5, 0.5, 1.5, 0.03, 4.5, 67.64, 142.80),
        (0.1, 0.1, 0.09, 0.035, 4.1, 50.27, 72.17),
    ],
)
def test_coupled_microstrip_against_field_solver(w, s, h, t, er, field_odd, field_even):
    """Kirschning-Jansen: Zodd (Zdiff) within 3 % (measured 1.6 %); Zeven reads
    3-9 % low with thick copper, documented."""
    res = imp.coupled_microstrip(w, s, h, t, er)
    assert _pct(res["zOdd"], field_odd) < 3.0
    assert _pct(res["zEven"], field_even) < 10.0
    assert res["zEven"] > res["zOdd"]


def test_coupled_microstrip_zero_thickness_is_kirschning_jansen_accurate():
    """KJ quotes ~1 % at zero thickness; field solver 52.46 / 62.33."""
    res = imp.coupled_microstrip(0.3, 0.3, 0.2, 0.0, 4.4)
    assert _pct(res["zOdd"], 52.46) < 1.0
    assert _pct(res["zEven"], 62.33) < 1.0


def test_coupled_microstrip_decouples_at_wide_gap():
    single = imp.microstrip(0.3, 0.2, 0.0, 4.4)["z0"]
    res = imp.coupled_microstrip(0.3, 6.0, 0.2, 0.0, 4.4)
    assert res["zOdd"] == pytest.approx(single, rel=0.01)
    assert res["zEven"] == pytest.approx(single, rel=0.01)


def test_coupled_results_are_consistent():
    res = imp.coupled_microstrip(0.2, 0.15, 0.2, 0.035, 4.4)
    assert res["zDiff"] == pytest.approx(2 * res["zOdd"])
    assert res["zCommon"] == pytest.approx(res["zEven"] / 2)
    assert res["z0"] == pytest.approx(math.sqrt(res["zEven"] * res["zOdd"]))
    assert 0 < res["coupling"] < 1


# --- monotonicity and solvers ------------------------------------------------------


def test_impedance_falls_with_width_and_rises_with_gap():
    widths = [0.08, 0.1, 0.15, 0.2, 0.3, 0.5]
    zd = [imp.coupled_microstrip(w, 0.15, 0.2, 0.035, 4.4)["zDiff"] for w in widths]
    assert zd == sorted(zd, reverse=True)
    gaps = [0.08, 0.1, 0.15, 0.2, 0.3, 0.6]
    zd = [imp.coupled_stripline(0.15, g, 0.2, 0.0175, 4.3)["zDiff"] for g in gaps]
    assert zd == sorted(zd)


def test_solve_width_round_trip():
    width = imp.solve_width(lambda w: imp.microstrip(w, 0.2104, 0.035, 4.4)["z0"], 50.0)
    assert imp.microstrip(width, 0.2104, 0.035, 4.4)["z0"] == pytest.approx(50.0, abs=0.01)


def test_solve_width_for_a_differential_target():
    width = imp.solve_width(
        lambda w: imp.coupled_microstrip(w, 0.15, 0.2104, 0.035, 4.4)["zDiff"], 90.0
    )
    res = imp.coupled_microstrip(width, 0.15, 0.2104, 0.035, 4.4)
    assert res["zDiff"] == pytest.approx(90.0, abs=0.01)


def test_solve_gap_for_a_differential_target():
    gap = imp.solve_gap(lambda g: imp.coupled_stripline(0.12, g, 0.2, 0.0175, 4.3)["zDiff"], 100.0)
    assert imp.coupled_stripline(0.12, gap, 0.2, 0.0175, 4.3)["zDiff"] == pytest.approx(
        100.0, abs=0.01
    )


def test_solver_respects_bounds_and_reports_reach():
    fn = lambda w: imp.microstrip(w, 0.2, 0.035, 4.4)["z0"]  # noqa: E731
    with pytest.raises(imp.SolveError) as info:
        imp.solve_width(fn, 120.0, min_width=0.1, max_width=1.0)
    low, high = info.value.reachable
    assert low == pytest.approx(fn(1.0))
    assert high == pytest.approx(fn(0.1))
    assert "outside the reachable range" in str(info.value)


@pytest.mark.parametrize(
    "call",
    [
        lambda: imp.microstrip(0, 0.2, 0.035, 4.4),
        lambda: imp.microstrip(0.2, -1, 0.035, 4.4),
        lambda: imp.microstrip(0.2, 0.2, -0.1, 4.4),
        lambda: imp.microstrip(0.2, 0.2, 0.035, 0.5),
        lambda: imp.coupled_microstrip(0.2, 0, 0.2, 0.035, 4.4),
        lambda: imp.coupled_stripline(0.2, 0.15, 0.2, 0.035, 4.4, h2=0),
        lambda: imp.microstrip(True, 0.2, 0.035, 4.4),
    ],
)
def test_invalid_geometry_raises(call):
    with pytest.raises(imp.ImpedanceError):
        call()
