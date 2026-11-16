"""What systemd (the user's own manager, `systemctl --user`) and its journal say about timers and services.

Only questions: `list-timers`, `show` and `journalctl`. Nothing here starts, stops or changes a unit."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger("routine_panel")

TIMER_PROPS = ["Id", "Description", "ActiveState", "SubState", "UnitFileState", "FragmentPath", "Unit",
               "TimersCalendar", "TimersMonotonic", "RandomizedDelayUSec", "LastTriggerUSec", "Transient"]
SERVICE_PROPS = ["Id", "Description", "ActiveState", "SubState", "Result", "UnitFileState", "FragmentPath", "Type",
                 "ExecMainStartTimestamp", "ExecMainExitTimestamp", "ExecMainCode", "ExecMainStatus", "ExecMainPID",
                 "ActiveEnterTimestamp", "InactiveEnterTimestamp", "StateChangeTimestamp", "NRestarts", "Transient",
                 "LoadState"]
RUNNING_STATES = ("activating", "active", "reloading", "deactivating", "refreshing")
_FAILED_RESULTS = {
    "exit-code": "ended with an error",
    "signal": "was killed by a signal",
    "core-dump": "crashed",
    "watchdog": "stopped answering (watchdog)",
    "timeout": "ran past its time limit",
    "start-limit-hit": "failed to start too many times",
    "resources": "could not get what it needed to start",
    "oom-kill": "was ended for using too much memory",
    "exec-condition": "did not meet its start condition",
    "condition": "did not meet its start condition",
}


class SystemdError(Exception):
    pass


def available() -> bool:
    return shutil.which("systemctl") is not None


def _run(args: list[str], timeout: float = 10.0) -> str:
    try:
        answer = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                                stdin=subprocess.DEVNULL, env=_c_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise SystemdError(f"{args[0]}: {e}") from None
    if answer.returncode != 0 and not answer.stdout:
        raise SystemdError(f"{' '.join(args[:3])}: {answer.stderr.strip() or answer.returncode}")
    return answer.stdout


def _c_env() -> dict:
    import os
    env = dict(os.environ)
    env["LC_ALL"] = "C.UTF-8"  # English words and plain numbers, whatever the person's language
    env["SYSTEMD_PAGER"] = ""
    return env


# --- list-timers ----------------------------------------------------------------------------------------------
def list_timers() -> list[dict]:
    """Every timer of the user's manager: unit, activates, next and last (epoch seconds or None)."""
    text = _run(["systemctl", "--user", "list-timers", "--all", "--output=json", "--no-pager"])
    try:
        rows = json.loads(text or "[]")
    except ValueError as e:
        raise SystemdError(f"list-timers: {e}") from None
    timers = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("unit"):
            continue
        timers.append({"unit": row["unit"], "activates": row.get("activates") or "",
                       "next": _us(row.get("next")), "last": _us(row.get("last"))})
    return timers


def _us(value) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return value / 1_000_000
    return None


# --- show -----------------------------------------------------------------------------------------------------
def show(units: list[str], props: list[str]) -> dict[str, dict[str, list[str]]]:
    """`systemctl --user show` for several units at once: {unit: {property: [values]}}. A property can come more
    than once (a timer with two monotonic triggers), hence the lists. Timestamps come as "@<epoch seconds>"."""
    if not units:
        return {}
    text = _run(["systemctl", "--user", "show", "--timestamp=unix", "--no-pager", "-p", ",".join(props), "--", *units])
    return parse_show(text)


def parse_show(text: str) -> dict[str, dict[str, list[str]]]:
    units: dict[str, dict[str, list[str]]] = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        values: dict[str, list[str]] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values.setdefault(key, []).append(value)
        unit = (values.get("Id") or [""])[0]
        if unit:
            units[unit] = values
    return units


def one(values: dict[str, list[str]], key: str) -> str:
    found = values.get(key) or [""]
    return found[0]


def timestamp(values: dict[str, list[str]], key: str) -> float | None:
    """A "@<epoch>" timestamp property as epoch seconds; None when it is empty or zero."""
    text = one(values, key).strip()
    if text.startswith("@"):
        try:
            number = float(text[1:])
        except ValueError:
            return None
        return number if number > 0 else None
    return None


# --- schedules in words ---------------------------------------------------------------------------------------
_SPAN_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600, "hr": 3600, "d": 86400,
               "day": 86400, "days": 86400, "w": 604800, "week": 604800, "weeks": 604800, "M": 2629800,
               "month": 2629800, "y": 31557600, "year": 31557600}
_DAY_NAMES = {"Mon": "Mondays", "Tue": "Tuesdays", "Wed": "Wednesdays", "Thu": "Thursdays", "Fri": "Fridays",
              "Sat": "Saturdays", "Sun": "Sundays"}
_MONOTONIC_WORDS = {"OnActiveUSec": "after the timer starts", "OnBootUSec": "after boot",
                    "OnStartupUSec": "after login", "OnUnitActiveUSec": None, "OnUnitInactiveUSec": None}


def span_seconds(text: str) -> float | None:
    """A systemd time span ("5min", "1h 30min", "30s", "0") in seconds."""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():  # plain microseconds in some properties
        return int(text) / 1_000_000
    total = 0.0
    for number, unit in re.findall(r"([\d.]+)\s*([a-zA-Z]+)", text):
        factor = _SPAN_UNITS.get(unit) or _SPAN_UNITS.get(unit.lower())
        if factor is None:
            return None
        total += float(number) * factor
    return total


def say_span(seconds: float) -> str:
    seconds = round(seconds)
    if seconds % 86400 == 0 and seconds >= 86400:
        n = seconds // 86400
        return "day" if n == 1 else f"{n} days"
    if seconds % 3600 == 0 and seconds >= 3600:
        n = seconds // 3600
        return "hour" if n == 1 else f"{n} hours"
    if seconds % 60 == 0 and seconds >= 60:
        n = seconds // 60
        return "minute" if n == 1 else f"{n} minutes"
    return f"{seconds} seconds"


def describe_calendar(spec: str) -> str:
    """An OnCalendar= expression, normalised the way `systemctl show` gives it ("*-*-* *:00/15:00",
    "Mon *-*-* 11:00:00"), in a few words. Anything uncommon comes back as it is."""
    spec = spec.strip()
    m = re.fullmatch(r"(?:(?P<days>[A-Za-z,.]+) )?\*-\*-\* (?P<h>[\d*/]+):(?P<m>[\d*/]+):(?P<s>[\d*/]+)", spec)
    if not m:
        return spec
    days, hour, minute, second = m.group("days"), m.group("h"), m.group("m"), m.group("s")
    prefix = ""
    if days:
        names = [d for d in re.split(r"[,]", days) if d]
        if len(names) == 1 and names[0] in _DAY_NAMES:
            prefix = _DAY_NAMES[names[0]] + " "
        elif days in ("Mon..Fri",):
            prefix = "weekdays "
        else:
            return spec
    if hour == "*" and minute == "*" and second.isdigit():
        return f"{prefix}every minute at :{int(second):02d}" if int(second) else f"{prefix}every minute"
    if hour == "*" and minute.startswith(("*/", "00/", "0/")) and second in ("0", "00"):
        step = minute.split("/")[1]
        if step.isdigit():
            return f"{prefix}every {int(step)} minutes"
    if hour == "*" and minute.isdigit() and second in ("0", "00"):
        return f"{prefix}hourly at :{int(minute):02d}"
    if hour.isdigit() and minute.isdigit() and second in ("0", "00"):
        return f"{prefix or 'daily '}{int(hour):02d}:{int(minute):02d}"
    return spec


def describe_timer(values: dict[str, list[str]]) -> str:
    """A timer's schedule in words, from its TimersCalendar/TimersMonotonic properties and its random delay."""
    parts = []
    for entry in values.get("TimersCalendar") or []:
        m = re.search(r"OnCalendar=([^;]+?)\s*;", entry)
        if m:
            parts.append(describe_calendar(m.group(1)))
    repeating = []
    once = []
    for entry in values.get("TimersMonotonic") or []:
        m = re.search(r"(On\w+USec)=([^;]+?)\s*;", entry)
        if not m:
            continue
        seconds = span_seconds(m.group(2))
        if seconds is None:
            continue
        kind = m.group(1)
        if kind in ("OnUnitActiveUSec", "OnUnitInactiveUSec"):
            repeating.append(f"every {say_span(seconds)}")
        elif kind in _MONOTONIC_WORDS:
            once.append(f"{say_span(seconds)} {_MONOTONIC_WORDS[kind]}")
    parts.extend(repeating or once)
    text = " · ".join(parts) or "no schedule"
    delay = span_seconds(one(values, "RandomizedDelayUSec"))
    if delay:
        text += f" (+ up to {say_span(delay)} at random)"
    return text


# --- states -----------------------------------------------------------------------------------------------------
def failure(values: dict[str, list[str]]) -> str | None:
    """Why the service's last run counts as a fault, in words; None if it does not."""
    state = one(values, "ActiveState")
    result = one(values, "Result")
    if state != "failed" and result in ("", "success"):
        return None
    words = _FAILED_RESULTS.get(result, result or "failed")
    code = one(values, "ExecMainStatus")
    how = one(values, "ExecMainCode")
    detail = ""
    if result == "exit-code" and code not in ("", "0"):
        detail = f" (exit code {code})"
    elif result == "signal" and code:
        detail = f" (signal {code})"
    elif how == "2" and code:  # CLD_KILLED
        detail = f" (signal {code})"
    return f"Last run {words}{detail}"


# --- the journal ----------------------------------------------------------------------------------------------
@dataclass
class Run:
    start: float
    end: float | None = None
    lines: list[tuple[float, str]] = field(default_factory=list)  # the program's own lines
    system: list[tuple[float, str]] = field(default_factory=list)  # systemd's lines about it (failures)


def journal(unit: str, since: float | None = None, until: float | None = None, lines: int = 400) -> list[dict]:
    """The journal's entries for a user unit, oldest first, each {t, text, job, result, comm}."""
    if not shutil.which("journalctl"):
        return []
    args = ["journalctl", "--user", "-u", unit, "-o", "json", "--no-pager", "-n", str(lines),
            "--output-fields=MESSAGE,JOB_TYPE,JOB_RESULT,_COMM,SYSLOG_IDENTIFIER"]
    if since is not None:
        args.append(f"--since=@{int(since)}")
    if until is not None:
        args.append(f"--until=@{int(until) + 1}")
    try:
        text = _run(args, timeout=10)
    except SystemdError as e:
        log.debug("journal %s: %s", unit, e)
        return []
    entries = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # journald gives bytes as a list of numbers
            try:
                message = bytes(message).decode("utf-8", "replace")
            except (TypeError, ValueError):
                message = ""
        try:
            t = int(entry.get("__REALTIME_TIMESTAMP", "0")) / 1_000_000
        except ValueError:
            continue
        entries.append({"t": t, "text": str(message or ""), "job": entry.get("JOB_TYPE"),
                        "result": entry.get("JOB_RESULT"),
                        "comm": entry.get("_COMM") or entry.get("SYSLOG_IDENTIFIER") or ""})
    return entries


def runs_from(entries: list[dict]) -> list[Run]:
    """The runs of a oneshot service in its journal entries: each from a "Starting" job to the next."""
    runs: list[Run] = []
    for entry in entries:
        if entry["job"] == "start" and not entry["result"]:
            runs.append(Run(start=entry["t"]))
        elif not runs:
            continue
        elif entry["job"] == "start" and entry["result"]:
            runs[-1].end = entry["t"]
            if entry["result"] != "done":
                runs[-1].system.append((entry["t"], entry["text"]))
        elif entry["comm"] == "systemd":
            if re.search(r"(Failed|failed|Main process exited|Killing|timed out|Deactivated)", entry["text"]):
                runs[-1].system.append((entry["t"], entry["text"]))
        else:
            runs[-1].lines.append((entry["t"], entry["text"]))
    return runs


def own_lines(entries: list[dict]) -> list[tuple[float, str]]:
    """The program's own lines (not systemd's "Starting"/"Finished"), oldest first."""
    return [(e["t"], e["text"]) for e in entries if e["comm"] != "systemd" and not e["job"] and e["text"]]
