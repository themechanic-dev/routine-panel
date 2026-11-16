"""The little that differs between systems when the panel runs another program.

On Windows a program started from a windowed app opens a console window of its own for a moment (measured with other
apps of ours: windows that flash on the screen), so every program the panel runs there starts without one."""

from __future__ import annotations

import subprocess
import sys

WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def run(args: list[str], timeout: float = 10.0, input_text: str | None = None, env: dict | None = None,
        encoding: str = "utf-8") -> subprocess.CompletedProcess:
    """subprocess.run, quietly: output captured as text, no console window on Windows, stdin closed unless given."""
    kwargs = {}
    if WINDOWS:
        kwargs["creationflags"] = _NO_WINDOW
    return subprocess.run(args, capture_output=True, text=True, encoding=encoding, errors="replace", timeout=timeout,
                          check=False, env=env, input=input_text,
                          stdin=None if input_text is not None else subprocess.DEVNULL, **kwargs)


def popen_flags() -> dict:
    return {"creationflags": _NO_WINDOW} if WINDOWS else {}
