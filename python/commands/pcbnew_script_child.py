"""Child process for ``run_pcbnew_script`` (MCP_ISSUES I3). Not imported by the server.

Usage: ``python pcbnew_script_child.py <request.json>``

The request names the board, the code, whether to save, and where to write
the result. The board is loaded fresh from disk in this process, so a script
can neither see nor corrupt the server's in-memory board, and a crash or hang
here never takes the server down.

The script runs with these names defined:

``board``            the loaded ``pcbnew.BOARD``
``pcbnew``           the module
``args``             the ``args`` object from the tool call
``mm(v)``/``to_mm(v)``   mm <-> internal units
``footprint(ref)``   footprint by reference (a real ``FOOTPRINT`` or an error)
``fpid_of(fp)``      ``"Lib:Name"`` of a footprint; falls back to the board file
                     text when ``GetFPID`` returns an unusable SwigPyObject
``load_footprint(spec)``  a new ``FOOTPRINT`` from ``"Lib:Name"`` or
                     ``"path.pretty:Name"``; specs in ``preloadFootprints`` were
                     loaded before ``LoadBoard`` (KiCad 10's IO plugin can return
                     bare SwigPyObjects after a board is loaded)
``place_footprint(spec, ref, x, y, rot=0, layer="F.Cu")``  load + position + add
``zone_layers(zone)``  layer names from the layer set (``GetLayerName`` lies for
                     a new zone)
``add_zone(net, layer, points, priority=0, clearance=None, min_width=None, name=None)``
``refill()``         refill every zone
``check(obj, cls)``  raise a clear TypeError when SWIG handed back the wrong type

Set ``result = ...`` (or ``return ...`` at top level) to return a JSON value.
"""

import contextlib
import io
import json
import os
import re
import sys
import time
import traceback

_MAX_TEXT = 50_000


def _truncate(text: str) -> str:
    if len(text) <= _MAX_TEXT:
        return text
    return text[:_MAX_TEXT] + f"\n... [{len(text) - _MAX_TEXT} more characters truncated]"


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json.dumps(value, default=str))


def main(request_path: str) -> int:
    with open(request_path, "r", encoding="utf-8") as fh:
        req = json.load(fh)
    out_path = req["resultPath"]
    result = {"ok": False, "stdout": "", "stderr": "", "saved": False}

    def finish(code: int) -> int:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, default=str)
        return code

    # Where pcbnew comes from, in order: the directory of the pcbnew the server
    # itself imported; this interpreter's own path; the server's sys.path; KiCad
    # path discovery. Discovery and the server's path may list an older KiCad
    # first (one whose DLLs do not load), so they are only fallbacks.
    server_path = [p for p in (req.get("sysPath") or []) if p]
    if server_path and server_path[0] not in sys.path:
        sys.path.insert(0, server_path[0])  # the server's python/ dir, for helpers
    pcbnew_dir = req.get("pcbnewDir")
    if pcbnew_dir and pcbnew_dir not in sys.path:
        sys.path.insert(0, pcbnew_dir)

    def _extend_with_server_path():
        for p in server_path[1:]:
            if p not in sys.path:
                sys.path.append(p)

    def _add_discovered():
        from utils.platform_helper import PlatformHelper

        PlatformHelper.add_kicad_to_python_path()

    pcbnew = None
    errors = []
    for prepare in (None, _extend_with_server_path, _add_discovered):
        try:
            if prepare:
                prepare()
            sys.modules.pop("pcbnew", None)
            import pcbnew  # noqa: F811

            break
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            pcbnew = None
    if pcbnew is None:
        result["error"] = "cannot import pcbnew in the script interpreter: " + " | ".join(errors)
        return finish(2)

    board_path = req["boardPath"]
    board_dir = os.path.dirname(board_path)

    # -- footprint loading (before LoadBoard: see MCP_ISSUES #5r) ------------- #
    lib_paths = {}

    def _lib_path(nick: str):
        if nick.endswith(".pretty") or os.path.isdir(nick):
            return nick
        if nick in lib_paths:
            return lib_paths[nick]
        path = None
        try:
            from commands.library import LibraryManager

            from pathlib import Path

            path = LibraryManager(project_path=Path(board_dir)).get_library_path(nick)
        except Exception:
            path = None
        lib_paths[nick] = path
        return path

    def _split_spec(spec: str):
        if ":" not in spec or (len(spec) > 2 and spec[1] == ":" and spec.count(":") == 1):
            raise ValueError(f"footprint spec must be 'Lib:Name' or 'path.pretty:Name': {spec}")
        lib, name = spec.rsplit(":", 1)
        return lib, name

    def _io_load(spec: str):
        lib, name = _split_spec(spec)
        path = _lib_path(lib)
        if not path:
            raise LookupError(f"footprint library '{lib}' not found in the fp-lib-tables")
        fp = pcbnew.PCB_IO_KICAD_SEXPR().FootprintLoad(path, name)
        if fp is None:
            raise LookupError(f"footprint '{name}' not found in {path}")
        return fp

    preloaded = {}
    preload_errors = {}
    for spec in req.get("preloadFootprints") or []:
        try:
            preloaded[spec] = _io_load(spec)
        except Exception as exc:
            preload_errors[spec] = str(exc)

    t0 = time.time()
    board = pcbnew.LoadBoard(board_path)
    if not isinstance(board, pcbnew.BOARD):
        result["error"] = f"LoadBoard returned {type(board).__name__}, not a BOARD"
        return finish(3)
    load_s = time.time() - t0

    # -- helpers -------------------------------------------------------------- #
    def check(obj, cls):
        if not isinstance(obj, cls):
            raise TypeError(
                f"expected {getattr(cls, '__name__', cls)}, got {type(obj).__name__} "
                "(a KiCad 10 SWIG call returned an unusable object)"
            )
        return obj

    def mm(value):
        return pcbnew.FromMM(float(value))

    def to_mm(value):
        return pcbnew.ToMM(value)

    _fpid_map = {}

    def _fpid_from_text():
        if _fpid_map:
            return _fpid_map
        with open(board_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for m in re.finditer(r'\(footprint\s+"((?:[^"\\]|\\.)*)"', text):
            u = re.compile(r'\(uuid\s+"?([0-9a-fA-F-]+)"?\)').search(text, m.end(), m.end() + 600)
            if u:
                _fpid_map[u.group(1)] = m.group(1)
        return _fpid_map

    def fpid_of(fp):
        check(fp, pcbnew.FOOTPRINT)
        try:
            value = fp.GetFPIDAsString()
            if isinstance(value, str) and value:
                return value
        except Exception:
            pass
        try:
            lib_id = fp.GetFPID()
            value = str(lib_id.Format())
            if value:
                return value
        except Exception:
            pass
        try:
            uuid = fp.m_Uuid.AsString()
        except Exception:
            uuid = None
        return _fpid_from_text().get(uuid, "")

    def footprint(ref):
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            raise LookupError(f"no footprint {ref} on the board")
        return check(fp, pcbnew.FOOTPRINT)

    def load_footprint(spec):
        if spec in preloaded:
            return check(pcbnew.FOOTPRINT(preloaded[spec]), pcbnew.FOOTPRINT)
        try:
            fp = _io_load(spec)
        except Exception as exc:
            raise LookupError(f"{exc}; preload_errors={preload_errors.get(spec)}")
        if not isinstance(fp, pcbnew.FOOTPRINT):
            raise TypeError(
                f"loading {spec} after LoadBoard returned {type(fp).__name__} (KiCad 10 SWIG "
                "trap): pass it in preloadFootprints so it is loaded before the board"
            )
        return fp

    def place_footprint(spec, ref, x, y, rot=0.0, layer="F.Cu"):
        fp = load_footprint(spec)
        fp.SetReference(ref)
        fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
        board.Add(fp)
        if layer in ("B.Cu", "Bottom", "bottom"):
            fp.Flip(fp.GetPosition(), False)
        fp.SetOrientationDegrees(float(rot))
        return fp

    def zone_layers(zone):
        check(zone, pcbnew.ZONE)
        return [board.GetLayerName(layer) for layer in zone.GetLayerSet().Seq()]

    def add_zone(net, layer, points, priority=0, clearance=None, min_width=None, name=None):
        zone = pcbnew.ZONE(board)
        layer_id = board.GetLayerID(layer)
        if layer_id < 0:
            raise ValueError(f"unknown layer {layer}")
        zone.SetLayer(layer_id)
        if net:
            item = board.FindNet(net)
            if item is None:
                raise LookupError(f"no net {net} on the board")
            zone.SetNet(item)
        outline = zone.Outline()
        idx = outline.NewOutline()
        if not isinstance(idx, int):
            # Seen in KiCad 10: NewOutline() returning a bare SwigPyObject.
            poly = pcbnew.SHAPE_POLY_SET()
            poly.NewOutline()
            for x, y in points:
                poly.Append(mm(x), mm(y))
            zone.SetOutline(poly)
        else:
            for x, y in points:
                outline.Append(mm(x), mm(y))
        zone.SetAssignedPriority(int(priority))
        if clearance is not None:
            zone.SetLocalClearance(mm(clearance))
        if min_width is not None:
            zone.SetMinThickness(mm(min_width))
        if name:
            zone.SetZoneName(name)
        board.Add(zone)
        return zone

    def refill():
        filler = pcbnew.ZONE_FILLER(board)
        return filler.Fill(board.Zones())

    namespace = {
        "__name__": "__mcp_script__",
        "board": board,
        "pcbnew": pcbnew,
        "args": req.get("args") or {},
        "mm": mm,
        "to_mm": to_mm,
        "footprint": footprint,
        "fpid_of": fpid_of,
        "load_footprint": load_footprint,
        "place_footprint": place_footprint,
        "zone_layers": zone_layers,
        "add_zone": add_zone,
        "refill": refill,
        "check": check,
        "result": None,
    }

    code = req["code"]
    wrapped = False
    try:
        compiled = compile(code, "<run_pcbnew_script>", "exec")
    except SyntaxError as exc:
        if "outside function" not in str(exc):
            result["error"] = f"SyntaxError: {exc}"
            return finish(4)
        body = "\n".join("    " + line for line in code.splitlines()) or "    pass"
        compiled = compile("def __mcp_main__():\n" + body + "\n", "<run_pcbnew_script>", "exec")
        wrapped = True

    stdout, stderr = io.StringIO(), io.StringIO()
    t1 = time.time()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compiled, namespace)
            value = namespace["__mcp_main__"]() if wrapped else namespace.get("result")
        result["ok"] = True
        result["returnValue"] = _jsonable(value)
    except BaseException as exc:  # noqa: BLE001 - report everything, incl. SystemExit
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = _truncate(traceback.format_exc())
    result["stdout"] = _truncate(stdout.getvalue())
    result["stderr"] = _truncate(stderr.getvalue())
    result["timing"] = {"loadSec": round(load_s, 3), "runSec": round(time.time() - t1, 3)}
    if preload_errors:
        result["preloadErrors"] = preload_errors

    if result["ok"] and req.get("save"):
        save_to = req["savePath"]
        try:
            try:
                ok = pcbnew.SaveBoard(save_to, board, True)
            except TypeError:
                ok = pcbnew.SaveBoard(save_to, board)
            result["saved"] = bool(ok is None or ok) and os.path.getsize(save_to) > 0
            if not result["saved"]:
                result["error"] = "pcbnew.SaveBoard reported failure"
        except Exception as exc:
            result["ok"] = False
            result["error"] = f"save failed: {type(exc).__name__}: {exc}"
    return finish(0 if result["ok"] else 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
