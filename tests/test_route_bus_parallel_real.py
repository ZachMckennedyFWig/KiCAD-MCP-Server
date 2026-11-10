"""Real-pcbnew tests for route_bus mode="parallel" (true parallel bundles).

A synthetic board checks the geometry contract (members side by side at the
pitch, per-member lengths, dry run, explicit order, sequential fallback) and
a copy of the CD_Mainboard test project checks a real I2S bundle end to end,
including kicad-cli DRC on the saved copy.

Requires KICAD_USE_REAL_PCBNEW=1::

    KICAD_USE_REAL_PCBNEW=1 python -m pytest tests/test_route_bus_parallel_real.py
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

pytestmark = [pytest.mark.integration, pytest.mark.real_pcbnew]

CD_BOARD = Path(
    os.environ.get(
        "KICAD_MCP_CD_MAINBOARD", r"D:\KiCadMCPTests\CD_Mainboard\CD_Mainboard.kicad_pcb"
    )
)
KICAD_CLI_CANDIDATES = [
    os.environ.get("KICAD_CLI", ""),
    r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
    "/usr/bin/kicad-cli",
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
]


@pytest.fixture(autouse=True)
def require_real_pcbnew() -> None:
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")


@pytest.fixture
def pcbnew():
    import pcbnew as real

    return real


def _footprint_dir(lib: str) -> Path:
    candidates = []
    env = os.environ.get("KICAD10_FOOTPRINT_DIR") or os.environ.get("KICAD9_FOOTPRINT_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path(r"C:\Program Files\KiCad\10.0\share\kicad\footprints"),
        Path("/usr/share/kicad/footprints"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints"),
    ]
    for base in candidates:
        if (base / f"{lib}.pretty").is_dir():
            return base / f"{lib}.pretty"
    pytest.skip(f"KiCad footprint library {lib} not found")


def _v(pcbnew, x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def _build(pcbnew, tmp_path, n=3):
    """50 x 40 mm board: n resistors stacked at the left, n at the lower right.

    Net Bk joins Rk.2 (left column, x = 10) to R(10+k).1 (right column,
    x = 40, 12 mm lower), so the bundle has to bend.
    """
    from utils.footprint_swap import load_library_footprint

    lib = str(_footprint_dir("Resistor_SMD"))
    board = pcbnew.BOARD()
    for x1, y1, x2, y2 in [(0, 0, 50, 0), (50, 0, 50, 40), (50, 40, 0, 40), (0, 40, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_v(pcbnew, x1, y1))
        seg.SetEnd(_v(pcbnew, x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)
    nets = {}
    for name in [f"B{k}" for k in range(n)] + ["GND"]:
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        nets[name] = item
    for k in range(n):
        for ref, x, y, padnets in (
            (f"R{k + 1}", 10, 8 + 2 * k, {"1": "GND", "2": f"B{k}"}),
            (f"R{k + 11}", 40, 20 + 2 * k, {"1": f"B{k}", "2": "GND"}),
        ):
            fp = load_library_footprint(lib, "R_0805_2012Metric")
            fp.SetReference(ref)
            fp.SetFPID(pcbnew.LIB_ID("Resistor_SMD", "R_0805_2012Metric"))
            fp.SetPosition(_v(pcbnew, x, y))
            board.Add(fp)
            for pad in fp.Pads():
                pad.SetNet(nets[padnets[pad.GetNumber()]])
    path = tmp_path / "bus.kicad_pcb"
    pcbnew.SaveBoard(str(path), board)
    return pcbnew.LoadBoard(str(path)), path


def _conns(n=3):
    return [
        {
            "net": f"B{k}",
            "from": {"ref": f"R{k + 1}", "pad": "2"},
            "to": {"ref": f"R{k + 11}", "pad": "1"},
        }
        for k in range(n)
    ]


def _net_polylines(board, net):
    from commands.bus_router import segments_to_polylines

    segs = []
    for t in board.GetTracks():
        if t.GetNetname() == net and t.GetClass() == "PCB_TRACK":
            a, b = t.GetStart(), t.GetEnd()
            segs.append(((a.x / 1e6, a.y / 1e6), (b.x / 1e6, b.y / 1e6)))
    return segments_to_polylines(segs)


def _routed_length(board, net):
    return sum(
        t.GetLength() / 1e6
        for t in board.GetTracks()
        if t.GetNetname() == net and t.GetClass() != "PCB_VIA"
    )


def _fraction_at_pitch(board, net_a, net_b, pitch, tol=0.01):
    """Share of net_a's routed length that runs at ``pitch`` from net_b."""
    from commands.bus_router import parallel_fraction, polyline_length

    pa, pb = _net_polylines(board, net_a), _net_polylines(board, net_b)
    total = sum(polyline_length(p) for p in pa)
    hit = 0.0
    for p in pa:
        hit += polyline_length(p) * max(parallel_fraction(p, q, pitch, tol=tol) for q in pb)
    return hit / total


def _kicad_cli():
    for c in KICAD_CLI_CANDIDATES:
        if c and Path(c).is_file():
            return c
    return None


def _drc(path: Path):
    cli = _kicad_cli()
    if cli is None:
        return None
    out = path.with_suffix(".drc.json")
    subprocess.run(
        [
            cli,
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-error",
            "--refill-zones",
            "-o",
            str(out),
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return json.loads(out.read_text(encoding="utf-8"))


# ----------------------------------------------------------- synthetic board


def test_parallel_bundle_runs_side_by_side_at_pitch(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, path = _build(pcbnew, tmp_path)
    rc = RouterCommands(board)
    pitch = 0.6
    t0 = time.time()
    out = rc.route_bus(
        {"connections": _conns(), "pitch": pitch, "width": 0.2, "layersAllowed": ["F.Cu"]}
    )
    seconds = time.time() - t0
    assert out["success"] is True, out
    assert out["modeUsed"] == "parallel", out.get("fallbackReason")
    assert out["written"] is True
    assert seconds < 30
    assert out["pitch"] == pitch
    assert out["layer"] == "F.Cu"
    # members ordered across the bundle; offsets centre-symmetric
    assert sorted(out["order"]) == ["B0", "B1", "B2"]
    offs = [r["offsetMm"] for r in out["results"]]
    assert offs == pytest.approx([-pitch, 0.0, pitch])
    # reported per-member lengths = copper on the board, spread = max - min
    for r in out["results"]:
        assert r["lengthMm"] == pytest.approx(_routed_length(board, r["net"]), abs=1e-3)
        assert r["lengthMm"] == pytest.approx(r["trunkLengthMm"] + sum(r["legLengthsMm"]), abs=3e-3)
        assert r["vias"] == 0
    lengths = out["lengths"]
    assert out["lengthSpreadMm"] == pytest.approx(
        max(lengths.values()) - min(lengths.values()), abs=1e-3
    )
    # the trunks are parallel at the pitch nearly everywhere, the whole
    # routes over most of their length
    for p in out["parallelism"]:
        assert p["trunkAtPitch"] >= 0.9, p
    order = out["order"]
    for a, b in zip(order, order[1:]):
        assert _fraction_at_pitch(board, a, b, pitch) >= 0.6
    # every pad pair is connected
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    for k in range(3):
        a = [p for p in board.FindFootprintByReference(f"R{k + 1}").Pads() if p.GetNumber() == "2"][
            0
        ]
        connected = {i.m_Uuid.AsString() for i in conn.GetConnectedItems(a)}
        b = [
            p for p in board.FindFootprintByReference(f"R{k + 11}").Pads() if p.GetNumber() == "1"
        ][0]
        assert b.m_Uuid.AsString() in connected
    # the saved board passes kicad-cli DRC
    pcbnew.SaveBoard(str(path), board)
    drc = _drc(path)
    if drc is not None:
        assert drc["violations"] == [], drc["violations"][:3]
        unconnected = json.dumps(drc.get("unconnected_items", []))
        assert '"B0"' not in unconnected and "[B0]" not in unconnected


def test_parallel_dry_run_and_explicit_order(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path)
    rc = RouterCommands(board)
    before = len(list(board.GetTracks()))
    dry = rc.route_bus(
        {
            "connections": _conns(),
            "pitch": 0.5,
            "layersAllowed": ["F.Cu"],
            "dryRun": True,
            "order": ["B2", "B1", "B0"],
        }
    )
    assert dry["success"] is True and dry["written"] is False
    assert len(list(board.GetTracks())) == before
    if dry["modeUsed"] == "parallel":
        assert dry["order"] == ["B2", "B1", "B0"]
        assert any("order" in n for n in dry["notes"])  # crossing at the ends
    bad = rc.route_bus({"connections": _conns(), "order": ["B0", "B1"], "layersAllowed": ["F.Cu"]})
    assert bad["success"] is False
    assert "order must list exactly" in bad["errorDetails"]
    assert rc.route_bus({"connections": _conns(), "mode": "diagonal"})["success"] is False
    assert rc.route_bus({"connections": _conns(), "pitch": -1})["success"] is False


def test_parallel_falls_back_to_sequential_and_says_why(pcbnew, tmp_path):
    from commands.astar_router import RouterCommands

    board, _ = _build(pcbnew, tmp_path)
    rc = RouterCommands(board)
    # breakout disks this large overlap: no room for a trunk between them
    out = rc.route_bus(
        {
            "connections": _conns(),
            "pitch": 0.6,
            "layersAllowed": ["F.Cu", "B.Cu"],
            "breakoutRadius": 20,
        }
    )
    assert out["success"] is True, out
    assert out["mode"] == "parallel"
    assert out["modeUsed"] == "sequential"
    assert "too close" in out["fallbackReason"]
    assert set(out["lengths"]) == {"B0", "B1", "B2"}
    assert out["lengthSpreadMm"] >= 0
    (tmp_path / "s").mkdir()
    board2, _ = _build(pcbnew, tmp_path / "s")
    res = RouterCommands(board2).route_bus(
        {"connections": _conns(), "mode": "sequential", "pitch": 0.6}
    )
    assert res["success"] is True and res["modeUsed"] == "sequential"
    assert "fallbackReason" not in res
    assert [r["net"] for r in res["results"]] == ["B0", "B1", "B2"]


# ------------------------------------------------------------- CD_Mainboard

I2S = ["I2S_BCLK", "I2S_LRCLK", "I2S_DOUT"]


@pytest.mark.parametrize(
    "case",
    [
        # trunk on F.Cu at 0.75 mm (layer changes would be allowed, none needed)
        {"pitch": 0.75, "layer": "F.Cu", "allowVias": True, "vias": 0},
        # trunk on B.Cu at 0.5 mm: one layer, the legs drop through vias
        {"pitch": 0.5, "layer": "B.Cu", "allowVias": True, "vias": None},
    ],
    ids=["fcu-0.75", "bcu-0.5"],
)
def test_cd_mainboard_i2s_bundle(pcbnew, tmp_path, case):
    if not CD_BOARD.is_file():
        pytest.skip(f"{CD_BOARD} not found")
    from commands.astar_router import RouterCommands
    from commands.board_edit import BoardEditCommands

    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        src = CD_BOARD.with_suffix(ext)
        if src.is_file():
            shutil.copy(src, tmp_path / ("cd" + ext))
    path = tmp_path / "cd.kicad_pcb"
    board = pcbnew.LoadBoard(str(path))
    # free the I2S group: the three bundle nets plus MCLK/DIN that share the
    # corridor to U15 (left unrouted here)
    rm = BoardEditCommands(board).unroute(
        {"nets": I2S + ["CODEC_MCLK", "I2S_DIN"], "includeLocked": True}
    )
    assert rm["success"] is True and rm["removed"]["count"] > 0
    rc = RouterCommands(board)
    t0 = time.time()
    out = rc.route_bus(
        {
            "connections": [{"net": n} for n in I2S],  # closest missing connection
            "pitch": case["pitch"],
            "layer": case["layer"],
            "allowVias": case["allowVias"],
            "layersAllowed": ["F.Cu", "B.Cu"],
        }
    )
    seconds = time.time() - t0
    assert out["success"] is True, out
    assert out["modeUsed"] == "parallel", out.get("fallbackReason")
    assert seconds < 60
    assert sorted(out["order"]) == sorted(I2S)
    froms = sorted(r["from"] for r in out["results"])
    assert froms == ["R32.2", "R33.2", "R34.2"]
    assert sorted(r["to"] for r in out["results"]) == ["U15.2", "U15.3", "U15.4"]
    if case["vias"] is not None:
        assert out["vias"] == case["vias"]
    for p in out["parallelism"]:
        assert p["trunkAtPitch"] >= 0.9, p
    order = out["order"]
    for a, b in zip(order, order[1:]):
        assert _fraction_at_pitch(board, a, b, case["pitch"]) >= 0.6
    for r in out["results"]:
        assert r["lengthMm"] == pytest.approx(_routed_length(board, r["net"]), abs=1e-3)
    assert out["lengthSpreadMm"] < 3.0
    pcbnew.SaveBoard(str(path), board)
    drc = _drc(path)
    if drc is None:
        pytest.skip("kicad-cli not found: DRC part skipped")
    assert drc["violations"] == [], drc["violations"][:3]
    open_nets = json.dumps(drc.get("unconnected_items", []))
    for n in I2S:
        assert f"[{n}]" not in open_nets
