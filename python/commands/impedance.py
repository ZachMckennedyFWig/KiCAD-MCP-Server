"""Controlled impedance from the board stackup.

``calculate_impedance`` reports the single-ended or differential impedance of
a trace on one copper layer, or solves the width (or gap) for a target. The
geometry comes from the ``(stackup ...)`` section of the saved ``.kicad_pcb``
(read the same way as ``get_stackup``, because pcbnew does not expose the
stackup), and the formulas live in ``utils.impedance``.

Reference planes: unless ``referenceLayers`` names them, the copper layers
directly above and below the trace layer are taken to be solid reference
planes. An outer layer is a surface microstrip over the next copper layer; an
inner layer is a stripline between its two neighbours (offset when the two
dielectric heights differ).
"""

import logging
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from commands.stackup import read_stackup_text

from utils import impedance as imp

logger = logging.getLogger("kicad_interface")

DEFAULT_GAP_MM = 0.2
DEFAULT_EPSILON_R = 4.5

# JLCPCB JLC04161H-7628: 1.6 mm, 1 oz outer, 0.5 oz inner, 7628 prepreg.
DEFAULT_STACKUP_4_LAYER: List[Dict[str, Any]] = [
    {"type": "soldermask", "canonical": "F.Mask", "thicknessMm": 0.01},
    {"type": "copper", "canonical": "F.Cu", "thicknessMm": 0.035},
    {"type": "dielectric", "thicknessMm": 0.2104, "epsilonR": 4.4, "material": "7628"},
    {"type": "copper", "canonical": "In1.Cu", "thicknessMm": 0.0152},
    {"type": "dielectric", "thicknessMm": 1.065, "epsilonR": 4.6, "material": "FR4"},
    {"type": "copper", "canonical": "In2.Cu", "thicknessMm": 0.0152},
    {"type": "dielectric", "thicknessMm": 0.2104, "epsilonR": 4.4, "material": "7628"},
    {"type": "copper", "canonical": "B.Cu", "thicknessMm": 0.035},
    {"type": "soldermask", "canonical": "B.Mask", "thicknessMm": 0.01},
]
DEFAULT_STACKUP_2_LAYER: List[Dict[str, Any]] = [
    {"type": "soldermask", "canonical": "F.Mask", "thicknessMm": 0.01},
    {"type": "copper", "canonical": "F.Cu", "thicknessMm": 0.035},
    {"type": "dielectric", "thicknessMm": 1.51, "epsilonR": 4.5, "material": "FR4"},
    {"type": "copper", "canonical": "B.Cu", "thicknessMm": 0.035},
    {"type": "soldermask", "canonical": "B.Mask", "thicknessMm": 0.01},
]
DEFAULT_STACKUP_NOTES = {
    4: (
        "Board has no stackup; assumed a JLC-like 4-layer 1.6 mm stackup "
        "(JLC04161H-7628: 35 um outer copper, 0.2104 mm 7628 prepreg er 4.4, "
        "15.2 um inner copper, 1.065 mm core er 4.6)"
    ),
    2: (
        "Board has no stackup; assumed a 2-layer 1.6 mm FR-4 board "
        "(35 um copper, 1.51 mm core er 4.5)"
    ),
}


class GeometryError(ValueError):
    """The layer or stackup cannot give an impedance geometry."""


def _failure(message: str, details: str) -> Dict[str, Any]:
    return {"success": False, "message": message, "errorDetails": details}


def _row_names(row: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(n for n in (row.get("canonical"), row.get("name")) if isinstance(n, str))


def _dielectric_pieces(row: Dict[str, Any]) -> List[Tuple[float, Optional[float]]]:
    """(thickness, epsilonR) for each sublayer of a dielectric row."""
    pieces = row.get("sublayers")
    if isinstance(pieces, list) and pieces:
        out = []
        for piece in pieces:
            thickness = piece.get("thicknessMm")
            if isinstance(thickness, (int, float)) and thickness > 0:
                out.append((float(thickness), piece.get("epsilonR")))
        if out:
            return out
    thickness = row.get("thicknessMm")
    if isinstance(thickness, (int, float)) and thickness > 0:
        return [(float(thickness), row.get("epsilonR"))]
    return []


def _walk(
    layers: List[Dict[str, Any]],
    start: int,
    step: int,
    is_reference: Callable[[Dict[str, Any]], bool],
    warnings: List[str],
) -> Dict[str, Any]:
    """Collect dielectric from ``start`` towards the next reference plane."""
    height = 0.0
    weighted = 0.0
    weight = 0.0
    dielectrics: List[Dict[str, Any]] = []
    i = start + step
    while 0 <= i < len(layers):
        row = layers[i]
        kind = row.get("type")
        if kind == "copper":
            if is_reference(row):
                return {
                    "height": height,
                    "epsilonWeighted": weighted,
                    "epsilonWeight": weight,
                    "plane": (_row_names(row) or ("?",))[0],
                    "dielectrics": dielectrics,
                }
            # A copper layer between the trace and its reference: count its
            # thickness as height but not in the permittivity average.
            thickness = row.get("thicknessMm")
            if isinstance(thickness, (int, float)):
                height += float(thickness)
        elif kind == "dielectric":
            for thickness, eps in _dielectric_pieces(row):
                if not isinstance(eps, (int, float)) or isinstance(eps, bool) or eps < 1:
                    warnings.append(
                        f"A dielectric row has no epsilon_r; assumed {DEFAULT_EPSILON_R}"
                    )
                    eps = DEFAULT_EPSILON_R
                height += thickness
                weighted += thickness * float(eps)
                weight += thickness
                entry: Dict[str, Any] = {"thicknessMm": thickness, "epsilonR": float(eps)}
                if row.get("material"):
                    entry["material"] = row["material"]
                dielectrics.append(entry)
        i += step
    return {
        "height": height,
        "epsilonWeighted": weighted,
        "epsilonWeight": weight,
        "plane": None,
        "dielectrics": dielectrics,
    }


def _mask_near(layers: List[Dict[str, Any]], index: int, step: int) -> Optional[Dict[str, Any]]:
    i = index + step
    while 0 <= i < len(layers):
        row = layers[i]
        if row.get("type") == "soldermask":
            mask: Dict[str, Any] = {}
            if isinstance(row.get("thicknessMm"), (int, float)):
                mask["thicknessMm"] = row["thicknessMm"]
            if isinstance(row.get("epsilonR"), (int, float)):
                mask["epsilonR"] = row["epsilonR"]
            return mask
        if row.get("type") in ("copper", "dielectric"):
            return None
        i += step
    return None


def layer_geometry(
    layers: List[Dict[str, Any]],
    layer: str,
    reference_layers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Impedance geometry of ``layer`` from parsed stackup rows.

    ``layers`` is the ``layers`` list of a ``get_stackup`` result (top to
    bottom). Returns ``structure`` (microstrip, stripline, or
    embedded_microstrip), ``copperThicknessMm``, ``epsilonR``, heights to the
    reference planes, and the planes used. Raises :class:`GeometryError`.
    """
    matches = [i for i, row in enumerate(layers) if layer in _row_names(row)]
    if not matches:
        coppers = [n for row in layers if row.get("type") == "copper" for n in _row_names(row)[:1]]
        raise GeometryError(
            f"Layer {layer} is not in the stackup (copper layers: {', '.join(coppers)})"
        )
    index = matches[0]
    row = layers[index]
    if row.get("type") != "copper":
        raise GeometryError(f"Layer {layer} is not a copper layer")
    copper = row.get("thicknessMm")
    warnings: List[str] = []
    if not isinstance(copper, (int, float)) or copper < 0:
        copper = 0.035
        warnings.append("The copper row has no thickness; assumed 0.035 mm")

    refs = set(reference_layers or [])
    unknown = [
        name
        for name in refs
        if not any(name in _row_names(r) and r.get("type") == "copper" for r in layers)
    ]
    if unknown:
        raise GeometryError(f"Reference layers not in the stackup: {', '.join(sorted(unknown))}")
    if layer in refs:
        raise GeometryError("The trace layer cannot be its own reference plane")

    def is_reference(candidate: Dict[str, Any]) -> bool:
        if not refs:
            return True
        return any(name in refs for name in _row_names(candidate))

    above = _walk(layers, index, -1, is_reference, warnings)
    below = _walk(layers, index, +1, is_reference, warnings)
    has_above = above["plane"] is not None
    has_below = below["plane"] is not None
    if not has_above and not has_below:
        raise GeometryError(f"No reference plane found above or below {layer}")

    geometry: Dict[str, Any] = {
        "layer": layer,
        "copperThicknessMm": float(copper),
        "referencePlanes": [p for p in (above["plane"], below["plane"]) if p],
    }
    if has_above and has_below:
        weight = above["epsilonWeight"] + below["epsilonWeight"]
        eps = (above["epsilonWeighted"] + below["epsilonWeighted"]) / weight if weight else None
        geometry.update(
            {
                "structure": "stripline",
                "heightAboveMm": above["height"],
                "heightBelowMm": below["height"],
                "dielectrics": above["dielectrics"][::-1] + below["dielectrics"],
            }
        )
    else:
        side, other = (below, above) if has_below else (above, below)
        eps = side["epsilonWeighted"] / side["epsilonWeight"] if side["epsilonWeight"] else None
        geometry.update(
            {
                "heightMm": side["height"],
                "dielectrics": side["dielectrics"],
            }
        )
        if other["height"] > 0:
            # A buried layer referenced to one side only: dielectric covers it.
            geometry["structure"] = "embedded_microstrip"
            geometry["coverMm"] = other["height"]
        else:
            geometry["structure"] = "microstrip"
            mask = _mask_near(layers, index, -1 if has_below else +1)
            if mask:
                geometry["solderMask"] = mask
    if eps is None:
        raise GeometryError(f"No dielectric between {layer} and its reference plane")
    geometry["epsilonR"] = eps
    heights = [geometry.get(k) for k in ("heightMm", "heightAboveMm", "heightBelowMm")]
    if any(h is not None and h <= 0 for h in heights):
        raise GeometryError(f"Zero dielectric height between {layer} and its reference plane")
    if warnings:
        geometry["warnings"] = sorted(set(warnings))
    return geometry


def _copper_count(board: Any) -> Optional[int]:
    fn = getattr(board, "GetCopperLayerCount", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _board_path(board: Any) -> Optional[str]:
    getter = getattr(board, "GetFileName", None)
    if not callable(getter):
        return None
    try:
        path = getter()
    except Exception:
        return None
    return path if isinstance(path, str) and path else None


def board_stackup_rows(board: Any) -> Tuple[List[Dict[str, Any]], str, Optional[str]]:
    """Stackup rows for ``board``: (rows, source, note).

    ``source`` is ``"board"`` when the rows come from the saved board file and
    ``"default"`` when the board has no stackup (then ``note`` says which
    default was assumed). Raises :class:`GeometryError` when neither works.
    """
    path = _board_path(board)
    reason = "the board has no file"
    if path is not None:
        file_path = Path(path)
        if file_path.is_file():
            try:
                text = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                text = None
                reason = f"the board file could not be read ({exc})"
            if text is not None:
                parsed = read_stackup_text(board, text)
                if parsed.get("success") and any(
                    row.get("type") == "copper" for row in parsed.get("layers", [])
                ):
                    return parsed["layers"], "board", None
                reason = "the board file has no (stackup ...) section"
        else:
            reason = "the board file is not saved yet"
    count = _copper_count(board)
    if count is None or count == 4:
        return DEFAULT_STACKUP_4_LAYER, "default", f"{DEFAULT_STACKUP_NOTES[4]} because {reason}"
    if count == 2:
        return DEFAULT_STACKUP_2_LAYER, "default", f"{DEFAULT_STACKUP_NOTES[2]} because {reason}"
    raise GeometryError(
        f"Cannot read a stackup because {reason}, and there is no default for a "
        f"{count}-layer board; define the stackup in Board Setup or with set_stackup_layer"
    )


def read_layer_geometry(
    board: Any, layer: str, reference_layers: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Geometry for ``layer`` on ``board``, with ``stackupSource`` and any note."""
    rows, source, note = board_stackup_rows(board)
    geometry = layer_geometry(rows, layer, reference_layers)
    geometry["stackupSource"] = source
    if note:
        geometry["stackupNote"] = note
    return geometry


# ---------------------------------------------------------------------------
# Evaluation on a geometry
# ---------------------------------------------------------------------------


def _embedded_scale(eeff: float, er: float, cover: float, h: float) -> Tuple[float, float]:
    factor = math.exp(-2.0 * cover / h)
    new = eeff * factor + er * (1.0 - factor)
    return math.sqrt(eeff / new), new


def single_impedance(geometry: Dict[str, Any], width: float) -> Dict[str, Any]:
    """Single-ended Z0 of a ``width`` mm trace on ``geometry``."""
    t = geometry["copperThicknessMm"]
    er = geometry["epsilonR"]
    structure = geometry["structure"]
    if structure == "microstrip":
        res = imp.microstrip(width, geometry["heightMm"], t, er)
        model = imp.MICROSTRIP_MODEL
    elif structure == "embedded_microstrip":
        res = imp.embedded_microstrip(width, geometry["heightMm"], t, er, geometry["coverMm"])
        model = imp.EMBEDDED_MICROSTRIP_MODEL
    else:
        h1, h2 = geometry["heightBelowMm"], geometry["heightAboveMm"]
        res = imp.stripline(width, h1, t, er, h2=h2)
        model = imp.STRIPLINE_MODEL if abs(h1 - h2) < 1e-9 else imp.OFFSET_STRIPLINE_MODEL
    return {"z0": res["z0"], "epsilonEff": res["epsilonEff"], "model": model}


def pair_impedance(geometry: Dict[str, Any], width: float, gap: float) -> Dict[str, Any]:
    """Differential (edge-coupled) impedances for ``width`` and edge gap ``gap``."""
    t = geometry["copperThicknessMm"]
    er = geometry["epsilonR"]
    structure = geometry["structure"]
    if structure in ("microstrip", "embedded_microstrip"):
        h = geometry["heightMm"]
        res = dict(imp.coupled_microstrip(width, gap, h, t, er))
        model = imp.COUPLED_MICROSTRIP_MODEL
        if structure == "embedded_microstrip":
            cover = geometry["coverMm"]
            se, ee = _embedded_scale(res["epsilonEffEven"], er, cover, h)
            so, eo = _embedded_scale(res["epsilonEffOdd"], er, cover, h)
            res["zEven"] *= se
            res["zOdd"] *= so
            res["epsilonEffEven"], res["epsilonEffOdd"] = ee, eo
            res["zDiff"] = 2.0 * res["zOdd"]
            res["zCommon"] = res["zEven"] / 2.0
            res["z0"] = math.sqrt(res["zEven"] * res["zOdd"])
            res["coupling"] = (res["zEven"] - res["zOdd"]) / (res["zEven"] + res["zOdd"])
            model += ", with Wadell dielectric-cover correction per mode"
    else:
        h1, h2 = geometry["heightBelowMm"], geometry["heightAboveMm"]
        res = dict(imp.coupled_stripline(width, gap, h1, t, er, h2=h2))
        model = imp.COUPLED_STRIPLINE_MODEL
        if abs(h1 - h2) >= 1e-9:
            model += "; offset by admittance sum"
    out: Dict[str, Any] = dict(res)
    out["model"] = model
    return out


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: _round(v, digits) for k, v in value.items()}
    if isinstance(value, list):
        return [_round(v, digits) for v in value]
    return value


def public_geometry(geometry: Dict[str, Any]) -> Dict[str, Any]:
    return _round(dict(geometry))


def format_pair_impedance(res: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("zDiff", "zOdd", "zEven", "zCommon", "coupling", "epsilonEffOdd", "epsilonEffEven")
    out: Dict[str, Any] = {k: round(res[k], 2 if k.startswith("z") else 4) for k in keys}
    out["model"] = res["model"]
    return out


def solve_pair(
    geometry: Dict[str, Any],
    target_zdiff: float,
    width: Optional[float] = None,
    gap: Optional[float] = None,
    bounds: Optional[Dict[str, float]] = None,
) -> Tuple[float, float, str]:
    """(width, gap, solvedFor) for ``target_zdiff``.

    Keeps ``width`` fixed and solves the gap when only the width is given;
    otherwise keeps ``gap`` (default :data:`DEFAULT_GAP_MM`) and solves the
    width. Raises :class:`utils.impedance.SolveError` when out of reach.
    """
    bounds = bounds or {}
    min_w = bounds.get("minWidth", imp.DEFAULT_MIN_WIDTH)
    max_w = bounds.get("maxWidth", imp.DEFAULT_MAX_WIDTH)
    min_g = bounds.get("minGap", imp.DEFAULT_MIN_GAP)
    max_g = bounds.get("maxGap", imp.DEFAULT_MAX_GAP)
    if width is not None and gap is None:
        solved = imp.solve_gap(
            lambda g: pair_impedance(geometry, width, g)["zDiff"], target_zdiff, min_g, max_g
        )
        return width, solved, "gap"
    if gap is None:
        gap = DEFAULT_GAP_MM
    solved = imp.solve_width(
        lambda w: pair_impedance(geometry, w, gap)["zDiff"], target_zdiff, min_w, max_w
    )
    return solved, gap, "width"


def solve_single(
    geometry: Dict[str, Any], target_z0: float, bounds: Optional[Dict[str, float]] = None
) -> float:
    bounds = bounds or {}
    return imp.solve_width(
        lambda w: single_impedance(geometry, w)["z0"],
        target_z0,
        bounds.get("minWidth", imp.DEFAULT_MIN_WIDTH),
        bounds.get("maxWidth", imp.DEFAULT_MAX_WIDTH),
    )


def board_minimums(board: Any) -> Dict[str, float]:
    """Minimum track width and clearance from the board rules, in mm (if set)."""
    out: Dict[str, float] = {}
    try:
        settings = board.GetDesignSettings()
    except Exception:
        return out
    for attr, key in (("m_TrackMinWidth", "minWidth"), ("m_MinClearance", "minGap")):
        value = getattr(settings, attr, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            out[key] = value / 1_000_000.0
    return out


def _positive_mm(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def net_class_diff_pair_gap(board: Any, net_name: str, net: Any = None) -> Optional[float]:
    """Diff-pair gap (mm) of the net class ``net_name`` belongs to, if one is set.

    KiCad 10's SWIG ``NETINFO_ITEM.GetNetClass()`` returns an opaque object,
    and a headless board carries only the Default class anyway, so the
    project file is the source: an explicit assignment, then the first
    matching pattern, then Default. A live NETCLASS proxy is used when one
    is available.
    """
    try:
        net_class = net.GetNetClass() if net is not None else None
        getter = getattr(net_class, "GetDiffPairGap", None)
        if callable(getter):
            has_gap = getattr(net_class, "HasDiffPairGap", None)
            if not callable(has_gap) or has_gap():
                value = getter()
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value / 1_000_000
    except Exception:
        pass

    path = _board_path(board)
    if not path:
        return None
    try:
        from utils.project_netclasses import load_project_net_classes

        settings = load_project_net_classes(str(Path(path).with_suffix(".kicad_pro")))
    except Exception:
        return None
    if not settings:
        return None
    classes = {cls.get("name"): cls for cls in settings.get("classes", [])}
    names: List[str] = list(settings.get("assignments", {}).get(net_name, []))
    if not names:
        from fnmatch import fnmatchcase

        for pattern, class_name in settings.get("patterns", []):
            if fnmatchcase(net_name, pattern):
                names.append(class_name)
                break
    names.append("Default")
    for name in names:
        gap = _positive_mm((classes.get(name) or {}).get("diff_pair_gap"))
        if gap is not None:
            return gap
    return None


def _number(params: Dict[str, Any], key: str) -> Optional[float]:
    if key not in params or params[key] is None:
        return None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a number")
    if value <= 0:
        raise ValueError(f"{key} must be greater than zero")
    return float(value)


class ImpedanceCommands:
    """``calculate_impedance``. Reads the board through ``get_board`` on every call."""

    def __init__(self, get_board: Callable[[], Any]):
        self._get_board = get_board

    def calculate_impedance(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Impedance of a trace or pair on ``layer``, or the width/gap for a target.

        Params: ``layer`` (required), ``mode`` ("single" or "differential";
        defaults to differential when ``gap`` or ``targetDiffImpedance`` is
        given), ``width``, ``gap`` (edge to edge), ``targetImpedance``
        (single-ended ohms), ``targetDiffImpedance`` (differential ohms),
        ``minWidth``/``maxWidth``/``minGap``/``maxGap`` (solver bounds, mm),
        ``referenceLayers`` (copper layers that are solid planes; default:
        the adjacent copper layers).
        """
        board = self._get_board()
        if not board:
            return _failure("No board is loaded", "Load or create a board first")
        layer = params.get("layer")
        if not isinstance(layer, str) or not layer:
            return _failure("layer is required", "Pass a copper layer such as F.Cu")
        mode = params.get("mode")
        try:
            width = _number(params, "width")
            gap = _number(params, "gap")
            target = _number(params, "targetImpedance")
            target_diff = _number(params, "targetDiffImpedance")
            bounds = board_minimums(board)
            for key in ("minWidth", "maxWidth", "minGap", "maxGap"):
                value = _number(params, key)
                if value is not None:
                    bounds[key] = value
        except ValueError as exc:
            return _failure("Invalid parameter", str(exc))
        if mode is None:
            mode = "differential" if (gap is not None or target_diff is not None) else "single"
        if mode not in ("single", "differential"):
            return _failure("Invalid mode", "mode must be 'single' or 'differential'")
        if mode == "single" and target_diff is not None:
            return _failure(
                "targetDiffImpedance needs mode 'differential'",
                "Use targetImpedance for a single-ended trace",
            )
        if mode == "differential" and target is not None:
            return _failure(
                "Use targetDiffImpedance in differential mode",
                "targetImpedance is the single-ended Z0 target",
            )
        references = params.get("referenceLayers")
        if references is not None and (
            not isinstance(references, list) or not all(isinstance(r, str) for r in references)
        ):
            return _failure("Invalid parameter", "referenceLayers must be a list of layer names")

        try:
            geometry = read_layer_geometry(board, layer, references)
        except GeometryError as exc:
            return _failure("Cannot derive the layer geometry", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"calculate_impedance stackup read failed: {exc}")
            return _failure("Failed to read the stackup", str(exc))

        try:
            if mode == "single":
                result = self._single(geometry, width, target, bounds)
            else:
                result = self._differential(geometry, width, gap, target_diff, bounds)
        except imp.SolveError as exc:
            return {
                "success": False,
                "message": "Target impedance is out of reach",
                "errorDetails": str(exc),
                "reachableOhms": [round(v, 2) for v in exc.reachable],
                "bounds": _round(bounds),
                "geometry": public_geometry(geometry),
            }
        except (imp.ImpedanceError, ValueError) as exc:
            return _failure("Cannot calculate impedance", str(exc))
        if isinstance(result, dict) and result.get("success") is False:
            return result
        result["geometry"] = public_geometry(geometry)
        result["accuracyNote"] = imp.ACCURACY_NOTE
        notes = []
        if geometry.get("stackupNote"):
            notes.append(geometry["stackupNote"])
        if geometry["structure"] == "stripline":
            ratio = max(geometry["heightAboveMm"], geometry["heightBelowMm"]) / min(
                geometry["heightAboveMm"], geometry["heightBelowMm"]
            )
            if ratio > 3:
                notes.append(
                    f"The plane heights differ by {ratio:.1f}:1; the offset-stripline "
                    "approximation reads high by about 4-7% at this asymmetry"
                )
        if not params.get("referenceLayers"):
            notes.append(
                "The adjacent copper layers ("
                + ", ".join(geometry["referencePlanes"])
                + ") are assumed to be solid reference planes; pass referenceLayers otherwise"
            )
        result["notes"] = notes
        return result

    def _single(self, geometry, width, target, bounds) -> Dict[str, Any]:
        if target is not None:
            width = solve_single(geometry, target, bounds)
            solved = "width"
        elif width is None:
            return _failure("width is required", "Pass width, or targetImpedance to solve it")
        else:
            solved = None
        res = single_impedance(geometry, width)
        out: Dict[str, Any] = {
            "success": True,
            "mode": "single",
            "layer": geometry["layer"],
            "widthMm": round(width, 4),
            "z0": round(res["z0"], 2),
            "epsilonEff": round(res["epsilonEff"], 4),
            "model": res["model"],
        }
        if solved:
            out["solvedFor"] = solved
            out["targetImpedance"] = target
        out["message"] = (
            f"Z0 {out['z0']} ohm for a {out['widthMm']} mm trace on {geometry['layer']}"
        )
        return out

    def _differential(self, geometry, width, gap, target, bounds) -> Dict[str, Any]:
        solved = None
        if target is not None and not (width is not None and gap is not None):
            width, gap, solved = solve_pair(geometry, target, width, gap, bounds)
        elif width is None or gap is None:
            return _failure(
                "width and gap are required",
                "Pass width and gap, or targetDiffImpedance to solve one of them",
            )
        res = pair_impedance(geometry, width, gap)
        single = single_impedance(geometry, width)
        out: Dict[str, Any] = {
            "success": True,
            "mode": "differential",
            "layer": geometry["layer"],
            "widthMm": round(width, 4),
            "gapMm": round(gap, 4),
            **format_pair_impedance(res),
            "z0Uncoupled": round(single["z0"], 2),
        }
        if target is not None:
            out["targetDiffImpedance"] = target
            out["deviationPercent"] = round(100.0 * (res["zDiff"] - target) / target, 2) + 0.0
        if solved:
            out["solvedFor"] = solved
        out["message"] = (
            f"Zdiff {out['zDiff']} ohm for width {out['widthMm']} mm, gap {out['gapMm']} mm "
            f"on {geometry['layer']}"
        )
        return out
