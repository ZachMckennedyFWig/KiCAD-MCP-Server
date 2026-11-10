"""Geometry helpers for batch_connect's automatic wiring and label placement.

Kept out of schematic_batch.py so the connection logic can evolve without
touching the component-placement code there. All coordinates are schematic
millimetres, origin at the sheet's top-left corner, Y growing downwards.
"""

from pathlib import Path

from commands.wire_manager import WireManager

_GRID = 1.27  # 50-mil KiCad schematic grid (mm)


def _snap(val: float) -> float:
    """Round a coordinate to the nearest 50-mil (1.27mm) schematic grid point."""
    return round(round(val / _GRID) * _GRID, 4)


# Outward unit vector (schematic Y-down) for a pin/label orientation.
_DIRS = {0: (1.0, 0.0), 90: (0.0, -1.0), 180: (-1.0, 0.0), 270: (0.0, 1.0)}


def _orthogonal_route(start, orientation, end, grid=_GRID):
    """Points of an orthogonal wire from a pin at *start* to a facing label at *end*.

    The wire leaves the pin along its outward direction. When the two points
    line up a single straight segment is returned; otherwise a Z-shaped route
    (out, across at the grid-snapped midpoint, in) so the wire arrives at the
    label head-on, as eeschema's own two-segment drag would. Returns None when
    the label is not in front of the pin or there is no room for the jog — the
    caller then places a plain label instead of drawing a wire (#7: the old
    code drew a diagonal straight across neighbouring symbols).
    """
    d = _DIRS.get(int(round(orientation / 90.0) * 90) % 360)
    if d is None:
        return None
    sx, sy = round(float(start[0]), 4), round(float(start[1]), 4)
    ex, ey = round(float(end[0]), 4), round(float(end[1]), 4)
    along = (ex - sx) * d[0] + (ey - sy) * d[1]
    if along <= 1e-6:
        return None
    if d[0]:  # horizontal pin: jog vertically
        if abs(ey - sy) < 1e-6:
            return [[sx, sy], [ex, ey]]
        mid = _snap((sx + ex) / 2.0)
        if not (min(sx, ex) + 1e-6 < mid < max(sx, ex) - 1e-6):
            return None
        return [[sx, sy], [mid, sy], [mid, ey], [ex, ey]]
    if abs(ex - sx) < 1e-6:
        return [[sx, sy], [ex, ey]]
    mid = _snap((sy + ey) / 2.0)
    if not (min(sy, ey) + 1e-6 < mid < max(sy, ey) - 1e-6):
        return None
    return [[sx, sy], [sx, mid], [ex, mid], [ex, ey]]


def _sheet_geometry(sch_path):
    """Connection points, wire segments, symbol pin hulls and visible field boxes.

    Used to keep auto-drawn wires and offset labels off existing objects. All
    values are mm. Returns a dict, or an empty geometry when the file cannot be
    parsed (callers then fall back to the plain behaviour).
    """
    geom = {"points": set(), "wires": [], "hulls": [], "fields": []}
    try:
        import sexpdata
        from sexpdata import Symbol

        data = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
    except Exception:
        return geom

    def key(x, y):
        return (round(float(x), 3), round(float(y), 3))

    lib = [
        item
        for item in data
        if isinstance(item, list) and item and item[0] == Symbol("lib_symbols")
    ]
    for item in data:
        if not (isinstance(item, list) and item):
            continue
        tag = item[0]
        parsed = WireManager._parse_wire(item)
        if parsed is not None:
            (x1, y1), (x2, y2), _, _ = parsed
            geom["wires"].append(((x1, y1), (x2, y2)))
            geom["points"].add(key(x1, y1))
            geom["points"].add(key(x2, y2))
            continue
        if tag in (Symbol("label"), Symbol("global_label"), Symbol("hierarchical_label")):
            at = next(
                (p for p in item[1:] if isinstance(p, list) and p and p[0] == Symbol("at")), None
            )
            if at is not None and len(at) >= 3:
                geom["points"].add(key(at[1], at[2]))
            continue
        if tag != Symbol("symbol"):
            continue
        if not any(isinstance(p, list) and p and p[0] == Symbol("lib_id") for p in item[1:]):
            continue
        try:
            pins = WireManager._collect_pin_positions(lib + [item])
        except Exception:
            pins = []
        for px, py in pins:
            geom["points"].add(key(px, py))
        if len(pins) >= 2:
            xs = [p[0] for p in pins]
            ys = [p[1] for p in pins]
            x0, x1_, y0, y1_ = min(xs), max(xs), min(ys), max(ys)
            # A two-pin part's pin hull is a line; give it the body's width.
            if x1_ - x0 < 1e-6:
                x0, x1_ = x0 - 1.0, x1_ + 1.0
            if y1_ - y0 < 1e-6:
                y0, y1_ = y0 - 1.0, y1_ + 1.0
            geom["hulls"].append({"x_min": x0, "x_max": x1_, "y_min": y0, "y_max": y1_})
        ref_is_virtual = False
        for prop in item[1:]:
            if not (isinstance(prop, list) and len(prop) >= 3 and prop[0] == Symbol("property")):
                continue
            if str(prop[1]) == "Reference" and str(prop[2]).startswith("#"):
                ref_is_virtual = True
        if ref_is_virtual:
            continue
        for prop in item[1:]:
            if not (isinstance(prop, list) and len(prop) >= 3 and prop[0] == Symbol("property")):
                continue
            if str(prop[1]) not in ("Reference", "Value"):
                continue
            box = _field_bbox(prop)
            if box:
                geom["fields"].append(box)
    return geom


def _field_bbox(prop):
    """Approximate bbox (mm) of a visible symbol field, or None when hidden/empty."""
    from sexpdata import Symbol

    text = str(prop[2])
    if not text:
        return None

    def walk(node):
        for sub in node[1:] if isinstance(node, list) else []:
            yield sub
            if isinstance(sub, list):
                yield from walk(sub)

    at = None
    size = 1.27
    justify = set()
    for sub in walk(prop):
        if sub == Symbol("hide"):
            return None
        if isinstance(sub, list) and sub:
            if sub[0] == Symbol("hide") and (len(sub) < 2 or str(sub[1]) == "yes"):
                return None
            if sub[0] == Symbol("at") and at is None and len(sub) >= 3:
                at = sub
            elif sub[0] == Symbol("size") and len(sub) >= 2:
                try:
                    size = float(sub[1])
                except (TypeError, ValueError):
                    pass
            elif sub[0] == Symbol("justify"):
                justify |= {str(j) for j in sub[1:]}
    if at is None:
        return None
    x, y = float(at[1]), float(at[2])
    angle = int(round(float(at[3]) / 90.0) * 90) % 180 if len(at) > 3 else 0
    w = max(len(text), 1) * size * 0.8
    h = size
    if "left" in justify:
        lo, hi = 0.0, w
    elif "right" in justify:
        lo, hi = -w, 0.0
    else:
        lo, hi = -w / 2.0, w / 2.0
    if angle == 90:  # text runs up the page
        return {"x_min": x - h / 2, "x_max": x + h / 2, "y_min": y - hi, "y_max": y - lo}
    return {"x_min": x + lo, "x_max": x + hi, "y_min": y - h / 2, "y_max": y + h / 2}


def _boxes_overlap(a, b, margin=0.0):
    return (
        a["x_min"] - margin < b["x_max"]
        and a["x_max"] + margin > b["x_min"]
        and a["y_min"] - margin < b["y_max"]
        and a["y_max"] + margin > b["y_min"]
    )


def _segment_hits_box(p, q, box, eps=1e-6):
    """True when axis-aligned segment p-q passes through the open interior of *box*."""
    (x1, y1), (x2, y2) = p, q
    if abs(y1 - y2) < eps:
        return (
            box["y_min"] + eps < y1 < box["y_max"] - eps
            and max(min(x1, x2), box["x_min"]) < min(max(x1, x2), box["x_max"]) - eps
        )
    if abs(x1 - x2) < eps:
        return (
            box["x_min"] + eps < x1 < box["x_max"] - eps
            and max(min(y1, y2), box["y_min"]) < min(max(y1, y2), box["y_max"]) - eps
        )
    return True  # diagonal: never acceptable


def _route_conflict(geom, route):
    """Why *route* would short or cross something on the sheet, or None if clear.

    Interior route points must not land on an existing pin, wire end or label
    (that would join nets), no existing connection point may sit on a route
    segment's interior, no segment may run along an existing wire, and no
    segment may cross another symbol's pin hull (its body).
    """

    def key(pt):
        return (round(float(pt[0]), 3), round(float(pt[1]), 3))

    ends = {key(route[0]), key(route[-1])}
    for pt in route[1:-1]:
        if key(pt) in geom["points"]:
            return f"corner ({pt[0]}, {pt[1]}) lands on an existing connection"
    for p, q in zip(route, route[1:]):
        for cp in geom["points"]:
            if cp in ends:
                continue
            if WireManager._point_strictly_on_wire(cp[0], cp[1], p[0], p[1], q[0], q[1]):
                return f"segment passes over existing connection point {cp}"
        for a, b in geom["wires"]:
            same_h = abs(p[1] - q[1]) < 1e-6 and abs(a[1] - b[1]) < 1e-6 and abs(a[1] - p[1]) < 1e-6
            same_v = abs(p[0] - q[0]) < 1e-6 and abs(a[0] - b[0]) < 1e-6 and abs(a[0] - p[0]) < 1e-6
            if same_h:
                lo, hi = max(min(p[0], q[0]), min(a[0], b[0])), min(
                    max(p[0], q[0]), max(a[0], b[0])
                )
                if hi - lo > 1e-6:
                    return "segment overlaps an existing wire"
            if same_v:
                lo, hi = max(min(p[1], q[1]), min(a[1], b[1])), min(
                    max(p[1], q[1]), max(a[1], b[1])
                )
                if hi - lo > 1e-6:
                    return "segment overlaps an existing wire"
        for hull in geom["hulls"]:
            if _segment_hits_box(p, q, hull):
                return "segment crosses a symbol body"
    return None


def _label_bbox(x, y, orientation, text):
    from commands.schematic_declutter import _label_bbox as declutter_label_bbox

    return declutter_label_bbox(x, y, orientation, text)
