"""Real-pcbnew tests for tune_length (serpentine length tuning).

Requires KICAD_USE_REAL_PCBNEW=1 and KiCad's stock Resistor_SMD library::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_length_tuning_real.py

The CD_Mainboard test also needs a copy of that project (env
KICAD_CD_MAINBOARD_DIR, default D:/KiCadMCPTests/CD_Mainboard) and runs
kicad-cli DRC on a temporary copy when kicad-cli is found. The original is
never modified.
"""

import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from .test_routing_workflows_real import _build, _items, _seg_dist, _track  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.real_pcbnew]


@pytest.fixture(autouse=True)
def require_real_pcbnew() -> None:
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")


@pytest.fixture
def pcbnew():
    import pcbnew as real

    return real


def _length(board, net):
    from commands.astar_router import RouterCommands

    return RouterCommands(board).length_match({"nets": [net]})["nets"][0]["lengthMm"]


def _snapshot(board):
    out = []
    for t in board.GetTracks():
        out.append(
            (
                t.m_Uuid.AsString(),
                t.GetNetname(),
                t.GetStart().x,
                t.GetStart().y,
                t.GetEnd().x,
                t.GetEnd().y,
            )
        )
    return sorted(out)


def _sig_board(pcbnew, tmp_path, walls=(14.0,)):
    """SIG routed straight R1.2 -> R2.1 on F.Cu (y = 15), OTHER walls above/below."""
    board, path = _build(pcbnew, tmp_path, gnd_zone=False)
    nets = {n: board.FindNet(n) for n in ("SIG", "OTHER")}
    a = b = None
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            ref = fp.GetReference()
            if (ref, pad.GetNumber()) == ("R1", "2"):
                a = (pad.GetPosition().x / 1e6, pad.GetPosition().y / 1e6)
            if (ref, pad.GetNumber()) == ("R2", "1"):
                b = (pad.GetPosition().x / 1e6, pad.GetPosition().y / 1e6)
    _track(pcbnew, board, nets["SIG"], a, b, pcbnew.F_Cu, width=0.25)
    for y in walls:
        _track(pcbnew, board, nets["OTHER"], (12, y), (28, y), pcbnew.F_Cu, width=0.25)
    return board, a, b


def _pieces(board, net):
    out = []
    for t in _items(board, net):
        s, e = t.GetStart(), t.GetEnd()
        out.append(((s.x / 1e6, s.y / 1e6), (e.x / 1e6, e.y / 1e6), t))
    return out


def _min_gap(board, net, other, width=0.25):
    """Smallest edge-to-edge gap between two nets' straight tracks (mm)."""
    from utils.copper_geometry import seg_seg_dist

    best = math.inf
    for p, q, _ in _pieces(board, net):
        for c, d, _ in _pieces(board, other):
            best = min(best, seg_seg_dist(p, q, c, d) - width)
    return best


# ---------------------------------------------------------------- synthetic


def test_tune_adds_exact_length_away_from_the_wall(pcbnew, tmp_path):
    from commands.length_tuning import LengthTuner

    board, a, b = _sig_board(pcbnew, tmp_path)
    before = _length(board, "SIG")
    tuner = LengthTuner(board)
    snap = _snapshot(board)

    dry = tuner.tune_length({"net": "SIG", "targetLength": before + 4.0, "dryRun": True})
    assert dry["success"] is True, dry
    assert dry["dryRun"] is True and dry["written"] is False
    assert dry["nets"][0]["status"] == "tuned"
    assert dry["nets"][0]["plannedMm"] == pytest.approx(before + 4.0, abs=1e-3)
    assert _snapshot(board) == snap  # dry run wrote nothing

    res = tuner.tune_length({"net": "SIG", "targetLength": before + 4.0, "locked": True})
    assert res["success"] is True and res["written"] is True, res
    row = res["nets"][0]
    assert row["status"] == "tuned" and row["meanders"] >= 2
    assert row["afterMm"] == pytest.approx(before + 4.0, abs=1e-3)
    assert _length(board, "SIG") == pytest.approx(before + 4.0, abs=1e-3)
    seg = row["segments"][0]
    assert seg["amplitudeMm"] <= 1.0 + 1e-9
    # the wall is at y = 14 (above); the meander goes below the track
    pieces = _pieces(board, "SIG")
    assert all(p[1] >= a[1] - 1e-6 and q[1] >= a[1] - 1e-6 for p, q, _ in pieces)
    assert any(p[1] > a[1] + 0.5 for p, _, _ in pieces)
    assert all(t.IsLocked() for _, _, t in pieces)
    # the chain is continuous from pad to pad: every interior end is shared
    ends = {}
    for p, q, _ in pieces:
        for pt in (p, q):
            key = (round(pt[0], 6), round(pt[1], 6))
            ends[key] = ends.get(key, 0) + 1
    odd = [k for k, n in ends.items() if n % 2]
    assert sorted(odd) == sorted(
        [(round(a[0], 6), round(a[1], 6)), (round(b[0], 6), round(b[1], 6))]
    )
    # chamfered corners: every piece is 0, 45 or 90 degrees
    for p, q, _ in pieces:
        dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
        assert dx < 1e-5 or dy < 1e-5 or abs(dx - dy) < 1e-5
    assert _min_gap(board, "SIG", "OTHER") >= 0.2 - 1e-4

    # already on target: nothing to do, no auto-save
    again = tuner.tune_length({"net": "SIG", "targetLength": before + 4.0})
    assert again["nets"][0]["status"] == "withinTolerance"
    assert again["written"] is False and again["_skipAutoSave"] is True


def test_amplitude_shrinks_between_two_walls(pcbnew, tmp_path):
    from commands.length_tuning import LengthTuner

    board, a, _ = _sig_board(pcbnew, tmp_path, walls=(14.0, 16.0))
    before = _length(board, "SIG")
    res = LengthTuner(board).tune_length(
        {
            "net": "SIG",
            "targetLength": before + 3.0,
            "amplitude": 1.5,
            "style": "accordion",
            "region": {"x1": 12.5, "y1": 10, "x2": 27.5, "y2": 20},
        }
    )
    assert res["success"] is True, res
    row = res["nets"][0]
    assert row["segments"] and row["segments"][0]["side"] == "both"
    # the region keeps the meanders between the walls (x 12..28, 1.0 mm away on
    # each side): the centre line may move 1.0 - 0.125 - 0.125 - 0.2 = 0.55 mm
    pts = [pt for p, q, _ in _pieces(board, "SIG") for pt in (p, q)]
    off = [(x, abs(y - a[1])) for x, y in pts if abs(y - a[1]) > 1e-6]
    assert off and all(12.0 <= x <= 28.0 for x, _ in off)
    assert 0.3 < max(d for _, d in off) <= 0.55 + 1e-3
    assert _length(board, "SIG") == pytest.approx(before + row["addedMm"], abs=1e-3)
    assert _min_gap(board, "SIG", "OTHER") >= 0.2 - 1e-4


def test_round_corners_write_arcs_and_count_their_length(pcbnew, tmp_path):
    from commands.length_tuning import LengthTuner

    board, _, _ = _sig_board(pcbnew, tmp_path)
    before = _length(board, "SIG")
    res = LengthTuner(board).tune_length(
        {"net": "SIG", "targetLength": before + 2.5, "style": "rounded"}
    )
    assert res["nets"][0]["status"] == "tuned", res
    arcs = [t for t in _items(board, "SIG") if t.GetClass() == "PCB_ARC"]
    assert arcs
    assert _length(board, "SIG") == pytest.approx(before + 2.5, abs=1e-3)


def test_write_failure_rolls_back(pcbnew, tmp_path, monkeypatch):
    from commands import length_tuning as lt

    board, _, _ = _sig_board(pcbnew, tmp_path)
    before = _length(board, "SIG")
    snap = _snapshot(board)

    def boom(board_, item):
        raise RuntimeError("detach failed")

    monkeypatch.setattr(lt, "detach_board_item", boom)
    res = lt.LengthTuner(board).tune_length({"net": "SIG", "targetLength": before + 3})
    assert res["success"] is False and "unchanged" in res["message"]
    assert _snapshot(board) == snap
    assert _length(board, "SIG") == pytest.approx(before)


def test_skips_and_input_errors(pcbnew, tmp_path):
    from commands.length_tuning import LengthTuner

    board, _, _ = _sig_board(pcbnew, tmp_path)
    tuner = LengthTuner(board)
    before = _length(board, "SIG")
    sig = _items(board, "SIG")[0]
    sig.SetLocked(True)
    locked = tuner.tune_length({"net": "SIG", "targetLength": before + 2, "dryRun": True})
    row = locked["nets"][0]
    assert row["status"] == "notTuned" and "locked" in row["skipped"][0]["reason"]
    allowed = tuner.tune_length(
        {"net": "SIG", "targetLength": before + 2, "dryRun": True, "allowLocked": True}
    )
    assert allowed["nets"][0]["status"] == "tuned"
    sig.SetLocked(False)

    too_long = tuner.tune_length({"net": "SIG", "targetLength": before - 5})
    assert too_long["nets"][0]["status"] == "tooLong" and too_long["written"] is False

    outside = tuner.tune_length(
        {
            "net": "SIG",
            "targetLength": before + 2,
            "dryRun": True,
            "region": {"x1": 0, "y1": 0, "x2": 5, "y2": 5},
        }
    )
    assert outside["nets"][0]["status"] == "notTuned"

    bad = tuner.tune_length({"net": "SIG", "targetLength": before + 2, "segments": ["nope"]})
    assert bad["success"] is False and "segments not found" in bad["errorDetails"]
    assert tuner.tune_length({"net": "NOPE", "targetLength": 3})["success"] is False
    assert tuner.tune_length({"net": "SIG"})["success"] is False


def test_routes_and_dispatcher_registration():
    from commands.routing_workflows import BOARD_MUTATING, routing_workflow_routes
    from kicad_interface import KiCADInterface

    routes = routing_workflow_routes(lambda: None)
    assert "tune_length" in routes and "tune_length" in BOARD_MUTATING
    assert "tune_length" in KiCADInterface._BOARD_MUTATING_COMMANDS
    assert routes["tune_length"]({"net": "SIG"})["message"] == "No board is loaded"


# ------------------------------------------------------------- CD_Mainboard


def _cd_mainboard_dir() -> Path:
    path = Path(os.environ.get("KICAD_CD_MAINBOARD_DIR", r"D:\KiCadMCPTests\CD_Mainboard"))
    if not (path / "CD_Mainboard.kicad_pcb").is_file():
        pytest.skip("CD_Mainboard test project not found")
    return path


def _kicad_cli():
    for cand in (
        os.environ.get("KICAD_CLI"),
        r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
        shutil.which("kicad-cli"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def _net_drc_violations(cli, pcb: Path, net: str):
    report = pcb.with_suffix(".drc.json")
    subprocess.run(
        [
            cli,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-all",
            "--refill-zones",
            "-o",
            str(report),
            str(pcb),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    tag = f"[{net}]"
    return sorted(
        (v.get("type"), tuple(sorted(i.get("description", "") for i in v.get("items", []))))
        for v in data.get("violations", [])
        if any(tag in i.get("description", "") for i in v.get("items", []))
    )


def test_cd_mainboard_tune_i2s_din(pcbnew, tmp_path):
    from commands.length_tuning import LengthTuner

    src = _cd_mainboard_dir()
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        if (src / f"CD_Mainboard{ext}").is_file():
            shutil.copy(src / f"CD_Mainboard{ext}", tmp_path / f"CD_Mainboard{ext}")
    pcb = tmp_path / "CD_Mainboard.kicad_pcb"
    net = "I2S_DIN"
    cli = _kicad_cli()
    base = _net_drc_violations(cli, pcb, net) if cli else None

    board = pcbnew.LoadBoard(str(pcb))
    before = _length(board, net)
    snap = _snapshot(board)
    tuner = LengthTuner(board)
    dry = tuner.tune_length({"net": net, "targetLength": before + 5.0, "dryRun": True})
    assert dry["nets"][0]["status"] == "tuned", dry
    assert _snapshot(board) == snap

    res = tuner.tune_length({"net": net, "targetLength": before + 5.0})
    assert res["success"] is True and res["written"] is True, res
    row = res["nets"][0]
    assert row["status"] == "tuned" and row["meanders"] > 0
    assert _length(board, net) == pytest.approx(before + 5.0, abs=2e-3)
    # only I2S_DIN changed
    others = [s for s in snap if s[1] != net]
    assert [s for s in _snapshot(board) if s[1] != net] == others
    pcbnew.SaveBoard(str(pcb), board)

    reloaded = pcbnew.LoadBoard(str(pcb))
    assert _length(reloaded, net) == pytest.approx(before + 5.0, abs=2e-3)
    if cli:
        assert _net_drc_violations(cli, pcb, net) == base
