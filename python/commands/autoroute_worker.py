"""Child-process helper for the orchestrated ``autoroute`` pipeline.

The pipeline (``commands/autoroute_pipeline.py``) runs in a background thread
and must not call pcbnew there: SWIG/pcbnew is not thread-safe and the main
command loop keeps using its board. All pcbnew work on board *copies* is done
by this script in a separate Python process instead::

    python autoroute_worker.py export --board staged.kicad_pcb --dsn out.dsn --report info.json
    python autoroute_worker.py import --board staged.kicad_pcb --ses in.ses --out routed.kicad_pcb
                                     --report report.json [--import-nets nets.json]
                                     [--lock-all-existing] [--lock-routed]

Each subcommand writes a JSON report and exits 0 on success, 2 on a handled
failure (the report then has ``"ok": false`` and ``"error"``).
"""

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON_ROOT = os.path.dirname(_HERE)
if _PYTHON_ROOT not in sys.path:
    sys.path.insert(0, _PYTHON_ROOT)


def _mm(v: int) -> float:
    return v / 1_000_000.0


def _apply_project_classes(board: Any, board_path: str) -> Optional[str]:
    """Load the project's net classes into a headless board (#302)."""
    try:
        from utils.project_netclasses import apply_net_classes_to_board, load_project_net_classes

        pro = os.path.splitext(board_path)[0] + ".kicad_pro"
        settings = load_project_net_classes(pro)
        if settings:
            apply_net_classes_to_board(board, settings)
        return None
    except Exception as exc:  # best effort, like the in-process path
        return f"project net classes not applied: {exc}"


def _rule_areas(board: Any, pcbnew: Any) -> List[Dict[str, Any]]:
    """Board-level rule areas with their routing flags and bbox (mm)."""
    out = []
    for z in board.Zones():
        try:
            if not z.GetIsRuleArea():
                continue
            bb = z.GetBoundingBox()
            layers = [board.GetLayerName(lid) for lid in z.GetLayerSet().Seq()]
            out.append(
                {
                    "name": z.GetZoneName(),
                    "layers": layers,
                    "copperLayers": [
                        board.GetLayerName(lid)
                        for lid in z.GetLayerSet().Seq()
                        if pcbnew.IsCopperLayer(lid)
                    ],
                    "noTracks": bool(z.GetDoNotAllowTracks()),
                    "noVias": bool(z.GetDoNotAllowVias()),
                    "noPours": bool(z.GetDoNotAllowZoneFills()),
                    "noPads": bool(z.GetDoNotAllowPads()),
                    "noFootprints": bool(z.GetDoNotAllowFootprints()),
                    "bbox": [
                        _mm(bb.GetLeft()),
                        _mm(bb.GetTop()),
                        _mm(bb.GetRight()),
                        _mm(bb.GetBottom()),
                    ],
                }
            )
        except Exception:
            continue
    return out


def cmd_export(args: argparse.Namespace) -> Dict[str, Any]:
    import pcbnew

    board = pcbnew.LoadBoard(args.board)
    warning = _apply_project_classes(board, args.board)
    ok = pcbnew.ExportSpecctraDSN(board, args.dsn)
    if not (ok is True or (type(ok) is int and ok == 0)) or not os.path.isfile(args.dsn):
        return {"ok": False, "error": f"ExportSpecctraDSN returned {ok} for {args.board}"}
    ds = board.GetDesignSettings()
    nets = []
    netinfo = board.GetNetInfo()
    for code in range(netinfo.GetNetCount()):
        item = netinfo.GetNetItem(code)
        if item and item.GetNetname():
            nets.append(item.GetNetname())
    tracks = list(board.GetTracks())
    info = {
        "ok": True,
        "copperEdgeClearanceMm": _mm(ds.m_CopperEdgeClearance),
        "ruleAreas": _rule_areas(board, pcbnew),
        "nets": nets,
        "tracks": sum(1 for t in tracks if t.GetClass() != "PCB_VIA"),
        "vias": sum(1 for t in tracks if t.GetClass() == "PCB_VIA"),
        "lockedItems": sum(1 for t in tracks if t.IsLocked()),
    }
    if warning:
        info["warning"] = warning
    return info


def cmd_import(args: argparse.Namespace) -> Dict[str, Any]:
    import pcbnew

    board = pcbnew.LoadBoard(args.board)
    _apply_project_classes(board, args.board)
    import_nets: Optional[set] = None
    if args.import_nets:
        with open(args.import_nets, "r", encoding="utf-8") as fh:
            import_nets = set(json.load(fh))

    before = list(board.GetTracks())
    n_tracks_before = sum(1 for t in before if t.GetClass() != "PCB_VIA")
    n_vias_before = len(before) - n_tracks_before
    # KiCad's SES import deletes every unlocked track/via and re-creates the
    # SES wiring. Lock what must survive unchanged (other nets, and every
    # existing item when the DSN exported them as fixed), remember which of
    # those were unlocked, and restore that afterwards.
    temp_locked = set()
    preserved = set()
    for t in before:
        uid = t.m_Uuid.AsString()
        keep = args.lock_all_existing or t.IsLocked()
        if import_nets is not None and t.GetNetname() not in import_nets:
            keep = True
        if keep:
            preserved.add(uid)
            if not t.IsLocked():
                t.SetLocked(True)
                temp_locked.add(uid)

    ok = pcbnew.ImportSpecctraSES(board, args.ses)
    if not (ok is True or (type(ok) is int and ok == 0)):
        return {"ok": False, "error": f"ImportSpecctraSES returned {ok}"}

    added_tracks = added_vias = 0
    for t in board.GetTracks():
        uid = t.m_Uuid.AsString()
        if uid in temp_locked:
            t.SetLocked(False)
        elif uid not in preserved:
            t.SetLocked(bool(args.lock_routed))
            if t.GetClass() == "PCB_VIA":
                added_vias += 1
            else:
                added_tracks += 1

    t0 = time.time()
    fill_error = None
    try:
        filler = pcbnew.ZONE_FILLER(board)
        filler.Fill(board.Zones())
    except Exception as exc:
        fill_error = str(exc)
    fill_s = round(time.time() - t0, 1)
    board.BuildConnectivity()
    unconnected = board.GetConnectivity().GetUnconnectedCount(False)

    if not pcbnew.SaveBoard(args.out, board, True):
        return {"ok": False, "error": f"SaveBoard({args.out}) failed"}
    after = list(board.GetTracks())
    n_tracks_after = sum(1 for t in after if t.GetClass() != "PCB_VIA")
    report = {
        "ok": True,
        "tracksBefore": n_tracks_before,
        "viasBefore": n_vias_before,
        "tracksAfter": n_tracks_after,
        "viasAfter": len(after) - n_tracks_after,
        "routedItemsFromSes": {"tracks": added_tracks, "vias": added_vias},
        "preservedItems": len(preserved),
        "zoneFillSeconds": fill_s,
        "unconnectedAfterRefill": unconnected,
    }
    if fill_error:
        report["zoneFillError"] = fill_error
    return report


def cmd_unconnected(args: argparse.Namespace) -> Dict[str, Any]:
    """KiCad's own unconnected count after a zone refill (no kicad-cli needed)."""
    import pcbnew

    board = pcbnew.LoadBoard(args.board)
    try:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        pass
    board.BuildConnectivity()
    return {"ok": True, "unconnected": board.GetConnectivity().GetUnconnectedCount(False)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="autoroute_worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export")
    p.add_argument("--board", required=True)
    p.add_argument("--dsn", required=True)
    p.add_argument("--report", required=True)
    p = sub.add_parser("import")
    p.add_argument("--board", required=True)
    p.add_argument("--ses", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--import-nets", dest="import_nets")
    p.add_argument("--lock-all-existing", dest="lock_all_existing", action="store_true")
    p.add_argument("--lock-routed", dest="lock_routed", action="store_true")
    p = sub.add_parser("unconnected")
    p.add_argument("--board", required=True)
    p.add_argument("--report", required=True)
    args = parser.parse_args(argv)

    handler = {"export": cmd_export, "import": cmd_import, "unconnected": cmd_unconnected}[args.cmd]
    try:
        report = handler(args)
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
