"""KICAD_MCP_HIDE_CONSOLE_WINDOWS: child processes without console windows (Windows)."""

import subprocess
import sys

import pytest

from utils import hidden_windows as hw

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
DETACHED_PROCESS = 0x00000008


def test_on_by_default():
    assert hw.enabled({}) is True
    assert hw.enabled({hw.ENV: ""}) is True


@pytest.mark.parametrize("value", ["1", "true", "YES", " on ", "anything"])
def test_enabled_values(value):
    assert hw.enabled({hw.ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "No", " off "])
def test_disabled_values(value):
    assert hw.enabled({hw.ENV: value}) is False


def test_creationflags_add_no_window_but_keep_explicit_consoles():
    assert hw.creationflags_for(0) == CREATE_NO_WINDOW
    group = 0x00000200  # CREATE_NEW_PROCESS_GROUP (how launch_kicad_ui starts KiCad)
    assert hw.creationflags_for(group) == group | CREATE_NO_WINDOW
    assert hw.creationflags_for(CREATE_NEW_CONSOLE) == CREATE_NEW_CONSOLE
    assert hw.creationflags_for(DETACHED_PROCESS) == DETACHED_PROCESS


def test_install_does_nothing_when_off_or_not_windows(monkeypatch):
    monkeypatch.setattr(hw, "_installed", False)
    original = subprocess.Popen.__init__
    assert hw.install(env={hw.ENV: "0"}, platform="win32") is False
    assert hw.install(env={}, platform="linux") is False
    assert subprocess.Popen.__init__ is original


def test_install_passes_create_no_window_to_every_popen(monkeypatch):
    seen = {}

    def recorder(self, *args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("recorded")  # never start a process here

    monkeypatch.setattr(subprocess.Popen, "__init__", recorder)
    monkeypatch.setattr(hw, "_installed", False)
    assert hw.install(env={}, platform="win32") is True
    with pytest.raises(RuntimeError):
        subprocess.Popen(["kicad-cli", "version"], creationflags=0x00000200)
    assert seen["creationflags"] == 0x00000200 | CREATE_NO_WINDOW
    with pytest.raises(RuntimeError):
        subprocess.run(["kicad-cli", "version"])
    assert seen["creationflags"] == CREATE_NO_WINDOW


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation flags")
def test_the_test_session_starts_real_processes_without_a_window(monkeypatch):
    """conftest.py hides the suite's own windows: CreateProcess really gets the flag.

    Whether a window appeared cannot be observed from a non-interactive session,
    so this checks the flags handed to the Windows API, which decide it.
    """
    import _winapi

    seen = []
    real_create = _winapi.CreateProcess

    def spy(*args):
        seen.append(args[5])  # creationflags
        return real_create(*args)

    monkeypatch.setattr(_winapi, "CreateProcess", spy)
    out = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
    assert out.stdout.strip() == "ok"
    assert seen and seen[-1] & CREATE_NO_WINDOW
