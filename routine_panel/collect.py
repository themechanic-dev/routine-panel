"""One picture of every routine on this computer: the rows the window shows, grouped by where they come from.

Each row (an "item"):
    id, kind ("timer" | "claude" | "process"), name, about
    status   "running" | "waiting" | "fault" | "off";  since (running since), reason (why fault/off),
             attention (waits for the person, not a fault)
    schedule (words), next (epoch s or None), next_note, next_approx
    last     {start, end, result, ok} or None
    claude   whether it has a source of tokens;  tokens  {in, cache_read, out, …} | {"none_yet": true} | None
    lines    [{t, text}] the last lines of its log;  details  [[label, value], …]

Reading is cheap and repeated every two seconds; what costs (the journal, transcripts, logs) is read again only
when it has changed."""

from __future__ import annotations

import logging
import os
import re
import socket
import threading
import time
from pathlib import Path

from routine_panel import __version__, oshelp, systemd
from routine_panel.claude_routines import ClaudeRoutines
from routine_panel.processes import claude_app_running, command_lines, process_items, services_of, tail_lines
from routine_panel.settings import expand
from routine_panel.tokens import LogCalls, Transcripts, last_cycle

log = logging.getLogger("routine_panel")

CLAUDE_APP_NAMES = ("claude-desktop", "claude-desktop-bin", "Claude")
MIN_INTERVAL_S = 1.0
RUNNING_JOURNAL_EVERY_S = 10.0
_NOISE = re.compile(r"(: Consumed .* CPU time|^Starting |^Finished |^Started |^Stopped |: Deactivated successfully)")


class Collector:
    def __init__(self, settings: dict, theme: str | None = None, view: str | None = None):
        self.settings = settings
        self.theme = theme
        self.view = view
        home = Path.home()
        self.unit_dir = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "systemd" / "user"
        self.transcripts = Transcripts(expand(settings.get("claude_projects")) or home / ".claude" / "projects")
        self.claude = ClaudeRoutines(expand(settings.get("claude_sessions")) or claude_sessions_folder(),
                                     self.transcripts)
        self.tasks = None
        if oshelp.WINDOWS:
            from routine_panel.winsched import TaskScheduler
            self.tasks = TaskScheduler()
        self.log_calls = LogCalls()
        self._journal: dict[str, tuple[tuple, float, dict]] = {}
        self._tokens: dict[str, tuple[tuple, dict | None]] = {}
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._snapshot_at = 0.0

    # --- the picture ------------------------------------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            if self._snapshot is None or time.monotonic() - self._snapshot_at >= MIN_INTERVAL_S:
                self._snapshot = self.snapshot()
                self._snapshot_at = time.monotonic()
            return self._snapshot

    def snapshot(self) -> dict:
        started = time.monotonic()
        now = time.time()
        errors: list[str] = []
        processes = command_lines()
        app = claude_app_running(processes, CLAUDE_APP_NAMES)
        process_entries = self.settings.get("processes") or []

        timers: list[dict] = []
        services: dict = {}
        timers_title, timers_sub = "Timers", "systemd timers of your own"
        if self.tasks is not None:
            from routine_panel import winsched
            timers_title, timers_sub = "Scheduled tasks", "Task Scheduler, your own"
            data = self.tasks.data() or {}
            timers = winsched.items(data, bool(self.settings.get("show_system_timers")), now)
            for row in timers:
                self._task_tokens(row)
            app = winsched.claude_running(data)
            if self.tasks.error:
                errors.append(f"Task Scheduler could not be asked: {self.tasks.error}")
        elif systemd.available():
            try:
                timers, services = self.timer_items(now, services_of(process_entries))
            except systemd.SystemdError as e:
                errors.append(f"systemd could not be asked: {e}")
                log.warning("timers: %s", e)
        else:
            errors.append("This computer has no systemd: its timers are not shown")
        claude_items = []
        try:
            claude_items = self.claude.items(now, app)
        except Exception as e:  # noqa: BLE001 - the app's files are not ours: a change there must not end the panel
            log.exception("Claude routines")
            errors.append(f"The Claude app's routines could not be read: {e}")
        process_rows = process_items(process_entries, services, processes, now)

        sections = [
            {"id": "timers", "title": timers_title, "subtitle": timers_sub, "items": timers},
            {"id": "claude", "title": "Claude routines", "subtitle": "scheduled tasks of the Claude app",
             "items": claude_items},
            {"id": "processes", "title": "Always running", "subtitle": "programs named in the settings",
             "items": process_rows},
        ]
        hidden = set(self.settings.get("hidden") or [])
        labels = self.settings.get("labels") or {}
        counts = {"running": 0, "waiting": 0, "fault": 0, "off": 0}
        for section in sections:
            kept = []
            for item in section["items"]:
                if item["id"] in hidden:
                    continue
                label = labels.get(item["id"])
                if isinstance(label, dict):
                    item["name"] = str(label.get("name") or item["name"])
                    item["about"] = str(label.get("about") or item["about"])
                item["details"].insert(0, ["Panel id", item["id"]])
                counts[item["status"]] = counts.get(item["status"], 0) + 1
                kept.append(item)
            kept.sort(key=lambda i: i["name"].casefold())
            section["items"] = kept
        took = time.monotonic() - started
        if took > 1.0:
            log.info("reading took %.2f s", took)
        return {"now": now, "host": socket.gethostname(), "version": __version__, "theme": self.theme,
                "view": self.view,
                "claude_app": app, "counts": counts, "sections": sections, "errors": errors,
                "took_ms": round(took * 1000)}

    def _task_tokens(self, row: dict) -> None:
        """A Windows task that calls Claude itself and logs each call ("token_logs", by the task's name): the calls
        since its last start are its last cycle (Windows keeps no journal of runs to tell them apart)."""
        full = row["id"].removeprefix("task:")
        token_log = self._token_log(full.rsplit("\\", 1)[-1]) or self._token_log(full)
        if token_log is None:
            return
        row["claude"] = True
        path, pattern = token_log
        calls = self.log_calls.calls(path, pattern)
        start = (row.get("last") or {}).get("start")
        usage = last_cycle(calls, [start] if start else [])
        row["tokens"] = usage.as_json(live=row["status"] == "running", source="log") if usage else {"none_yet": True}
        row["log_lines"] = tail_lines(path, 8)

    # --- timers -----------------------------------------------------------------------------------------------
    def _own(self, values: dict) -> bool:
        if self.settings.get("show_system_timers"):
            return True
        fragment = systemd.one(values, "FragmentPath")
        if not fragment:
            return False
        return fragment.startswith(str(self.unit_dir) + "/") or "/systemd/transient/" in fragment

    def timer_items(self, now: float, extra_services: list[str]) -> tuple[list[dict], dict]:
        listed = {t["unit"]: t for t in systemd.list_timers()}
        names = set(listed)
        try:  # the user's timers that are not loaded (stopped and disabled) are not in list-timers
            names.update(p.name for p in self.unit_dir.glob("*.timer"))
        except OSError:
            pass
        timer_values = systemd.show(sorted(names), systemd.TIMER_PROPS)
        own = [unit for unit in sorted(names) if unit in timer_values and self._own(timer_values[unit])]
        service_of = {}
        for unit in own:
            target = (listed.get(unit) or {}).get("activates") or systemd.one(timer_values[unit], "Unit") or \
                unit.removesuffix(".timer") + ".service"
            service_of[unit] = target
        services = systemd.show(sorted(set(service_of.values()) | set(extra_services)), systemd.SERVICE_PROPS)
        items = []
        for unit in own:
            try:
                items.append(self._timer_item(unit, timer_values[unit], listed.get(unit), service_of[unit],
                                              services.get(service_of[unit], {}), now))
            except Exception:  # noqa: BLE001
                log.exception("timer %s", unit)
        return items, services

    def _timer_item(self, unit: str, tv: dict, listed: dict | None, service: str, sv: dict, now: float) -> dict:
        about = systemd.one(sv, "Description") or systemd.one(tv, "Description")
        item = {"id": f"timer:{unit}", "kind": "timer", "name": unit.removesuffix(".timer"), "about": about,
                "status": "waiting", "since": None, "reason": None, "attention": None, "claude": False,
                "schedule": systemd.describe_timer(tv), "next": (listed or {}).get("next"), "next_note": None,
                "next_approx": bool(systemd.span_seconds(systemd.one(tv, "RandomizedDelayUSec"))),
                "last": None, "tokens": None, "lines": [], "details": []}
        state = systemd.one(sv, "ActiveState")
        sub = systemd.one(sv, "SubState")
        running = state in ("activating", "deactivating", "reloading", "refreshing") or \
            (state == "active" and sub not in ("exited", ""))
        start = systemd.timestamp(sv, "ExecMainStartTimestamp")
        end = systemd.timestamp(sv, "ExecMainExitTimestamp")
        if end is not None and start is not None and end < start:
            end = None
        failure = systemd.failure(sv)

        journal = self._journal_info(service, start, end, state, running)
        if start is None and journal.get("start"):
            start, end = journal["start"], journal.get("end")
        if start is None and (listed or {}).get("last"):
            start = listed["last"]
        if start is not None:
            item["last"] = {"start": start, "end": None if running else end,
                            "result": journal.get("result") or (failure or ("running" if running else "finished")),
                            "ok": None if running else failure is None}
        item["lines"] = journal.get("lines", [])

        timer_state = systemd.one(tv, "ActiveState")
        file_state = systemd.one(tv, "UnitFileState")
        if running:
            item["status"] = "running"
            item["since"] = start
            if item["next"] is None:
                item["next_note"] = "after this run"
        elif timer_state != "active":
            item["status"] = "off"
            item["reason"] = "Timer disabled" if file_state in ("disabled", "masked") else "Timer stopped"
            item["next"] = None
        elif failure:
            item["status"] = "fault"
            item["reason"] = failure
        if item["status"] == "off" and failure:
            item["reason"] += f" · {failure.lower()}"

        token_log = self._token_log(service)
        if token_log is not None:
            item["claude"] = True
            item["tokens"] = self._log_tokens(service, token_log, running, start)
        extra = expand((self.settings.get("logs") or {}).get(service)) if isinstance(
            (self.settings.get("logs") or {}).get(service), str) else None
        if extra is None and token_log is not None:
            extra = token_log[0]
        if extra is not None:
            item["log_lines"] = tail_lines(extra, 8)
            item["details"].append(["Log", str(extra)])
        item["details"] = [["Source", "systemd timer (user)"], ["Timer", unit], ["Service", service],
                           ["Unit file", systemd.one(tv, "FragmentPath")],
                           ["Timer state", f"{timer_state or '?'} · {file_state or '?'}"],
                           ["Service state", f"{state or '?'} ({sub or '?'})"]] + item["details"]
        return item

    def _journal_info(self, service: str, start: float | None, end: float | None, state: str,
                      running: bool) -> dict:
        """The last run's lines from the journal, read again when the run changes (and every few seconds while
        it runs)."""
        key = (start, end, state)
        cached = self._journal.get(service)
        if cached and cached[0] == key and (not running or time.monotonic() - cached[1] < RUNNING_JOURNAL_EVERY_S):
            return cached[2]
        if start is not None:
            entries = systemd.journal(service, since=start - 1, lines=200)
        else:
            entries = systemd.journal(service, lines=60)
        runs = systemd.runs_from(entries)
        info: dict = {}
        if runs:
            run = runs[-1]
            info["start"], info["end"] = run.start, run.end
            own = [line for line in run.lines if line[1].strip()]
            if own:
                info["result"] = own[-1][1][:300]
            shown = [(t, text) for t, text in run.lines + run.system if not _NOISE.search(text)]
            shown.sort(key=lambda x: x[0])
            info["lines"] = [{"t": t, "text": text[:500]} for t, text in shown[-8:]]
        else:
            shown = [(e["t"], e["text"]) for e in entries if not e["job"] and not _NOISE.search(e["text"])]
            info["lines"] = [{"t": t, "text": text[:500]} for t, text in shown[-8:]]
        self._journal[service] = (key, time.monotonic(), info)
        return info

    def _token_log(self, service: str) -> tuple[Path, str | None] | None:
        entry = (self.settings.get("token_logs") or {}).get(service)
        if isinstance(entry, str):
            path = expand(entry)
            return (path, None) if path else None
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            path = expand(entry["path"])
            pattern = entry.get("pattern") if isinstance(entry.get("pattern"), str) else None
            return (path, pattern) if path else None
        return None

    def _log_tokens(self, service: str, token_log: tuple[Path, str | None], running: bool,
                    run_start: float | None) -> dict:
        path, pattern = token_log
        calls = self.log_calls.calls(path, pattern)
        if not calls:
            return {"none_yet": True}
        last = max(calls, key=lambda c: c.end)
        key = (len(calls), last.end, running, run_start)
        cached = self._tokens.get(service)
        if cached and cached[0] == key:
            return cached[1]
        entries = systemd.journal(service, since=last.start - 7200, until=last.end, lines=2000)
        starts = [run.start for run in systemd.runs_from(entries)]
        usage = last_cycle(calls, starts)
        live = bool(running and run_start is not None and last.end >= run_start - 1)
        result = usage.as_json(live=live, source="log") if usage else {"none_yet": True}
        self._tokens[service] = (key, result)
        return result


def claude_sessions_folder() -> Path:
    """Where the Claude desktop app keeps its sessions (and its routines): ~/.config/Claude on Linux; on Windows
    %APPDATA%\\Claude for the installer's app, or the Store app's own folder, which Windows moves to
    %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude (measured 2026-11-03)."""
    home = Path.home()
    if not oshelp.WINDOWS:
        return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude" / "claude-code-sessions"
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    classic = roaming / "Claude" / "claude-code-sessions"
    if classic.is_dir():
        return classic
    try:
        for package in sorted((local / "Packages").glob("Claude_*")):
            store = package / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions"
            if store.is_dir():
                return store
    except OSError:
        pass
    return classic

