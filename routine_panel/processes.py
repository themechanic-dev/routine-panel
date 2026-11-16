"""Programs that run all the time (named in the settings), and a look at the computer's processes.

A program here is running while its service (or, without one, a process whose command line has the given text) is
up and, when it has a heartbeat file, that file has changed within three of its rounds."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from routine_panel import systemd
from routine_panel.settings import expand

log = logging.getLogger("routine_panel")
STALE_ROUNDS = 3


def command_lines() -> list[tuple[int, list[str]]] | None:
    """(pid, argv) of every process this user can see; None where /proc is not there."""
    if not sys.platform.startswith("linux"):
        return None
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        if raw:
            found.append((int(entry), [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]))
    return found


def claude_app_running(processes: list[tuple[int, list[str]]] | None, names: tuple[str, ...]) -> bool | None:
    """Whether the Claude desktop app's main process is up (not one of its helpers, which carry --type=); None
    when that cannot be told."""
    if processes is None:
        return None
    for _pid, argv in processes:
        if not argv:
            continue
        if len(argv) == 1 and " " in argv[0]:
            # Electron rewrites its command line into one string ("/usr/lib/claude-desktop/claude-desktop --startup",
            # measured 2026-11-16), so its words are split here; a path with spaces in it is then cut, which only
            # matters for the helpers this is meant to tell apart anyway.
            argv = argv[0].split(" ")
        program = os.path.basename(argv[0])
        if program in names and not any(a.startswith("--type=") for a in argv[1:]):
            return True
    return False


def process_items(entries: list, services: dict, processes: list | None, now: float) -> list[dict]:
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            items.append(_item(entry, services, processes, now))
        except Exception:
            log.exception("process %r could not be read", entry)
    return items


def services_of(entries: list) -> list[str]:
    return [e["unit"] for e in entries if isinstance(e, dict) and isinstance(e.get("unit"), str) and e["unit"]]


def _item(entry: dict, services: dict, processes: list | None, now: float) -> dict:
    unit = entry.get("unit") if isinstance(entry.get("unit"), str) else None
    match = entry.get("match") if isinstance(entry.get("match"), str) else None
    key = unit or match or "?"
    every = entry.get("every_s") if isinstance(entry.get("every_s"), (int, float)) and entry["every_s"] > 0 else None
    heartbeat = expand(entry.get("heartbeat"))
    item = {"id": f"process:{key}", "kind": "process", "name": str(entry.get("name") or key.removesuffix(".service")),
            "about": str(entry.get("about") or ""), "status": "running", "since": None, "reason": None,
            "attention": None, "claude": False, "schedule": "runs all the time", "next": None,
            "next_note": None, "next_approx": False, "last": None, "tokens": None, "lines": [], "details": []}
    if every:
        item["schedule"] = f"runs all the time · heartbeat every {_seconds_words(every)}"

    up = None
    if unit:
        values = services.get(unit)
        item["details"].append(["Service", unit])
        if values is None or systemd.one(values, "LoadState") == "not-found":
            up = False
            item["reason"] = "Its service is not running (not found)"
        else:
            state = systemd.one(values, "ActiveState")
            up = state in ("active", "activating", "reloading")
            item["since"] = systemd.timestamp(values, "ExecMainStartTimestamp") or \
                systemd.timestamp(values, "ActiveEnterTimestamp")
            if not item["about"]:
                item["about"] = systemd.one(values, "Description")
            if not up:
                why = systemd.failure(values)
                item["reason"] = why or f"Its service is {state or 'not running'}"
                ended = systemd.timestamp(values, "InactiveEnterTimestamp")
                if ended:
                    item["last"] = {"start": item["since"], "end": ended, "result": item["reason"], "ok": False}
                item["since"] = None
            pid = systemd.one(values, "ExecMainPID")
            if pid and pid != "0":
                item["details"].append(["Process id", pid])
    elif match:
        item["details"].append(["Command line has", match])
        hits = [pid for pid, argv in (processes or []) if match in " ".join(argv) and pid != os.getpid()]
        if processes is None:
            up = None
        else:
            up = bool(hits)
            if hits:
                item["since"] = _process_start(hits[0])
                item["details"].append(["Process id", str(hits[0])])
            else:
                item["reason"] = "No process with that command line"

    if heartbeat is not None:
        item["details"].append(["Heartbeat file", str(heartbeat)])
        try:
            beat = heartbeat.stat().st_mtime
        except OSError:
            beat = None
        if beat is None:
            if up:
                item["status"] = "fault"
                item["reason"] = "No heartbeat file"
        else:
            age = now - beat
            item["last"] = {"start": beat, "end": beat, "result": f"heartbeat", "ok": True}
            if every:
                item["next"] = beat + every
                item["next_note"] = "next heartbeat"
                if up and age > every * STALE_ROUNDS:
                    item["status"] = "fault"
                    item["reason"] = f"No heartbeat for {_seconds_words(age)}"
                    item["next"] = None
    if up is False:
        item["status"] = "fault"
    elif up is None and heartbeat is None:
        item["status"] = "waiting"
        item["reason"] = "Cannot tell whether it runs"

    log_file = expand(entry.get("log"))
    if log_file is not None:
        item["details"].append(["Log", str(log_file)])
        item["lines"] = [{"t": None, "text": line} for line in tail_lines(log_file, 6)]
    return item


def _process_start(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        ticks = int(fields[19])
        with open("/proc/uptime") as handle:
            uptime = float(handle.read().split()[0])
        return time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 16384))
            data = handle.read()
    except OSError:
        return []
    lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    if size > 16384 and lines:
        lines = lines[1:]
    return lines[-count:]


def _seconds_words(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 120:
        return f"{seconds} s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} days"
