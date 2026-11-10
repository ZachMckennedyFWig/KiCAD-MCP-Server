"""group_components puts footprints into a named PCB_GROUP.

The tool was advertised for a long time with no Python command behind it, so
every call answered "unknown command". The unit tests drive the command with
fakes; the real-pcbnew test round-trips the group through a saved .kicad_pcb.
"""

import os
import sys
from pathlib import Path
from typing import Any, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "python"))

from commands import component_groups  # noqa: E402
from commands.component_groups import group_components  # noqa: E402


class _FakeGroup:
    def __init__(self, board: Any = None) -> None:
        self.name = ""
        self.items: List[Any] = []

    def SetName(self, name: str) -> None:
        self.name = name

    def GetName(self) -> str:
        return self.name

    def AddItem(self, item: Any) -> None:
        if item.group is not None and item.group is not self:
            item.group.RemoveItem(item)
        if item not in self.items:
            self.items.append(item)
        item.group = self

    def RemoveItem(self, item: Any) -> None:
        self.items.remove(item)
        item.group = None

    def ContainsItem(self, item: Any) -> bool:
        return item in self.items

    def GetItems(self) -> List[Any]:
        return list(self.items)


class _FakeFootprint:
    def __init__(self, ref: str) -> None:
        self.ref = ref
        self.group: Optional[_FakeGroup] = None

    def GetReference(self) -> str:
        return self.ref

    def GetParentGroup(self) -> Optional[_FakeGroup]:
        return self.group


class _FakeBoard:
    def __init__(self, refs: List[str]) -> None:
        self.fps = {r: _FakeFootprint(r) for r in refs}
        self.groups: List[_FakeGroup] = []

    def FindFootprintByReference(self, ref: str) -> Optional[_FakeFootprint]:
        return self.fps.get(ref)

    def Groups(self) -> List[_FakeGroup]:
        return list(self.groups)

    def Add(self, item: Any) -> None:
        assert isinstance(item, _FakeGroup)
        self.groups.append(item)


@pytest.fixture
def fake_pcbnew(monkeypatch: Any) -> None:
    monkeypatch.setattr(component_groups.pcbnew, "PCB_GROUP", _FakeGroup, raising=False)


@pytest.mark.unit
def test_creates_named_group_with_members(fake_pcbnew: None) -> None:
    board = _FakeBoard(["U1", "C1", "C2"])
    result = group_components(board, {"references": ["U1", "C1"], "groupName": "PWR"})

    assert result["success"] is True
    assert result["group"] == {"name": "PWR", "created": True, "members": ["C1", "U1"]}
    assert len(board.groups) == 1 and board.groups[0].GetName() == "PWR"


@pytest.mark.unit
def test_existing_group_is_extended_not_duplicated(fake_pcbnew: None) -> None:
    board = _FakeBoard(["U1", "C1", "C2"])
    group_components(board, {"references": ["U1"], "groupName": "PWR"})
    result = group_components(board, {"references": ["U1", "C2"], "groupName": "PWR"})

    assert result["success"] is True
    assert result["group"]["created"] is False
    assert result["group"]["members"] == ["C2", "U1"]
    assert result["alreadyInGroup"] == ["U1"]
    assert len(board.groups) == 1


@pytest.mark.unit
def test_member_of_another_group_is_moved_and_reported(fake_pcbnew: None) -> None:
    board = _FakeBoard(["U1", "C1"])
    group_components(board, {"references": ["U1", "C1"], "groupName": "A"})
    result = group_components(board, {"references": ["C1"], "groupName": "B"})

    assert result["success"] is True
    assert result["movedFromOtherGroup"] == [{"reference": "C1", "fromGroup": "A"}]
    assert result["warnings"]
    a = next(g for g in board.groups if g.GetName() == "A")
    assert [i.ref for i in a.items] == ["U1"]


@pytest.mark.unit
def test_missing_reference_changes_nothing(fake_pcbnew: None) -> None:
    board = _FakeBoard(["U1"])
    result = group_components(board, {"references": ["U1", "R99"], "groupName": "X"})

    assert result["success"] is False
    assert result["missing"] == ["R99"]
    assert board.groups == []
    assert board.fps["U1"].group is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [{"groupName": "X"}, {"references": [], "groupName": "X"}, {"references": ["U1"]}],
)
def test_rejects_missing_parameters(fake_pcbnew: None, params: Any) -> None:
    assert group_components(_FakeBoard(["U1"]), params)["success"] is False


@pytest.mark.unit
def test_no_board_loaded() -> None:
    result = group_components(None, {"references": ["U1"], "groupName": "X"})
    assert result["success"] is False
    assert "No board" in result["message"]


@pytest.mark.unit
def test_routed_and_auto_saved() -> None:
    from unittest.mock import patch

    with patch("kicad_interface.USE_IPC_BACKEND", False):
        from kicad_interface import KiCADInterface

        assert "group_components" in KiCADInterface().command_routes
        assert "group_components" in KiCADInterface._BOARD_MUTATING_COMMANDS


@pytest.mark.integration
@pytest.mark.real_pcbnew
def test_real_pcbnew_group_survives_save(tmp_path: Path) -> None:
    if os.environ.get("KICAD_USE_REAL_PCBNEW") != "1":
        pytest.skip("requires KICAD_USE_REAL_PCBNEW=1")
    import pcbnew

    path = str(tmp_path / "g.kicad_pcb")
    board = pcbnew.NewBoard(path)
    for ref in ("U1", "C1", "C2"):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        board.Add(fp)

    first = group_components(board, {"references": ["U1", "C1"], "groupName": "A"})
    assert first["success"], first
    moved = group_components(board, {"references": ["C1", "C2"], "groupName": "B"})
    assert moved["success"], moved
    assert moved["movedFromOtherGroup"] == [{"reference": "C1", "fromGroup": "A"}]
    pcbnew.SaveBoard(path, board)

    reloaded = pcbnew.LoadBoard(path)
    groups = {
        g.GetName(): sorted(i.GetReference() for i in g.GetItems()) for g in reloaded.Groups()
    }
    assert groups == {"A": ["U1"], "B": ["C1", "C2"]}
