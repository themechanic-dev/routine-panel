"""Windows: the Task Scheduler's tasks, as the panel's rows, and whether the Claude app runs.

One PowerShell run (Windows PowerShell 5.1, which every Windows has) answers both, as JSON. It takes about a second,
so it runs in the background every few seconds and the panel shows the last answer; the first picture waits for it.
Only questions are asked: Get-ScheduledTask, Get-ScheduledTaskInfo, Get-Process. Nothing is started or changed.

Whose tasks: by default only the person's own (the task's author is this user), never those under \\Microsoft\\;
"show_system_timers" shows every task outside \\Microsoft\\."""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import re
import threading
import time

from routine_panel import oshelp

log = logging.getLogger("routine_panel")

REFRESH_S = 3.0
SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
function Iso($d) { if ($d -and $d.Year -gt 2000) { $d.ToUniversalTime().ToString('o') } else { $null } }
$tasks = foreach ($t in Get-ScheduledTask) {
  if ($t.TaskPath -like '\Microsoft\*') { continue }
  $i = $t | Get-ScheduledTaskInfo
  $triggers = foreach ($g in $t.Triggers) {
    [pscustomobject]@{
      kind = $g.CimClass.CimClassName; start = $g.StartBoundary; enabled = $g.Enabled
      days = $g.DaysInterval; weeks = $g.WeeksInterval; dow = $g.DaysOfWeek
      every = $g.Repetition.Interval; delay = $g.RandomDelay
    }
  }
  [pscustomobject]@{
    name = $t.TaskName; path = $t.TaskPath; state = [string]$t.State; about = $t.Description
    author = $t.Author; user = $t.Principal.UserId
    actions = @($t.Actions | ForEach-Object { (@($_.Execute, $_.Arguments) -join ' ').Trim() })
    triggers = @($triggers)
    last = $(Iso $i.LastRunTime); next = $(Iso $i.NextRunTime)
    result = $i.LastTaskResult; missed = $i.NumberOfMissedRuns
  }
}
$claude = @(Get-Process -Name claude -ErrorAction SilentlyContinue | ForEach-Object { $_.Path } | Where-Object { $_ })
[pscustomobject]@{ me = [Environment]::UserName; tasks = @($tasks); claude = $claude } | ConvertTo-Json -Depth 5 -Compress
"""

# LastTaskResult values that are states, not failures (winerror.h, SCHED_S_*)
_INFO = {
    0x41300: "ready", 0x41301: "running", 0x41302: "disabled", 0x41303: "has not run yet",
    0x41304: "no more runs", 0x41305: "not scheduled", 0x41306: "stopped before it finished",
    0x41307: "no triggers", 0x41308: "event trigger only",
}
_DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"]


class TaskScheduler:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._at = 0.0
        self._running = False
        self.error: str | None = None

    def data(self) -> dict | None:
        """The last answer; asks again in the background when it is older than a few seconds (the first time,
        waits for it)."""
        with self._lock:
            fresh = self._data is not None and time.monotonic() - self._at < REFRESH_S
            start = not fresh and not self._running
            if start:
                self._running = True
            first = self._data is None
        if start:
            if first:
                self._refresh()
            else:
                threading.Thread(target=self._refresh, name="task-scheduler", daemon=True).start()
        return self._data

    def _refresh(self) -> None:
        try:
            data = ask()
            with self._lock:
                self._data = data
                self._at = time.monotonic()
                self.error = None
        except Exception as e:  # noqa: BLE001 - the panel says what went wrong and keeps the last answer
            log.warning("task scheduler: %s", e)
            with self._lock:
                self.error = str(e)
                self._at = time.monotonic()
                if self._data is None:
                    self._data = {"me": "", "tasks": [], "claude": []}
        finally:
            with self._lock:
                self._running = False


def ask() -> dict:
    encoded = base64.b64encode(SCRIPT.encode("utf-16-le")).decode("ascii")
    answer = oshelp.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                         "-EncodedCommand", encoded], timeout=30)
    text = (answer.stdout or "").strip()
    if not text:
        raise RuntimeError(answer.stderr.strip()[:300] or f"PowerShell gave nothing (exit {answer.returncode})")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError("PowerShell's answer is not an object")
    for key in ("tasks", "claude"):  # ConvertTo-Json writes a list of one as the thing itself
        value = data.get(key)
        data[key] = [] if value is None else value if isinstance(value, list) else [value]
    return data


# --- rows -----------------------------------------------------------------------------------------------------------
def own(task: dict, me: str, show_all: bool) -> bool:
    if show_all:
        return True
    author = str(task.get("author") or "").strip().lower()
    me = (me or "").strip().lower()
    return bool(me) and (author == me or author.endswith("\\" + me))


def items(data: dict, show_all: bool, now: float) -> list[dict]:
    rows = []
    for task in data.get("tasks") or []:
        if isinstance(task, dict) and task.get("name") and own(task, data.get("me", ""), show_all):
            try:
                rows.append(item(task, now))
            except Exception:  # noqa: BLE001 - one odd task must not hide the others
                log.exception("task %s", task.get("name"))
    return rows


def item(task: dict, now: float) -> dict:
    path = str(task.get("path") or "\\")
    name = str(task["name"])
    full = path + name if path.endswith("\\") else path + "\\" + name
    folders = [part for part in path.split("\\") if part]
    shown = " › ".join(folders + [name])  # a task in a folder: "Routine Panel › Service"
    state = str(task.get("state") or "")
    result = task.get("result")
    result = int(result) if isinstance(result, (int, float)) else None
    last = _iso(task.get("last"))
    nxt = _iso(task.get("next"))
    triggers = [t for t in (task.get("triggers") or []) if isinstance(t, dict)]
    row = {"id": f"task:{full}", "kind": "timer", "name": shown, "about": str(task.get("about") or ""),
           "status": "waiting", "since": None, "reason": None, "attention": None, "claude": False,
           "schedule": describe(triggers), "next": nxt, "next_note": None,
           "next_approx": any(_span(t.get("delay")) for t in triggers), "last": None, "tokens": None,
           "lines": [], "details": []}
    words = None
    ok = None
    if result is not None:
        if result == 0:
            words, ok = "finished", True
        elif result in _INFO:
            words = _INFO[result]
        else:
            words, ok = f"ended with code 0x{result & 0xFFFFFFFF:08X}", False
    if last is not None and result != 0x41303:
        row["last"] = {"start": last, "end": None, "result": words or "", "ok": ok}
    if state == "Running":
        row["status"] = "running"
        row["since"] = last
        if row["last"]:
            row["last"]["ok"] = None
            row["last"]["result"] = "running"
        if nxt is None:
            row["next_note"] = "after this run"
    elif state == "Disabled":
        row["status"] = "off"
        row["reason"] = "Disabled in Task Scheduler"
        row["next"] = None
    elif ok is False:
        row["status"] = "fault"
        row["reason"] = f"Last run {words}"
    missed = task.get("missed")
    if isinstance(missed, int) and missed > 0 and row["status"] == "waiting":
        row["attention"] = f"{missed} missed run{'s' if missed != 1 else ''}"
    actions = [a for a in (task.get("actions") or []) if isinstance(a, str) and a]
    row["details"] = [["Source", "Task Scheduler"], ["Task", full], ["Runs", "; ".join(actions)],
                      ["Author", str(task.get("author") or "")], ["Runs as", str(task.get("user") or "")],
                      ["State", state or "?"],
                      ["Last result", f"0x{result & 0xFFFFFFFF:08X}" if result is not None else ""]]
    return row


def describe(triggers: list[dict]) -> str:
    parts = []
    delay = 0.0
    for t in triggers:
        if t.get("enabled") is False:
            continue
        kind = str(t.get("kind") or "")
        start = _local(t.get("start"))
        at = start.strftime("%H:%M") if start else ""
        every = _span(t.get("every"))
        delay = max(delay, _span(t.get("delay")) or 0.0)
        if kind == "MSFT_TaskDailyTrigger":
            days = t.get("days") if isinstance(t.get("days"), int) and t.get("days") > 0 else 1
            words = f"daily {at}" if days == 1 else f"every {days} days at {at}"
        elif kind == "MSFT_TaskWeeklyTrigger":
            mask = t.get("dow") if isinstance(t.get("dow"), int) else 0
            names = [_DAYS[i] for i in range(7) if mask & (1 << i)]
            if mask == 0b0111110:
                words = f"weekdays {at}"
            elif len(names) == 1:
                words = f"{names[0]} {at}"
            else:
                words = ", ".join(n[:3] for n in names) + f" {at}"
        elif kind == "MSFT_TaskTimeTrigger":
            words = f"once, {start.strftime('%d %b %Y %H:%M')}" if start and not every else ""
        elif kind == "MSFT_TaskLogonTrigger":
            words = "at log on"
        elif kind == "MSFT_TaskBootTrigger":
            words = "at startup"
        elif kind == "MSFT_TaskIdleTrigger":
            words = "when the computer is idle"
        elif kind == "MSFT_TaskEventTrigger":
            words = "on an event"
        elif kind == "MSFT_TaskSessionStateChangeTrigger":
            words = "on a session change"
        else:
            words = re.sub(r"^MSFT_Task|Trigger$", "", kind).lower() or "custom trigger"
        if every:
            repeat = f"every {_span_words(every)}"
            words = f"{words}, then {repeat}" if words else repeat
        if words:
            parts.append(words)
    text = " · ".join(parts) or "no schedule"
    if delay:
        text += f" (+ up to {_span_words(delay)} at random)"
    return text


def claude_running(data: dict) -> bool | None:
    """Whether the Claude desktop app runs: its program lives in WindowsApps (Store) or AnthropicClaude (installer);
    Claude Code's own claude.exe elsewhere does not count."""
    paths = data.get("claude")
    if paths is None:
        return None
    return any(isinstance(p, str) and ("windowsapps" in p.lower() or "anthropicclaude" in p.lower()) for p in paths)


# --- time -----------------------------------------------------------------------------------------------------------
def _iso(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    if moment.year < 2001:
        return None
    return moment.timestamp()


def _local(text) -> _dt.datetime | None:
    """A trigger's StartBoundary ("2026-11-16T11:00:00", maybe with an offset) as local time."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _span(text) -> float | None:
    """An ISO 8601 duration ("PT5M", "P1D", "PT1H30M") in seconds."""
    if not isinstance(text, str) or not text.startswith("P"):
        return None
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", text)
    if not m:
        return None
    d, h, mi, s = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    return total or None


def _span_words(seconds: float) -> str:
    seconds = int(round(seconds))
    for size, one, many in ((86400, "day", "days"), (3600, "hour", "hours"), (60, "minute", "minutes")):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return one if n == 1 else f"{n} {many}"
    return f"{seconds} seconds"
