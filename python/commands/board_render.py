"""Cropped, net-highlighted board renders (MCP_ISSUES Part 2, section 2.4, Q2).

``get_board_2d_view`` renders the whole board through kicad-cli, so a 0.4 mm
pitch QFN ends up about 10 px wide and every net looks the same. This module
draws the board itself from pcbnew geometry (KiCad's own polygons for pads,
tracks, text and zone fills), which lets it:

* crop to any window (``region``) at any ``scale`` (pixels per mm);
* draw ``highlightNets`` in bright colours with everything else greyed;
* overlay the gaps between a net's islands (``showRatsnest``) and the markers
  from the last ``run_drc`` report (``showDrcMarkers``);
* render the in-memory board, unsaved edits included.

Pixel (0, 0) is the region's top-left corner (x0, y0) in KiCad board
coordinates, and ``px = (x - x0) * pxPerMm``, ``py = (y - y0) * pxPerMm``.
"""

import base64
import io
import json
import logging
import math
import os
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from commands.board_query import (
    BoardIndex,
    LayerNames,
    _box_mm,
    _boxes_overlap,
    _r,
    _xy,
    connectivity,
    find_islands,
    island_gaps,
    item_polygons,
    poly_set_outlines,
    resolve_board,
    resolve_net_names,
)

logger = logging.getLogger("kicad_interface")

MAX_SIDE_PX = 4096
BACKGROUND = (16, 18, 24)

# Roughly KiCad's default colour theme.
_LAYER_COLOURS = {
    "F.Cu": (200, 52, 52),
    "B.Cu": (77, 127, 196),
    "In1.Cu": (127, 200, 127),
    "In2.Cu": (206, 125, 44),
    "In3.Cu": (79, 203, 203),
    "In4.Cu": (219, 98, 139),
    "In5.Cu": (167, 165, 198),
    "In6.Cu": (40, 204, 217),
    "F.Silkscreen": (242, 237, 161),
    "B.Silkscreen": (232, 178, 167),
    "F.Mask": (216, 100, 255),
    "B.Mask": (2, 255, 238),
    "F.Paste": (180, 160, 154),
    "B.Paste": (0, 194, 194),
    "F.Fab": (175, 175, 175),
    "B.Fab": (88, 93, 132),
    "F.Courtyard": (255, 38, 226),
    "B.Courtyard": (38, 233, 255),
    "Edge.Cuts": (208, 210, 205),
    "Margin": (255, 38, 226),
    "User.Drawings": (194, 194, 194),
    "User.Comments": (89, 148, 220),
}
_HIGHLIGHT = [
    (255, 215, 0),
    (0, 229, 255),
    (255, 64, 129),
    (118, 255, 3),
    (255, 145, 0),
    (213, 0, 249),
    (29, 233, 182),
    (255, 255, 255),
]
_VIA_COLOUR = (220, 220, 220)
_HOLE_COLOUR = (8, 8, 10)
_RATSNEST_COLOUR = (255, 255, 255)
_MARKER_COLOUR = (255, 40, 40)
_GRID_COLOUR = (255, 255, 255, 38)

RENDER_PARAMS = ("region", "highlightNets", "scale", "showRatsnest", "showDrcMarkers")


def wants_native_render(params: Dict[str, Any]) -> bool:
    """True when ``get_board_2d_view`` should use this renderer, not kicad-cli."""
    return any(params.get(k) not in (None, False, []) for k in RENDER_PARAMS)


def _err(message: str, details: Optional[str] = None) -> Dict[str, Any]:
    return {"success": False, "message": message, "errorDetails": details or message}


def _hex(c: Sequence[int]) -> str:
    return "#%02x%02x%02x" % tuple(c[:3])


def _layer_colour(name: str) -> Tuple[int, int, int]:
    if name in _LAYER_COLOURS:
        return _LAYER_COLOURS[name]
    if name.endswith(".Cu"):
        h = sum(ord(ch) for ch in name)
        return (90 + h * 37 % 140, 90 + h * 53 % 140, 90 + h * 71 % 140)
    return (160, 160, 160)


def _grey(c: Sequence[int], level: float) -> Tuple[int, int, int]:
    g = int((0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]) * 0.35 + 255 * level * 0.3)
    return (g, g, g)


def _nice_step(span: float, target: int = 10) -> float:
    raw = span / max(1, target)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _parse_region(raw: Any) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[Dict]]:
    if raw is None:
        return None, None
    try:
        if isinstance(raw, dict):
            vals = [float(raw[k]) for k in ("x0", "y0", "x1", "y1")]
        else:
            vals = [float(v) for v in raw]
            if len(vals) != 4:
                raise ValueError
    except Exception:
        return None, _err("region must be {x0, y0, x1, y1} in mm")
    x0, y0, x1, y1 = vals
    rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
        return None, _err("region has zero area")
    return rect, None


class _Canvas:
    def __init__(self, rect: Sequence[float], scale: float, ss: int):
        from PIL import Image

        self.rect = rect
        self.scale = scale * ss
        self.ss = ss
        self.w = max(1, int(math.ceil((rect[2] - rect[0]) * scale)))
        self.h = max(1, int(math.ceil((rect[3] - rect[1]) * scale)))
        self.img = Image.new("RGBA", (self.w * ss, self.h * ss), BACKGROUND + (255,))

    def px(self, x: float, y: float) -> Tuple[float, float]:
        return (x - self.rect[0]) * self.scale, (y - self.rect[1]) * self.scale

    def layer(self):
        from PIL import Image, ImageDraw

        overlay = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        return overlay, ImageDraw.Draw(overlay)

    def commit(self, overlay: Any) -> None:
        from PIL import Image

        self.img = Image.alpha_composite(self.img, overlay)

    def polygon(self, draw: Any, pts: Sequence[Tuple[float, float]], fill: Tuple) -> None:
        if len(pts) >= 3:
            draw.polygon([self.px(x, y) for x, y in pts], fill=fill)

    def circle(self, draw: Any, x: float, y: float, r: float, fill: Tuple) -> None:
        cx, cy = self.px(x, y)
        rp = max(0.5, r * self.scale)
        draw.ellipse([cx - rp, cy - rp, cx + rp, cy + rp], fill=fill)


def render_board_view(default_board: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Render a cropped, optionally net-highlighted view. See the module docstring."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return _err("Pillow is required for region/highlight renders", "pip install Pillow")
    fmt = params.get("format", "png")
    if fmt not in ("png", "jpg"):
        return _err(
            f"format '{fmt}' is not available with region/highlightNets/scale",
            "Use png or jpg; svg is only produced by the whole-board kicad-cli render",
        )
    region, error = _parse_region(params.get("region"))
    if error:
        return error
    board, source, error = resolve_board(default_board, params, key="pcbPath")
    if error:
        return error
    try:
        return _render(board, source, params, region, fmt)
    except Exception as exc:
        logger.exception("region render failed")
        return _err("Failed to render the board view", str(exc))


def _render(board, source, params, region, fmt) -> Dict[str, Any]:
    from PIL import Image

    ln = LayerNames(board)
    warnings: List[str] = []
    if region is None:
        try:
            box = board.GetBoardEdgesBoundingBox()
            if box.GetWidth() <= 0:
                box = board.ComputeBoundingBox(False)
            x0, y0, x1, y1 = _box_mm(box)
        except Exception:
            return _err("Board has no outline; pass region {x0, y0, x1, y1}")
        region = (x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    w_mm, h_mm = region[2] - region[0], region[3] - region[1]

    scale = params.get("scale")
    if scale is not None:
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            return _err("scale must be a number (pixels per mm)")
        if scale <= 0:
            return _err("scale must be > 0")
    else:
        width = int(params.get("width") or 1600)
        height = int(params.get("height") or 1200)
        scale = min(width / w_mm, height / h_mm)
    if max(w_mm, h_mm) * scale > MAX_SIDE_PX:
        new_scale = MAX_SIDE_PX / max(w_mm, h_mm)
        warnings.append(
            f"scale {scale:g} px/mm would exceed {MAX_SIDE_PX} px; reduced to {new_scale:.2f}"
        )
        scale = new_scale

    names = params.get("layers")
    if names:
        layer_ids, error = ln.resolve(names)
        if error:
            return error
    else:
        layer_ids = set(ln.copper)
        edge = ln.lookup("Edge.Cuts")
        if edge is not None:
            layer_ids.add(edge)
    copper = [l for l in reversed(ln.copper) if l in layer_ids]  # bottom first
    back = [l for l in layer_ids if l not in ln.copper_set and ln.name(l).startswith("B.")]
    front = [l for l in layer_ids if l not in ln.copper_set and ln.name(l).startswith("F.")]
    rest = [l for l in layer_ids if l not in ln.copper_set and l not in back and l not in front]
    order = sorted(back) + copper + sorted(front) + sorted(rest)

    hl_names: List[str] = []
    if params.get("highlightNets"):
        hl_names, problems = resolve_net_names(board, list(params["highlightNets"]))
        warnings += problems
        if not hl_names:
            return _err("None of highlightNets was found", "; ".join(problems))
    hl_colour = {n: _HIGHLIGHT[i % len(_HIGHLIGHT)] for i, n in enumerate(hl_names)}
    highlighting = bool(hl_names)

    px_mm = 1.0 / scale
    max_err = max(0.002, px_mm / 3)
    ss = 2 if max(w_mm, h_mm) * scale <= 2400 else 1
    canvas = _Canvas(region, scale, ss)
    index = BoardIndex(board)
    footprints = index.footprints
    margin = 0.5
    view = (region[0] - margin, region[1] - margin, region[2] + margin, region[3] + margin)

    def visible(item: Any) -> bool:
        try:
            return _boxes_overlap(_box_mm(item.GetBoundingBox()), view)
        except Exception:
            return True

    tracks = [t for t in board.GetTracks() if visible(t)]
    pads = [(p, fp) for fp in footprints if visible(fp) for p in fp.Pads() if visible(p)]
    zones = [z for z in index.all_zones if not z.GetIsRuleArea() and visible(z)]
    drawings = [d for d in board.GetDrawings() if visible(d)]
    fp_graphics: List[Any] = []
    for fp in footprints:
        if not _boxes_overlap(_box_mm(fp.GetBoundingBox()), view):
            continue
        fp_graphics += [g for g in fp.GraphicalItems() if visible(g)]
        try:
            fp_graphics += [f for f in fp.GetFields() if f.IsVisible() and visible(f)]
        except Exception:
            fp_graphics += [fp.Reference(), fp.Value()]

    def base_colour(lname: str, idx: int) -> Tuple[int, int, int]:
        """The layer colour, or its grey when other nets are highlighted."""
        base = _layer_colour(lname)
        if not highlighting:
            return base
        return _grey(base, idx / max(1, len(order)))

    def draw_layer(lid: int, idx: int, highlight_pass: bool) -> int:
        lname = ln.name(lid)
        is_cu = lid in ln.copper_set
        overlay, draw = canvas.layer()
        drawn = 0
        depth = 1.0
        if highlight_pass and is_cu:
            pos = ln.stack_index.get(lid, 0)
            depth = 1.0 - 0.35 * pos / max(1, len(ln.copper) - 1)
        for z in zones:
            if not z.IsOnLayer(lid):
                continue
            net = z.GetNetname()
            is_hl = net in hl_colour
            if highlight_pass != is_hl:
                continue
            col = hl_colour[net] if is_hl else base_colour(lname, idx)
            col = tuple(int(c * depth) for c in col)
            alpha = 120 if not highlighting or is_hl else 70
            try:
                if not z.HasFilledPolysForLayer(lid):
                    continue
                for pts in poly_set_outlines(z.GetFilledPolysList(lid)):
                    canvas.polygon(draw, pts, col + (alpha,))
                    drawn += 1
            except Exception:
                continue
        canvas.commit(overlay)
        overlay, draw = canvas.layer()
        for t in tracks:
            if t.GetClass() == "PCB_VIA" or t.GetLayer() != lid:
                continue
            net = t.GetNetname()
            is_hl = net in hl_colour
            if highlight_pass != is_hl:
                continue
            col = hl_colour[net] if is_hl else base_colour(lname, idx)
            col = tuple(int(c * depth) for c in col)
            for pts in item_polygons(t, lid, max_err):
                canvas.polygon(draw, pts, col + (230 if not highlighting or is_hl else 150,))
                drawn += 1
        for pad, _fp in pads:
            if not pad.IsOnLayer(lid):
                continue
            net = pad.GetNetname() if is_cu else None
            is_hl = net in hl_colour
            if highlight_pass != is_hl:
                continue
            col = hl_colour[net] if is_hl else base_colour(lname, idx)
            col = tuple(int(c * depth) for c in col)
            for pts in item_polygons(pad, lid, max_err):
                canvas.polygon(draw, pts, col + (235 if not highlighting or is_hl else 160,))
                drawn += 1
        if not highlight_pass:
            for g in drawings + fp_graphics:
                cls = g.GetClass()
                if cls not in ("PCB_SHAPE", "PCB_TEXT", "PCB_TEXTBOX", "PCB_FIELD"):
                    continue
                if g.GetLayer() != lid:
                    continue
                col = base_colour(lname, idx)
                for pts in item_polygons(g, lid, max_err):
                    canvas.polygon(draw, pts, col + (220,))
                    drawn += 1
        canvas.commit(overlay)
        return drawn

    counts: Dict[str, int] = {}
    for idx, lid in enumerate(order):
        counts[ln.name(lid)] = draw_layer(lid, idx, False)

    # Vias and drill holes sit on top of all copper.
    copper_shown = bool(copper)
    hl_counts: Dict[str, int] = {n: 0 for n in hl_names}

    def draw_vias_and_holes(highlight_pass: bool) -> None:
        overlay, draw = canvas.layer()
        if copper_shown:
            for v in tracks:
                if v.GetClass() != "PCB_VIA":
                    continue
                span = ln.copper_of(v.GetLayerSet())
                if not any(l in layer_ids for l in span):
                    continue
                net = v.GetNetname()
                is_hl = net in hl_colour
                if highlight_pass != is_hl:
                    continue
                x, y = _xy(v.GetPosition())
                try:
                    r = v.GetWidth(v.TopLayer()) / 2e6
                except TypeError:
                    r = v.GetWidth() / 2e6
                col = (
                    hl_colour[net]
                    if is_hl
                    else (_grey(_VIA_COLOUR, 0.5) if highlighting else _VIA_COLOUR)
                )
                canvas.circle(draw, x, y, r, col + (255 if is_hl or not highlighting else 170,))
                canvas.circle(draw, x, y, v.GetDrillValue() / 2e6, _HOLE_COLOUR + (255,))
                if is_hl:
                    hl_counts[net] += 1
            for pad, _fp in pads:
                drill = pad.GetDrillSize()
                if drill.x <= 0:
                    continue
                net = pad.GetNetname()
                if highlight_pass != (net in hl_colour):
                    continue
                x, y = _xy(pad.GetPosition())
                canvas.circle(draw, x, y, min(drill.x, drill.y) / 2e6, _HOLE_COLOUR + (255,))
        canvas.commit(overlay)

    draw_vias_and_holes(False)
    if highlighting:
        for idx, lid in enumerate(copper):
            draw_layer(lid, idx, True)
        draw_vias_and_holes(True)
        for t in tracks:
            if t.GetClass() != "PCB_VIA" and t.GetNetname() in hl_counts:
                hl_counts[t.GetNetname()] += 1
        for pad, _fp in pads:
            if pad.GetNetname() in hl_counts:
                hl_counts[pad.GetNetname()] += 1

    view_info: Dict[str, Any] = {
        "renderer": "native (pcbnew geometry + Pillow)",
        "boardSource": source,
        "region": {
            "x0": _r(region[0]),
            "y0": _r(region[1]),
            "x1": _r(region[2]),
            "y1": _r(region[3]),
        },
        "pxPerMm": _r(scale, 3),
        "widthPx": canvas.w,
        "heightPx": canvas.h,
        "pixelMapping": "px = (x - x0) * pxPerMm, py = (y - y0) * pxPerMm (mm, KiCad board coordinates)",
        "layers": [ln.name(l) for l in order],
    }
    if hl_names:
        view_info["highlight"] = [
            {"net": n, "color": _hex(hl_colour[n]), "itemsInView": hl_counts[n]} for n in hl_names
        ]

    # Overlays: ratsnest gaps and DRC markers.
    overlay, draw = canvas.layer()
    lw = max(1, int(round(1.2 * ss)))
    rats_nets: List[str] = []
    show_rats = params.get("showRatsnest")
    if show_rats:
        if isinstance(show_rats, list):
            rats_nets, problems = resolve_net_names(board, show_rats)
            warnings += problems
        else:
            rats_nets = list(hl_names)
            if not rats_nets:
                warnings.append("showRatsnest: true needs highlightNets (or pass a list of nets)")
    if rats_nets:
        conn, error = connectivity(board)
        if error:
            warnings.append(error["message"])
        else:
            lines = []
            for net in rats_nets[:16]:
                code = index.netcode(net)
                if code is None:
                    continue
                islands, _info = find_islands(conn, index, ln, code)
                for gap in island_gaps(islands, ln):
                    a, b = gap["from"]["at"], gap["to"]["at"]
                    lines.append({"net": net, "from": a, "to": b, "gapMm": gap["gapMm"]})
                    col = hl_colour.get(net, _RATSNEST_COLOUR)
                    pa, pb = canvas.px(*a), canvas.px(*b)
                    draw.line([pa, pb], fill=_RATSNEST_COLOUR + (255,), width=lw)
                    for p in (pa, pb):
                        r = 3 * ss
                        draw.ellipse(
                            [p[0] - r, p[1] - r, p[0] + r, p[1] + r], outline=col + (255,), width=lw
                        )
            view_info["ratsnest"] = lines[:50]
            if not lines:
                view_info["ratsnestNote"] = "Every requested net is fully connected."
    if params.get("showDrcMarkers"):
        markers, note = _drc_markers(board)
        inside = [
            m
            for m in markers
            if region[0] <= m["at"][0] <= region[2] and region[1] <= m["at"][1] <= region[3]
        ]
        for m in inside:
            cx, cy = canvas.px(*m["at"])
            r = 5 * ss
            draw.ellipse(
                [cx - r, cy - r, cx + r, cy + r], outline=_MARKER_COLOUR + (255,), width=lw + 1
            )
            draw.line([cx - r, cy - r, cx + r, cy + r], fill=_MARKER_COLOUR + (255,), width=lw)
            draw.line([cx - r, cy + r, cx + r, cy - r], fill=_MARKER_COLOUR + (255,), width=lw)
        view_info["drcMarkers"] = {
            "inView": len(inside),
            "total": len(markers),
            "items": inside[:20],
        }
        if note:
            view_info["drcMarkers"]["note"] = note
    canvas.commit(overlay)

    img = canvas.img
    if ss > 1:
        img = img.resize((canvas.w, canvas.h), Image.LANCZOS)

    if params.get("grid", True) is not False:
        img = _draw_grid(img, region, scale)

    buf = io.BytesIO()
    if fmt == "jpg":
        img.convert("RGB").save(buf, format="JPEG", quality=90)
    else:
        img.convert("RGB").save(buf, format="PNG", optimize=True)
    data = buf.getvalue()

    result: Dict[str, Any] = {"success": True, "format": fmt, "view": view_info}
    if warnings:
        result["warnings"] = warnings
    if params.get("responseMode") == "file":
        path = _output_path(board, fmt)
        with open(path, "wb") as fh:
            fh.write(data)
        result["filePath"] = path
        result["message"] = f"2D view saved to {path}"
    else:
        result["imageData"] = base64.b64encode(data).decode("ascii")
    return result


def _draw_grid(img: Any, region: Sequence[float], scale: float) -> Any:
    from PIL import Image, ImageDraw, ImageFont

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    w_mm, h_mm = region[2] - region[0], region[3] - region[1]
    step = _nice_step(max(w_mm, h_mm), 10)
    label = (255, 255, 255, 200)
    x = math.ceil(region[0] / step) * step
    while x <= region[2] + 1e-9:
        px = (x - region[0]) * scale
        draw.line([(px, 0), (px, img.size[1])], fill=_GRID_COLOUR, width=1)
        draw.text((px + 2, 1), f"{x:g}", fill=label, font=font)
        x += step
    y = math.ceil(region[1] / step) * step
    while y <= region[3] + 1e-9:
        py = (y - region[1]) * scale
        draw.line([(0, py), (img.size[0], py)], fill=_GRID_COLOUR, width=1)
        draw.text((2, py + 1), f"{y:g}", fill=label, font=font)
        y += step
    return Image.alpha_composite(img.convert("RGBA"), overlay)


def _drc_markers(board: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Markers from the last run_drc report next to the board, plus in-memory markers."""
    markers: List[Dict[str, Any]] = []
    note = None
    path = None
    try:
        name = board.GetFileName()
    except Exception:
        name = None
    if name:
        base = os.path.splitext(os.path.basename(name))[0]
        path = os.path.join(os.path.dirname(name), f"{base}_drc_violations.json")
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for v in data.get("violations", []):
                loc = v.get("location") or {}
                if "x" in loc and "y" in loc:
                    markers.append(
                        {
                            "type": v.get("type", "unknown"),
                            "severity": v.get("severity"),
                            "at": [_r(float(loc["x"])), _r(float(loc["y"]))],
                        }
                    )
        except Exception as exc:
            note = f"Could not read {path}: {exc}"
    else:
        note = "No DRC report found next to the board; run run_drc first."
    try:
        for m in board.Markers():
            x, y = _xy(m.GetPosition())
            markers.append({"type": "marker", "at": [_r(x), _r(y)]})
    except Exception:
        pass
    if markers and note and note.startswith("No DRC report"):
        note = None
    return markers, note


def _output_path(board: Any, fmt: str) -> str:
    try:
        name = board.GetFileName()
    except Exception:
        name = None
    if name:
        base = os.path.splitext(os.path.basename(name))[0]
        return os.path.join(os.path.dirname(name), f"{base}_2d_view.{fmt}")
    return os.path.join(tempfile.mkdtemp(prefix="kicad_view_"), f"board_2d_view.{fmt}")
