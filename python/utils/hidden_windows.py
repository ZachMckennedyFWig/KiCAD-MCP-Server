"""Start child processes without a console window on Windows (on by default).

On Windows every console program the server starts (kicad-cli for DRC and
exports, Java for Freerouting, git, a nested Python) gets its own console
window when the starting process has none, which is the case under an MCP
client. Those windows flash up and take the focus while the user works.

``install()`` makes every ``subprocess.Popen`` in this process pass
``CREATE_NO_WINDOW``. The flag only affects console programs: KiCad's own
windows (``launch_kicad_ui``, the GUI driver) still appear. A process started
this way has no console, so a console program *it* starts would get a window
again; every Python process the server starts that launches others calls
``install()`` itself (the setting travels in the environment).

``KICAD_MCP_HIDE_CONSOLE_WINDOWS=0`` (or ``false``/``no``/``off``) turns it
off, for debugging a child program in its own window. Elsewhere than Windows
``install()`` does nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Mapping, Optional

ENV = "KICAD_MCP_HIDE_CONSOLE_WINDOWS"

_FALSE = {"0", "false", "no", "off"}

# Flags that ask for a console of the child's own: honour them as given.
_CONSOLE_FLAGS = 0x00000010 | 0x00000008  # CREATE_NEW_CONSOLE | DETACHED_PROCESS
CREATE_NO_WINDOW = 0x08000000

_installed = False


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether windows are hidden: yes unless the setting says 0/false/no/off."""
    value = (env if env is not None else os.environ).get(ENV, "")
    return value.strip().lower() not in _FALSE


def creationflags_for(flags: int) -> int:
    """``flags`` plus CREATE_NO_WINDOW, unless they already ask for a console."""
    if flags & _CONSOLE_FLAGS:
        return flags
    return flags | CREATE_NO_WINDOW


def install(env: Optional[Mapping[str, str]] = None, platform: str = sys.platform) -> bool:
    """Patch ``subprocess.Popen`` when enabled on Windows. Returns True if patched."""
    global _installed
    if _installed:
        return True
    if not platform.startswith("win") or not enabled(env):
        return False

    original_init = subprocess.Popen.__init__

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["creationflags"] = creationflags_for(int(kwargs.get("creationflags") or 0))
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__  # type: ignore[method-assign]
    _installed = True
    return True
