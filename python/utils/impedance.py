"""Closed-form controlled-impedance calculators for PCB transmission lines.

All lengths are millimetres and every function is static (quasi-TEM, no
frequency dispersion, lossless). Nothing here imports pcbnew, so the formulas
are unit-testable on their own.

Models, and their error against a 2D finite-volume field solver
-----------------------------------------------------------------
The percentages below were measured during development with a
non-uniform-grid finite-volume Laplace solver (Richardson-extrapolated;
it reproduces Cohn's exact zero-thickness stripline to 0.01 %) on
PCB-typical geometries: h 0.09-1.5 mm, 0-35 um copper, er 4.0-4.5.

Single-ended microstrip
    Hammerstad & Jensen, "Accurate Models for Microstrip Computer-Aided
    Design", IEEE MTT-S 1980, with their finite-thickness width
    correction. Within 0.1 % of the field solver on every case tried.

Embedded (buried) microstrip
    The microstrip result with the effective permittivity raised by a
    dielectric cover of thickness ``cover`` over the strip, using the
    exponential cover factor from Wadell, "Transmission Line Design
    Handbook" (1991): ``eeff' = eeff * exp(-2 d / h) + er * (1 - exp(-2 d /
    h))``. Reads 1-2 % low against the field solver for covers of
    0.05-0.3 mm.

Symmetric stripline
    Wheeler, "Transmission-Line Properties of a Strip Line Between Parallel
    Planes", IEEE MTT 1978 (Wadell sec. 3.6), with his finite-thickness
    width correction. Within 0.5 % of the field solver, and within 0.3 % of
    Cohn's exact result at zero thickness.

Offset (asymmetric) stripline
    Admittance sum of two symmetric lines (Shelton's construction):
    ``1/Z = (1/Z_sym(2*h1 + t) + 1/Z_sym(2*h2 + t)) / 2``. Overestimates
    Z: +0.4 % at a 1.5:1 height ratio, +1.3 % at 2:1, +3.4 % at 3:1 and
    +6.7 % at 5:1.

Edge-coupled (differential) microstrip
    Kirschning & Jansen, "Accurate Wide-Range Design Equations for the
    Frequency-Dependent Characteristic of Parallel Coupled Microstrip Lines",
    IEEE MTT 1984 (static part), with Jansen's 1978 even/odd thickness
    correction on the Bahl/Garg single-strip width correction. Every
    width-dependent term, including the single-line reference, uses the
    thickness-corrected even- or odd-mode width. Zodd (so Zdiff) within
    +/-1.6 % of the field solver; Zeven reads 3-9 % low with 35 um copper
    on 0.1-0.2 mm dielectric. Zero thickness: within 0.2 % for both modes.

Edge-coupled (differential) stripline
    Cohn, "Shielded Coupled-Strip Transmission Line", IEEE MTT 1955: the
    exact zero-thickness conformal-mapping result (complete elliptic
    integrals) with Cohn's fringing-capacitance thickness correction,
    ``1/Ze = 1/Z(t) - (Cf(t)/Cf(0)) * (1/Z(0) - 1/Ze(0))`` and the mirror
    form for the odd mode. Zodd +1.5 to +2.2 % and Zeven -0.9 to -1.7 %
    for centred lines. Offset lines use the admittance sum above: Zodd
    +2.5 % at 1.5:1, +3.2 % at 2:1, +4.2 % at 3:1, +5.4 % at 5:1.

None of the models include solder mask, etch trapezoid, copper roughness, or
frequency dispersion. Treat every result as an estimate within about
+/-5 % of a field solver (+/-10 % with strongly offset striplines or a solder
mask), and confirm critical nets with the fab's impedance calculator, which
uses their own process data.

Zdiff = 2 * Zodd and Zcommon = Zeven / 2 throughout.
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional, Tuple

#: Free-space wave impedance, ohms.
ETA0 = 376.730313668

MICROSTRIP_MODEL = "Hammerstad-Jensen (1980) microstrip with thickness correction"
EMBEDDED_MICROSTRIP_MODEL = "Hammerstad-Jensen microstrip with Wadell dielectric-cover correction"
STRIPLINE_MODEL = "Wheeler (1978) stripline with thickness correction"
OFFSET_STRIPLINE_MODEL = "Wheeler (1978) stripline, offset by admittance sum of two symmetric lines"
COUPLED_MICROSTRIP_MODEL = (
    "Kirschning-Jansen (1984) edge-coupled microstrip with Jansen thickness correction"
)
COUPLED_STRIPLINE_MODEL = (
    "Cohn (1955) edge-coupled stripline: exact zero-thickness elliptic form with "
    "fringing-capacitance thickness correction"
)
ACCURACY_NOTE = (
    "Closed-form quasi-static estimate, typically within +/-5% of a 2D field "
    "solver (up to about +7% for a stripline whose plane heights differ by 5:1). "
    "Solder mask (lowers outer-layer impedance by roughly 2-5%), etch "
    "trapezoid, copper roughness, and frequency dispersion are not modelled. "
    "Confirm critical nets with the fabricator's impedance calculator."
)


class ImpedanceError(ValueError):
    """Raised for a geometry the formulas cannot evaluate."""


def _check_positive(**values: float) -> None:
    for name, value in values.items():
        if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ImpedanceError(f"{name} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ImpedanceError(f"{name} must be greater than zero")


def _check_thickness(t: float) -> None:
    if t is None or not isinstance(t, (int, float)) or isinstance(t, bool):
        raise ImpedanceError("thickness must be a number")
    if not math.isfinite(t) or t < 0:
        raise ImpedanceError("thickness must be zero or greater")


def _check_er(er: float) -> None:
    if er is None or not isinstance(er, (int, float)) or isinstance(er, bool):
        raise ImpedanceError("epsilonR must be a number")
    if not math.isfinite(er) or er < 1.0:
        raise ImpedanceError("epsilonR must be 1 or greater")


# ---------------------------------------------------------------------------
# Elliptic integrals (Cohn's exact stripline forms)
# ---------------------------------------------------------------------------


def ellipk(k: float) -> float:
    """Complete elliptic integral of the first kind K(k), modulus ``k``.

    Arithmetic-geometric mean; exact to machine precision for 0 <= k < 1.
    """
    if not 0.0 <= k < 1.0:
        raise ImpedanceError("elliptic modulus must be in [0, 1)")
    a = 1.0
    b = math.sqrt(1.0 - k * k)
    for _ in range(60):
        if abs(a - b) <= 1e-15 * a:
            break
        a, b = (a + b) / 2.0, math.sqrt(a * b)
    return math.pi / (2.0 * a)


def _k_ratio(k: float) -> float:
    """K(k') / K(k) with k' = sqrt(1 - k^2), stable near k = 0 and k = 1."""
    kp = math.sqrt(max(0.0, 1.0 - k * k))
    if k < 1e-6:
        # K(k') -> ln(4/k) as k -> 0
        return math.log(4.0 / k) / (math.pi / 2.0)
    if kp < 1e-6:
        return (math.pi / 2.0) / math.log(4.0 / kp)
    return ellipk(kp) / ellipk(k)


# ---------------------------------------------------------------------------
# Microstrip (Hammerstad-Jensen)
# ---------------------------------------------------------------------------


def _hj_z01(u: float) -> float:
    """Impedance of a microstrip of normalised width u in air."""
    f = 6.0 + (2.0 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    return (ETA0 / (2.0 * math.pi)) * math.log(f / u + math.sqrt(1.0 + (2.0 / u) ** 2))


def _hj_fill(u: float, er: float) -> float:
    """Hammerstad-Jensen filling factor ``(1 + 10/u)^(-a b)``."""
    a = (
        1.0
        + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0
        + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    return (1.0 + 10.0 / u) ** (-a * b)


def _hj_eeff(u: float, er: float) -> float:
    return (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * _hj_fill(u, er)


def _hj_du(u: float, th: float, er: float) -> float:
    """Hammerstad-Jensen thickness width correction (normalised to h)."""
    if th <= 0:
        return 0.0
    coth2 = 1.0 / math.tanh(math.sqrt(6.517 * u)) ** 2
    du1 = (th / math.pi) * math.log(1.0 + 4.0 * math.e / (th * coth2))
    if er == 1.0:
        return du1
    return 0.5 * du1 * (1.0 + 1.0 / math.cosh(math.sqrt(er - 1.0)))


def microstrip(w: float, h: float, t: float, er: float) -> Dict[str, float]:
    """Single-ended surface microstrip.

    ``w`` trace width, ``h`` dielectric height to the reference plane, ``t``
    copper thickness, ``er`` substrate permittivity. Returns ``z0`` (ohm) and
    ``epsilonEff``.
    """
    _check_positive(width=w, height=h)
    _check_thickness(t)
    _check_er(er)
    u = w / h
    th = t / h
    u1 = u + _hj_du(u, th, 1.0)
    ur = u + _hj_du(u, th, er)
    eeff_r = _hj_eeff(ur, er)
    z0 = _hj_z01(ur) / math.sqrt(eeff_r)
    eeff = eeff_r * (_hj_z01(u1) / _hj_z01(ur)) ** 2
    return {"z0": z0, "epsilonEff": eeff}


def embedded_microstrip(w: float, h: float, t: float, er: float, cover: float) -> Dict[str, float]:
    """Microstrip buried under ``cover`` mm of the same dielectric above the trace."""
    base = microstrip(w, h, t, er)
    if cover is None or cover <= 0:
        return base
    factor = math.exp(-2.0 * cover / h)
    eeff = base["epsilonEff"] * factor + er * (1.0 - factor)
    # Z0 scales with 1/sqrt(eeff) for the same geometry.
    z0 = base["z0"] * math.sqrt(base["epsilonEff"] / eeff)
    return {"z0": z0, "epsilonEff": eeff}


# ---------------------------------------------------------------------------
# Stripline (Wheeler; Cohn)
# ---------------------------------------------------------------------------


def _wheeler_stripline(w: float, b: float, t: float, er: float) -> float:
    """Wheeler 1978 centred stripline, plane spacing ``b`` (includes t)."""
    if t >= b:
        raise ImpedanceError("copper thickness must be less than the plane spacing")
    x = t / b
    if x > 0:
        m = 2.0 / (1.0 + (2.0 / 3.0) * x / (1.0 - x))
        dw = (
            (b - t)
            * (x / (math.pi * (1.0 - x)))
            * (1.0 - 0.5 * math.log((x / (2.0 - x)) ** 2 + (0.0796 * x / (w / b + 1.1 * x)) ** m))
        )
    else:
        dw = 0.0
    wp = w + dw
    r = (b - t) / wp
    a = 4.0 * r / math.pi
    inner = 2.0 * a  # 8 (b - t) / (pi w')
    return (ETA0 / (4.0 * math.pi * math.sqrt(er))) * math.log(
        1.0 + a * (inner + math.sqrt(inner * inner + 6.27))
    )


def _plane_spacings(h1: float, h2: Optional[float], t: float) -> Tuple[float, float]:
    """Virtual symmetric spacings 2*h1 + t and 2*h2 + t."""
    h2 = h1 if h2 is None else h2
    return 2.0 * h1 + t, 2.0 * h2 + t


def _admittance_sum(za: float, zb: float) -> float:
    return 2.0 * za * zb / (za + zb)


def stripline(
    w: float, h1: float, t: float, er: float, h2: Optional[float] = None
) -> Dict[str, float]:
    """Single-ended stripline between two planes.

    ``h1`` and ``h2`` are the dielectric heights from each face of the trace to
    the plane on that side (the plane spacing is ``h1 + t + h2``). Leave ``h2``
    out for a centred line.
    """
    _check_positive(width=w, height=h1)
    if h2 is not None:
        _check_positive(height2=h2)
    _check_thickness(t)
    _check_er(er)
    if h2 is None or abs(h1 - h2) <= 1e-12 * max(h1, h2):
        z0 = _wheeler_stripline(w, 2.0 * h1 + t, t, er)
    else:
        ba, bb = _plane_spacings(h1, h2, t)
        z0 = _admittance_sum(_wheeler_stripline(w, ba, t, er), _wheeler_stripline(w, bb, t, er))
    return {"z0": z0, "epsilonEff": er}


def stripline_exact_zero_thickness(w: float, b: float, er: float) -> float:
    """Cohn's exact centred stripline impedance for a zero-thickness strip."""
    _check_positive(width=w, spacing=b)
    _check_er(er)
    k = 1.0 / math.cosh(math.pi * w / (2.0 * b))
    return (ETA0 / (4.0 * math.sqrt(er))) / _k_ratio(k)


def _cohn_fringe(t: float, b: float) -> float:
    """Cohn's normalised fringing capacitance Cf(t/b) / eps (Cf(0) = 2 ln 2)."""
    if t <= 0:
        return 2.0 * math.log(2.0)
    x = t / b
    return 2.0 * math.log((2.0 - x) / (1.0 - x)) - x * math.log(x * (2.0 - x) / (1.0 - x) ** 2)


def _cohn_coupled_centred(w: float, s: float, b: float, t: float, er: float) -> Tuple[float, float]:
    """Even and odd impedance of centred edge-coupled stripline (Cohn 1955)."""
    if t >= b:
        raise ImpedanceError("copper thickness must be less than the plane spacing")
    scale = ETA0 / (4.0 * math.sqrt(er))
    a1 = math.tanh(math.pi * w / (2.0 * b))
    a2 = math.tanh(math.pi * (w + s) / (2.0 * b))
    ke = a1 * a2
    ko = a1 / a2
    ze0 = scale * _k_ratio(ke)
    zo0 = scale * _k_ratio(ko)
    if t <= 0:
        return ze0, zo0
    z_single_t = _wheeler_stripline(w, b, t, er)
    z_single_0 = _wheeler_stripline(w, b, 0.0, er)
    ratio = _cohn_fringe(t, b) / _cohn_fringe(0.0, b)
    ze = 1.0 / (1.0 / z_single_t - ratio * (1.0 / z_single_0 - 1.0 / ze0))
    zo = 1.0 / (1.0 / z_single_t + ratio * (1.0 / zo0 - 1.0 / z_single_0))
    return ze, zo


def _coupled_result(ze: float, zo: float, eeff_e: float, eeff_o: float) -> Dict[str, float]:
    return {
        "zEven": ze,
        "zOdd": zo,
        "zDiff": 2.0 * zo,
        "zCommon": ze / 2.0,
        "z0": math.sqrt(ze * zo),
        "coupling": (ze - zo) / (ze + zo),
        "epsilonEffEven": eeff_e,
        "epsilonEffOdd": eeff_o,
    }


def coupled_stripline(
    w: float, s: float, h1: float, t: float, er: float, h2: Optional[float] = None
) -> Dict[str, float]:
    """Edge-coupled stripline pair: width ``w``, edge-to-edge gap ``s``.

    ``h1``/``h2`` as for :func:`stripline`. ``z0`` in the result is the
    coupled-line characteristic impedance sqrt(Ze*Zo).
    """
    _check_positive(width=w, gap=s, height=h1)
    if h2 is not None:
        _check_positive(height2=h2)
    _check_thickness(t)
    _check_er(er)
    if h2 is None or abs(h1 - h2) <= 1e-12 * max(h1, h2):
        ze, zo = _cohn_coupled_centred(w, s, 2.0 * h1 + t, t, er)
    else:
        ba, bb = _plane_spacings(h1, h2, t)
        zea, zoa = _cohn_coupled_centred(w, s, ba, t, er)
        zeb, zob = _cohn_coupled_centred(w, s, bb, t, er)
        ze, zo = _admittance_sum(zea, zeb), _admittance_sum(zoa, zob)
    return _coupled_result(ze, zo, er, er)


# ---------------------------------------------------------------------------
# Edge-coupled microstrip (Kirschning-Jansen)
# ---------------------------------------------------------------------------


def _bahl_garg_du(u: float, th: float) -> float:
    """Bahl/Garg single-strip thickness width correction, normalised to h.

    ``(1.25 t / pi h)(1 + ln(2h/t))`` for u >= 1/2pi and
    ``(1.25 t / pi h)(1 + ln(4 pi w / t))`` below, blended smoothly.
    """
    if th <= 0:
        return 0.0
    wide = math.log(2.0 / th)
    narrow = math.log(4.0 * math.pi * u / th)
    blend = 1.0 / (1.0 + math.exp(-100.0 * (u - 1.0 / (2.0 * math.pi))))
    return (1.25 * th / math.pi) * (1.0 + narrow + (wide - narrow) * blend)


def _kj_fill_even(u: float, g: float, er: float) -> float:
    v = u * (20.0 + g * g) / (10.0 + g * g) + g * math.exp(-g)
    a = (
        1.0
        + math.log((v**4 + (v / 52.0) ** 2) / (v**4 + 0.432)) / 49.0
        + math.log(1.0 + (v / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    return (1.0 + 10.0 / v) ** (-a * b)


def _kj_fill_odd(u: float, g: float, er: float) -> float:
    b = 0.747 * er / (0.15 + er)
    c = b - (b - 0.207) * math.exp(-0.414 * u)
    d = 0.593 + 0.694 * math.exp(-0.562 * u)
    return math.exp(-c * g**d)


def coupled_microstrip(w: float, s: float, h: float, t: float, er: float) -> Dict[str, float]:
    """Edge-coupled surface microstrip pair: width ``w``, edge-to-edge gap ``s``."""
    _check_positive(width=w, gap=s, height=h)
    _check_thickness(t)
    _check_er(er)
    u = w / h
    g = s / h
    th = t / h

    if th > 0:
        du = _bahl_garg_du(u, th)
        dt = th / (g * er)
        du_e = du * (1.0 - 0.5 * math.exp(-0.69 * du / dt))
        du_o = du_e + dt
    else:
        du_e = du_o = 0.0
    ue = u + du_e
    uo = u + du_o

    # The zero-thickness single line is evaluated at each mode's corrected
    # width. Evaluating it at the drawn width instead (as some calculators
    # do) read Zodd 10-17 % high against a field solver with 35 um copper.
    eeff_se = _hj_eeff(ue, er)
    z_se = _hj_z01(ue) / math.sqrt(eeff_se)
    eeff_so = _hj_eeff(uo, er)
    z_so = _hj_z01(uo) / math.sqrt(eeff_so)

    eeff_e = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * _kj_fill_even(ue, g, er)
    a_o = 0.7287 * (eeff_so - (er + 1.0) / 2.0) * (1.0 - math.exp(-0.179 * uo))
    eeff_o = ((er + 1.0) / 2.0 + a_o - eeff_so) * _kj_fill_odd(uo, g, er) + eeff_so

    q1 = 0.8695 * ue**0.194
    q2 = 1.0 + 0.7519 * g + 0.189 * g**2.31
    q3 = (
        0.1975
        + (16.6 + (8.4 / g) ** 6) ** -0.387
        + math.log(g**10 / (1.0 + (g / 3.4) ** 10)) / 241.0
    )
    q4 = 2.0 * q1 / (q2 * (math.exp(-g) * ue**q3 + (2.0 - math.exp(-g)) * ue ** (-q3)))
    ze = z_se * math.sqrt(eeff_se / eeff_e) / (1.0 - z_se * math.sqrt(eeff_se) * q4 / ETA0)

    q5 = 1.794 + 1.14 * math.log(1.0 + 0.638 / (g + 0.517 * g**2.43))
    q6 = (
        0.2305
        + math.log(g**10 / (1.0 + (g / 5.8) ** 10)) / 281.3
        + math.log(1.0 + 0.598 * g**1.154) / 5.1
    )
    q7 = (10.0 + 190.0 * g * g) / (1.0 + 82.3 * g**3)
    q8 = math.exp(-6.5 - 0.95 * math.log(g) - (g / 0.15) ** 5)
    q9 = math.log(q7) * (q8 + 1.0 / 16.5)
    q10 = (q2 * q4 - q5 * math.exp(math.log(uo) * q6 * uo ** (-q9))) / q2
    zo = z_so * math.sqrt(eeff_so / eeff_o) / (1.0 - z_so * math.sqrt(eeff_so) * q10 / ETA0)
    return _coupled_result(ze, zo, eeff_e, eeff_o)


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------


class SolveError(ImpedanceError):
    """The target is outside what the allowed width or gap range can reach."""

    def __init__(self, message: str, reachable: Tuple[float, float]):
        super().__init__(message)
        self.reachable = reachable


def bisect_monotonic(
    fn: Callable[[float], float],
    target: float,
    lo: float,
    hi: float,
    tol: float = 1e-6,
    max_iter: int = 200,
) -> float:
    """Solve ``fn(x) == target`` on [lo, hi] for a monotonic ``fn`` by bisection.

    Raises :class:`SolveError` when the target lies outside [fn(lo), fn(hi)].
    """
    if not lo < hi:
        raise ImpedanceError("the lower bound must be below the upper bound")
    f_lo = fn(lo) - target
    f_hi = fn(hi) - target
    reach = tuple(sorted((f_lo + target, f_hi + target)))
    if f_lo == 0:
        return lo
    if f_hi == 0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        raise SolveError(
            f"target {target:.2f} ohm is outside the reachable range "
            f"{reach[0]:.2f}-{reach[1]:.2f} ohm",
            (reach[0], reach[1]),
        )
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = fn(mid) - target
        if f_mid == 0 or (hi - lo) / 2.0 < tol:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


#: Default search bounds, mm.
DEFAULT_MIN_WIDTH = 0.05
DEFAULT_MAX_WIDTH = 5.0
DEFAULT_MIN_GAP = 0.05
DEFAULT_MAX_GAP = 5.0


def solve_width(
    impedance_fn: Callable[[float], float],
    target: float,
    min_width: float = DEFAULT_MIN_WIDTH,
    max_width: float = DEFAULT_MAX_WIDTH,
) -> float:
    """Width giving ``target`` where ``impedance_fn(width)`` falls as width grows."""
    return bisect_monotonic(impedance_fn, target, min_width, max_width)


def solve_gap(
    impedance_fn: Callable[[float], float],
    target: float,
    min_gap: float = DEFAULT_MIN_GAP,
    max_gap: float = DEFAULT_MAX_GAP,
) -> float:
    """Gap giving ``target`` where ``impedance_fn(gap)`` rises as the gap grows."""
    return bisect_monotonic(impedance_fn, target, min_gap, max_gap)
