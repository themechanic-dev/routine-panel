"""Routine Panel's settings and folders.

Settings live in ~/.config/routine-panel/settings.json, all of them optional:

    screen              the monitor the window opens on (Voice Console's `desk` numbers); none = where the desktop puts it
    window_mode         "normal", "maximized" or "fullscreen": how the window opens the first time (after that, it
                        opens where and how it was when it closed)
    window_size         [width, height] of a new window
    port                the local server's port (127.0.0.1 only)
    chrome              where Google Chrome's program is, when it is not where Chrome installs itself
    hidden              ids to leave out, as the panel shows them in a row's details ("timer:backup.timer")
    show_system_timers  also show the timers that came with the system, not only your own (default false)
    labels              {"<id>": {"name": "...", "about": "..."}}: another name or line for a row
    token_logs          {"<unit>.service": "<log file>"} or {"<unit>.service": {"path": "...", "pattern": "<regex>"}}:
                        a program that calls Claude itself and writes each call to a log. The default pattern reads
                        lines such as "2026-11-16 12:13:40,815 ... asked claude-opus-5-5: 6058 tokens in, 3512 out".
                        A pattern of your own needs the named groups ts, input and output (model, cache_read,
                        cache_write and seconds are read when it has them).
    logs                {"<unit>.service": "<log file>"}: a log whose last lines a row shows, besides the journal's
    processes           [{"unit": "my-worker.service", "name": "...", "about": "...", "heartbeat": "<file>",
                          "every_s": 60, "log": "<file>"}]: programs that run all the time. "match" (text in the
                        command line) can stand for "unit" for a program that is not a systemd service.
    lan                 phones may also reach the service over the home network, not only Tailscale (default true)
    claude_sessions     the Claude app's folder of sessions (default ~/.config/Claude/claude-code-sessions)
    claude_projects     Claude Code's folder of transcripts (default ~/.claude/projects)

Paths may start with ~."""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from routine_panel import DEFAULT_PORT, POSIX_NAME, WINDOWS_NAME

log = logging.getLogger("routine_panel")

DEFAULTS: dict = {
    "screen": None,
    "window_mode": "normal",
    "window_size": [1640, 1000],
    "port": DEFAULT_PORT,
    "chrome": None,
    "hidden": [],
    "show_system_timers": False,
    "labels": {},
    "token_logs": {},
    "logs": {},
    "processes": [],
    "lan": True,
    "claude_sessions": None,
    "claude_projects": None,
}


@dataclass(frozen=True)
class Dirs:
    config: Path
    data: Path
    runtime: Path

    def make(self) -> "Dirs":
        for folder in (self.config, self.data, self.runtime):
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(folder, 0o700)
        return self


def app_dirs() -> Dirs:
    """Where Routine Panel keeps its files. Nothing is created: `.make()` does that.

    The runtime folder is short on purpose: Chrome keeps sockets under its temporary folder, and a long path makes it
    abort (measured 2026-11-06)."""
    if sys.platform.startswith("win"):
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return Dirs(roaming / WINDOWS_NAME, local / WINDOWS_NAME, Path(tempfile.gettempdir()) / WINDOWS_NAME)
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    runtime_base = os.environ.get("XDG_RUNTIME_DIR")
    runtime = Path(runtime_base) / POSIX_NAME if runtime_base else \
        Path(tempfile.gettempdir()) / f"{POSIX_NAME}-{os.getuid()}"
    return Dirs(config / POSIX_NAME, data / POSIX_NAME, runtime)


def expand(path: str | os.PathLike | None) -> Path | None:
    if path is None or str(path).strip() == "":
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def load(path: Path) -> dict:
    """The settings: the defaults, with what the file gives in place of them. A file that cannot be read leaves the
    defaults and says why in the log; a value of the wrong kind is left out the same way."""
    settings = copy.deepcopy(DEFAULTS)
    try:
        given = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return settings
    except (OSError, ValueError) as e:
        log.warning("settings not read (%s): %s", path, e)
        return settings
    if not isinstance(given, dict):
        log.warning("settings not read (%s): not a JSON object", path)
        return settings
    for key, value in given.items():
        if key.startswith("_"):
            continue  # room for comments: "_note": "..."
        if key not in DEFAULTS:
            log.warning("unknown setting %r left out", key)
            continue
        default = DEFAULTS[key]
        if default is not None and not isinstance(value, type(default)) and value is not None:
            log.warning("setting %r has the wrong kind of value: left at its default", key)
            continue
        settings[key] = value
    return settings
