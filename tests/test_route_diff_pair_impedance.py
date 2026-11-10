"""route_differential_pair sizes the pair for targetImpedance and reports impedance.

The board stub reads its stackup from a small .kicad_pcb and its net classes
from the .kicad_pro beside it, which is where the handler reads them on a real
board.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

pytestmark = pytest.mark.unit

from commands import routing as routing_mod  # noqa: E402
from commands.routing import RoutingCommands  # noqa: E402

from utils import impedance as imp  # noqa: E402

_PCB = """\
(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (setup
    (stackup
      (layer "F.Cu" (type "copper") (thickness 0.035))
      (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (epsilon_r 4.4))
      (layer "In1.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 2" (type "core") (thickness 1.065) (epsilon_r 4.6))
      (layer "In2.Cu" (type "copper") (thickness 0.0152))
      (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (epsilon_r 4.4))
      (layer "B.Cu" (type "copper") (thickness 0.035))
    )
  )
)
"""

_PRO = {
    "net_settings": {
        "classes": [
            {"name": "Default", "diff_pair_gap": 0.25, "diff_pair_width": 0.2},
            {"name": "USB", "diff_pair_gap": 0.15, "diff_pair_width": 0.3},
        ],
        "netclass_patterns": [{"pattern": "USB_*", "netclass": "USB"}],
    }
}


class _Vec:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Track:
    def __init__(self, board):
        self.start = self.end = self.net = self.width = None

    def SetStart(self, p):
        self.start = p

    def SetEnd(self, p):
        self.end = p

    def SetLayer(self, layer_id):
        pass

    def SetNet(self, net):
        self.net = net

    def SetWidth(self, width_nm):
        self.width = width_nm

    def GetWidth(self):
        return self.width


class _Nets(dict):
    def has_key(self, name):
        return name in self


class _NetInfo:
    def __init__(self, nets):
        self._nets = nets

    def NetsByName(self):
        return self._nets


class _Settings:
    m_TrackMinWidth = 100_000
    m_MinClearance = 100_000

    def GetCurrentTrackWidth(self):
        return 250_000


class _Board:
    def __init__(self, path):
        self.path = str(path)
        self.added = []
        self._info = _NetInfo(
            _Nets({"USB_DP": "net+", "USB_DM": "net-", "LVDS_P": "l+", "LVDS_N": "l-"})
        )

    def GetFileName(self):
        return self.path

    def GetCopperLayerCount(self):
        return 4

    def GetLayerID(self, name):
        return {"F.Cu": 0, "In1.Cu": 4, "In2.Cu": 6, "B.Cu": 2}.get(name, -1)

    def GetNetInfo(self):
        return self._info

    def GetDesignSettings(self):
        return _Settings()

    def Add(self, item):
        self.added.append(item)


@pytest.fixture
def board(monkeypatch, tmp_path):
    monkeypatch.setattr(routing_mod.pcbnew, "VECTOR2I", _Vec)
    monkeypatch.setattr(routing_mod.pcbnew, "PCB_TRACK", _Track)
    pcb = tmp_path / "pair.kicad_pcb"
    pcb.write_text(_PCB, encoding="utf-8")
    (tmp_path / "pair.kicad_pro").write_text(json.dumps(_PRO), encoding="utf-8")
    return _Board(pcb)


def _params(**overrides):
    params = {
        "startPos": {"x": 10, "y": 20, "unit": "mm"},
        "endPos": {"x": 30, "y": 20, "unit": "mm"},
        "layer": "F.Cu",
        "positiveNet": "USB_DP",
        "negativeNet": "USB_DM",
    }
    params.update(overrides)
    return params


def _spacing_mm(board):
    pos, neg = board.added
    return abs(pos.start.y - neg.start.y) / 1e6 - pos.width / 1e6


def test_target_solves_width_with_the_net_class_gap(board):
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=90))
    assert result["success"], result
    pair = result["diffPair"]
    assert pair["solvedFor"] == "width"
    assert pair["gap"] == 0.15  # USB class via the USB_* pattern
    assert pair["targetImpedance"] == 90
    assert result["impedance"]["zDiff"] == pytest.approx(90, abs=0.3)
    # what was placed is what was reported
    pos, neg = board.added
    assert pos.width / 1e6 == pytest.approx(pair["width"])
    assert _spacing_mm(board) == pytest.approx(0.15, abs=1e-6)
    expected = imp.coupled_microstrip(pair["width"], 0.15, 0.2104, 0.035, 4.4)["zDiff"]
    assert result["impedance"]["zDiff"] == pytest.approx(expected, abs=0.01)
    assert result["impedance"]["stackupSource"] == "board"
    assert result["impedance"]["structure"] == "microstrip"
    assert "accuracyNote" in result["impedance"]
    assert "Zdiff" in result["message"]


def test_target_falls_back_to_the_default_class_gap(board):
    result = RoutingCommands(board).route_differential_pair(
        _params(positiveNet="LVDS_P", negativeNet="LVDS_N", targetImpedance=100)
    )
    assert result["success"], result
    assert result["diffPair"]["gap"] == 0.25
    assert result["impedance"]["zDiff"] == pytest.approx(100, abs=0.3)


def test_target_keeps_a_given_gap(board):
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=90, gap=0.2))
    assert result["success"], result
    assert result["diffPair"]["gap"] == 0.2
    assert result["diffPair"]["solvedFor"] == "width"


def test_target_keeps_a_given_width_and_solves_the_gap(board):
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=90, width=0.25))
    assert result["success"], result
    pair = result["diffPair"]
    assert pair["solvedFor"] == "gap"
    assert pair["width"] == pytest.approx(0.25)
    assert _spacing_mm(board) == pytest.approx(pair["gap"], abs=1e-6)
    assert result["impedance"]["zDiff"] == pytest.approx(90, abs=0.3)


def test_target_on_an_inner_layer_uses_stripline(board):
    result = RoutingCommands(board).route_differential_pair(
        _params(layer="In2.Cu", targetImpedance=90)
    )
    assert result["success"], result
    assert result["impedance"]["structure"] == "stripline"
    assert result["impedance"]["referencePlanes"] == ["In1.Cu", "B.Cu"]
    assert result["impedance"]["zDiff"] == pytest.approx(90, abs=0.3)


def test_width_and_gap_given_are_not_changed(board):
    result = RoutingCommands(board).route_differential_pair(
        _params(targetImpedance=90, width=0.2, gap=0.15)
    )
    assert result["success"], result
    assert "solvedFor" not in result["diffPair"]
    assert result["diffPair"]["width"] == pytest.approx(0.2)
    expected = imp.coupled_microstrip(0.2, 0.15, 0.2104, 0.035, 4.4)["zDiff"]
    assert result["impedance"]["zDiff"] == pytest.approx(expected, abs=0.01)


def test_impedance_is_reported_without_a_target(board):
    """Backwards compatible: same defaults (gap 0.2, current track width)."""
    result = RoutingCommands(board).route_differential_pair(_params())
    assert result["success"], result
    assert result["diffPair"]["gap"] == 0.2
    assert result["diffPair"]["width"] == pytest.approx(0.25)
    expected = imp.coupled_microstrip(0.25, 0.2, 0.2104, 0.035, 4.4)["zDiff"]
    assert result["impedance"]["zDiff"] == pytest.approx(expected, abs=0.01)
    assert "targetImpedance" not in result["diffPair"]


def test_unreachable_target_routes_nothing(board):
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=250, gap=0.15))
    assert not result["success"]
    assert result["message"] == "Target impedance is out of reach"
    assert board.added == []


@pytest.mark.parametrize("bad", [-90, 0, "ninety", True])
def test_invalid_target_is_refused(board, bad):
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=bad))
    assert not result["success"]
    assert "targetImpedance" in result["errorDetails"]
    assert board.added == []


def test_board_without_a_file_uses_the_default_stackup(board):
    board.path = ""
    result = RoutingCommands(board).route_differential_pair(_params(targetImpedance=90))
    assert result["success"], result
    assert result["impedance"]["stackupSource"] == "default"
    assert "JLC" in result["impedance"]["stackupNote"]
    # no project file, so the built-in default gap
    assert result["diffPair"]["gap"] == 0.2
