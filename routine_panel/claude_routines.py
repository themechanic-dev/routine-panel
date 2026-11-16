"""The Claude app's routines (scheduled tasks) and their runs, read from the app's own files.

    <sessions>/<org>/<account>/scheduled-tasks.json   each routine: cronExpression, enabled, lastRunAt, lastScheduledFor
    <sessions>/<org>/<account>/local_<id>.json        each session; a routine's runs carry its scheduledTaskId

These are the app's inner files, not a published format: every field is read with care, and anything missing or of
another shape only leaves that part of the row empty. Nothing is ever written to them.

Measured 2026-11-16:
  * A routine starts a few minutes after its cron time, the same few minutes each time for one routine (±1 min):
    lastRunAt − lastScheduledFor. The next start is the next cron time plus that delay.
  * A run's file is written when it starts (createdAt = lastActivityAt = lastTurnEdgeAt, no latestUserFrameAt, no
    completedTurns) and not again until its turn ends (a routine measured 2026-11-16, 13:26:05 → 13:26:29: completedTurns 1,
    latestUserFrameAt, a later lastTurnEdgeAt, then postTurnSummary a few seconds after). Only its transcript grows
    in between. So a run is in progress while it has no completed turn, or a prompt newer than the end of its last
    turn, and no error came after it.
  * postTurnSummary.status_category: completed, review_ready, blocked (waits for the person: not a fault), or none
    with `error` set ("You've hit your 5-hour limit …")."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path

from routine_panel import cron
from routine_panel.tokens import Transcripts

log = logging.getLogger("routine_panel")

RUN_LONGEST_S = 45 * 60  # a run older than this is not shown as running, whatever its file says
MISSED_GRACE_S = 15 * 60  # a run this late after its usual start counts as missed
_RUN_FIELDS = ("sessionId", "scheduledTaskId", "createdAt", "lastActivityAt", "lastTurnEdgeAt", "latestUserFrameAt",
               "model", "completedTurns", "error", "errorAt", "cliSessionId", "lastAssistantCliSessionId", "title",
               "lastTurnCutShort", "isArchived")


class ClaudeRoutines:
    def __init__(self, sessions_root: Path, transcripts: Transcripts):
        self.root = Path(sessions_root)
        self.transcripts = transcripts
        self._records: dict[Path, tuple[tuple[int, int], dict | None]] = {}
        self._texts: dict[Path, tuple[tuple[int, int], object]] = {}

    # --- files --------------------------------------------------------------------------------------------------
    def task_files(self) -> list[Path]:
        try:
            return sorted(self.root.glob("*/*/scheduled-tasks.json"))
        except OSError:
            return []

    def _cached(self, path: Path, read):
        try:
            stat = path.stat()
        except OSError:
            self._texts.pop(path, None)
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._texts.get(path)
        if cached and cached[0] == key:
            return cached[1]
        value = read(path)
        self._texts[path] = (key, value)
        return value

    def _runs(self, folder: Path) -> dict[str, dict]:
        """The latest run of each routine in an account's folder: {task id: run record}."""
        latest: dict[str, dict] = {}
        seen = set()
        try:
            paths = list(folder.glob("local_*.json"))
        except OSError:
            paths = []
        for path in paths:
            seen.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            key = (stat.st_size, stat.st_mtime_ns)
            cached = self._records.get(path)
            if cached and cached[0] == key:
                record = cached[1]
            else:
                record = _read_run(path)
                self._records[path] = (key, record)
            if not record or not record.get("scheduledTaskId"):
                continue
            task = record["scheduledTaskId"]
            if task not in latest or _ms(record.get("createdAt")) > _ms(latest[task].get("createdAt")):
                latest[task] = record
        for gone in [p for p in self._records if p.parent == folder and p not in seen]:
            del self._records[gone]
        return latest

    # --- rows ---------------------------------------------------------------------------------------------------
    def items(self, now: float, app_running: bool | None) -> list[dict]:
        items = []
        for path in self.task_files():
            data = self._cached(path, _read_json)
            tasks = data.get("scheduledTasks") if isinstance(data, dict) else None
            if not isinstance(tasks, list):
                continue
            runs = self._runs(path.parent)
            for task in tasks:
                if isinstance(task, dict) and isinstance(task.get("id"), str):
                    try:
                        items.append(self._item(task, runs.get(task["id"]), now, app_running, path))
                    except Exception:  # one odd routine must not hide the others
                        log.exception("routine %s could not be read", task.get("id"))
        return items

    def _item(self, task: dict, run: dict | None, now: float, app_running: bool | None, source: Path) -> dict:
        task_id = task["id"]
        name = str(task.get("displayName") or task_id)
        skill = Path(str(task["filePath"])) if isinstance(task.get("filePath"), str) else None
        about = self._cached(skill, _skill_description) if skill else None
        enabled = task.get("enabled") is not False
        expression = str(task.get("cronExpression") or "")
        try:
            schedule = cron.parse(expression)
        except cron.CronError:
            schedule = None
        last_scheduled = _iso(task.get("lastScheduledFor"))
        last_fired = _iso(task.get("lastRunAt"))
        delay = None
        if last_scheduled is not None and last_fired is not None and 0 <= last_fired - last_scheduled < 6 * 3600:
            delay = last_fired - last_scheduled

        item = {"id": f"claude:{task_id}", "kind": "claude", "name": name, "about": about or "",
                "status": "waiting", "since": None, "reason": None, "attention": None, "claude": True,
                "schedule": _schedule_words(schedule, expression, delay), "next": None, "next_note": None,
                "next_approx": delay is not None, "last": None, "tokens": None, "lines": [], "details": []}

        # the last run
        start = end = None
        in_progress = False
        if run is not None:
            created = _ms(run.get("createdAt")) / 1000 or None
            start = (_ms(run.get("latestUserFrameAt")) / 1000) or created
            edge = _ms(run.get("lastTurnEdgeAt")) / 1000 or None
            error_at = _ms(run.get("errorAt")) / 1000 or None
            prompt = _ms(run.get("latestUserFrameAt")) / 1000 or None
            turn_open = not run.get("completedTurns") or (prompt is not None and (edge is None or edge < prompt))
            in_progress = (start is not None and turn_open and not (error_at and error_at >= start - 1)
                           and now - start < RUN_LONGEST_S)
            end = edge if edge and start and edge >= start else (error_at if error_at else None)
            summary = run.get("postTurnSummary") if isinstance(run.get("postTurnSummary"), dict) else {}
            error = run.get("error") if isinstance(run.get("error"), str) and error_at and error_at >= (start or 0) - 1 \
                else None
            result = error or summary.get("status_detail") or ("running" if in_progress else "finished")
            item["last"] = {"start": created or start, "end": None if in_progress else end, "result": str(result),
                            "ok": None if in_progress else (False if error else True)}
            transcript = self.transcripts.read(str(run.get("cliSessionId") or run.get("lastAssistantCliSessionId") or ""))
            if transcript is not None:
                usage = transcript["usage"]
                item["tokens"] = usage.as_json(live=in_progress, source="transcript")
                if not usage.models and run.get("model"):
                    item["tokens"]["models"] = [str(run["model"])]
                if transcript.get("final"):
                    item["lines"].append({"t": transcript.get("at"), "text": transcript["final"]})
            else:
                item["tokens"] = {"none_yet": True}
            if error:
                item["status"] = "fault"
                item["reason"] = error
            elif run.get("lastTurnCutShort") and not in_progress:
                item["status"] = "fault"
                item["reason"] = "The last run was cut short"
            if summary.get("status_category") == "blocked":
                item["attention"] = str(summary.get("needs_action") or summary.get("status_detail") or "Needs you")
            item["details"] += [["Last run", str(run.get("sessionId") or "")],
                                ["Model", str(run.get("model") or "")]]
            path = self.transcripts.path(str(run.get("cliSessionId") or ""))
            if path:
                item["details"].append(["Transcript", str(path)])
        else:
            item["tokens"] = {"none_yet": True}

        # a run that has fired but has no file yet
        if not in_progress and last_fired is not None and now - last_fired < 120 and \
                (start is None or last_fired > start + 30):
            in_progress = True
            start = last_fired
            item["last"] = {"start": last_fired, "end": None, "result": "starting", "ok": None}
            item["tokens"] = {"none_yet": True}  # the previous run's tokens are not this run's
            item["lines"] = []

        if in_progress:
            item["status"] = "running"
            item["since"] = start
            item["reason"] = None

        # the next run
        if enabled and schedule is not None:
            base = max(last_scheduled or 0.0, now - (delay or 0.0))
            if in_progress and start:
                base = max(base, start - (delay or 0.0))
            slot = schedule.next_after(base)
            if slot is not None:
                item["next"] = slot + (delay or 0.0)
                item["details"].append(["Next cron time", _local(slot)])
            if not in_progress and item["status"] != "fault":
                missed = self._missed(schedule, task, last_scheduled, delay, run, now)
                if missed is not None:
                    item["status"] = "fault"
                    item["reason"] = f"No run since the one due {_local(missed + (delay or 0.0), short=True)}"
            if app_running is False and not in_progress:
                item["status"] = "fault"
                item["reason"] = "The Claude app is closed: its routines run only while it is open"
        elif not enabled:
            item["status"] = "off"
            item["reason"] = "Turned off in the Claude app"
            item["next"] = None
        elif schedule is None:
            item["status"] = "fault"
            item["reason"] = f"Schedule not understood: {expression!r}"

        item["details"] = [["Source", "Claude app routine"], ["Routine id", task_id], ["Cron", expression],
                           ["Usual delay", _delay_words(delay) if delay is not None else "unknown"],
                           ["Instructions", str(skill or "")], ["Folder", str(task.get("cwd") or "")],
                           ["Read from", str(source)]] + item["details"]
        return item

    @staticmethod
    def _missed(schedule: cron.Cron, task: dict, last_scheduled: float | None, delay: float | None,
                run: dict | None, now: float) -> float | None:
        """The cron time of a run that should have started by now and did not; None if none is missing."""
        delay = delay or 0.0
        slot = schedule.last_at_or_before(now - delay - MISSED_GRACE_S)
        if slot is None:
            return None
        known = last_scheduled or 0.0
        if run is not None:
            known = max(known, _ms(run.get("createdAt")) / 1000 - delay - 60)
        created = _ms(task.get("createdAt")) / 1000
        if not known and not created:
            return None  # nothing says since when it should have run: not called missed
        if slot > known + 60 and slot > created:
            return slot
        return None


# --- reading --------------------------------------------------------------------------------------------------
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("%s not read: %s", path, e)
        return None


def _read_run(path: Path) -> dict | None:
    data = _read_json(path)
    if not isinstance(data, dict) or not data.get("scheduledTaskId"):
        return None
    record = {k: data.get(k) for k in _RUN_FIELDS}
    summary = data.get("postTurnSummary")
    if isinstance(summary, dict):
        record["postTurnSummary"] = {k: summary.get(k) for k in ("status_category", "status_detail", "needs_action")}
    return record


def _skill_description(path: Path) -> str:
    """The description line of a routine's SKILL.md front matter."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
    except OSError:
        return ""
    m = re.match(r"﻿?---\s*\n(.*?)\n---", head, re.S)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "description":
            return value.strip().strip('"').strip("'")
    return ""


def _ms(value) -> float:
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def _iso(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _local(t: float, short: bool = False) -> str:
    moment = _dt.datetime.fromtimestamp(t)
    today = _dt.datetime.now().date()
    if short and moment.date() == today:
        return moment.strftime("%H:%M")
    if short:
        return moment.strftime("%a %H:%M")
    return moment.strftime("%a %d %b %H:%M")


def _delay_words(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes} min {secs:02d} s" if minutes else f"{secs} s"


def _schedule_words(schedule: cron.Cron | None, expression: str, delay: float | None) -> str:
    words = schedule.describe() if schedule is not None else (f"cron {expression}" if expression else "no schedule")
    if delay is not None and delay >= 30:
        minutes = int(round(delay / 60))
        words += f" (starts ~{minutes} min later)" if minutes else f" (starts ~{int(delay)} s later)"
    return words
