"""Tokens of a routine's last cycle, never totals.

Two sources:
  * Claude Code's transcript of a run (~/.claude/projects/<folder>/<session id>.jsonl): every answer from the API
    carries its `usage`. One answer is written over several lines (one per part of its content), all with the same
    message id, so each id is counted once (measured 2026-11-16: the morning bulletin had 13 repeated lines).
  * A program's own log, one line per call ("asked claude-opus-5-5: 6058 tokens in, 3512 out, 37.7 s"). A cycle is the
    calls made during one run of its service; the runs come from the journal.

Files are read again only when their size or time changes."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("routine_panel")

DEFAULT_LOG_PATTERN = (r"^(?P<ts>\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d)(?:[,.]\d+)?\b.*?\basked (?P<model>[\w.:-]+): "
                       r"(?P<input>\d+) tokens in, (?P<output>\d+) out(?:, (?P<seconds>[\d.]+) ?s)?")
_LOG_TAIL_BYTES = 512 * 1024
_GAP_S = 90.0  # without the journal: calls this close (end of one to start of the next) are one cycle


@dataclass
class Usage:
    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0
    calls: int = 0
    models: list[str] = field(default_factory=list)
    first: float | None = None
    last: float | None = None
    cache_known: bool = True

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output
        self.calls += other.calls
        for model in other.models:
            if model not in self.models:
                self.models.append(model)
        firsts = [x for x in (self.first, other.first) if x is not None]
        lasts = [x for x in (self.last, other.last) if x is not None]
        self.first = min(firsts) if firsts else None
        self.last = max(lasts) if lasts else None
        self.cache_known = self.cache_known and other.cache_known

    @property
    def total(self) -> int:
        return self.input + self.cache_write + self.cache_read + self.output

    def as_json(self, live: bool = False, source: str = "") -> dict:
        return {"in": self.input + self.cache_write, "fresh": self.input, "cache_write": self.cache_write,
                "cache_read": self.cache_read, "out": self.output, "total": self.total, "calls": self.calls,
                "models": list(self.models), "first": self.first, "at": self.last, "live": live,
                "cache_known": self.cache_known, "source": source}


# --- transcripts ----------------------------------------------------------------------------------------------
class Transcripts:
    def __init__(self, projects: Path):
        self.projects = Path(projects)
        self._paths: dict[str, Path | None] = {}
        self._cache: dict[Path, tuple[tuple[int, int], dict]] = {}

    def path(self, session_id: str) -> Path | None:
        found = self._paths.get(session_id)
        if found is not None and found.exists():
            return found
        if not session_id or not re.fullmatch(r"[\w-]+", session_id):
            return None
        try:
            matches = sorted(self.projects.glob(f"*/{session_id}.jsonl"))
        except OSError:
            matches = []
        found = matches[0] if matches else None
        if found is not None:
            self._paths[session_id] = found
        return found

    def read(self, session_id: str) -> dict | None:
        """{usage: Usage, final: last words of the run, at: time of its last line} for a session; None if there
        is no transcript."""
        path = self.path(session_id)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        result = read_transcript(path)
        self._cache[path] = (key, result)
        return result


def read_transcript(path: Path) -> dict:
    usage = Usage()
    seen: set[str] = set()
    final = ""
    last_line_at = None
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError as e:
        log.debug("transcript %s: %s", path, e)
        return {"usage": usage, "final": "", "at": None}
    with handle:
        for line in handle:
            if '"usage"' not in line and '"timestamp"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            at = _iso(entry.get("timestamp"))
            if at is not None:
                last_line_at = at
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            text = _text_of(message)
            if text:
                final = text
            numbers = message.get("usage")
            if not isinstance(numbers, dict):
                continue
            key = message.get("id") or entry.get("uuid") or ""
            if key in seen:
                continue
            seen.add(key)
            usage.input += _int(numbers.get("input_tokens"))
            usage.cache_write += _int(numbers.get("cache_creation_input_tokens"))
            usage.cache_read += _int(numbers.get("cache_read_input_tokens"))
            usage.output += _int(numbers.get("output_tokens"))
            usage.calls += 1
            model = message.get("model")
            if isinstance(model, str) and model and not model.startswith("<") and model not in usage.models:
                usage.models.append(model)
            if at is not None:
                usage.first = at if usage.first is None else min(usage.first, at)
                usage.last = at if usage.last is None else max(usage.last, at)
    return {"usage": usage, "final": final[-600:], "at": last_line_at}


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _int(value) -> int:
    return int(value) if isinstance(value, (int, float)) and value > 0 else 0


def _iso(text) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# --- logs -----------------------------------------------------------------------------------------------------
@dataclass
class Call:
    end: float
    usage: Usage
    seconds: float = 0.0

    @property
    def start(self) -> float:
        return self.end - self.seconds


class LogCalls:
    """The calls a log records, read again only when the log changes."""

    def __init__(self):
        self._cache: dict[tuple[Path, str], tuple[tuple[int, int], list[Call]]] = {}

    def calls(self, path: Path, pattern: str | None = None) -> list[Call]:
        pattern = pattern or DEFAULT_LOG_PATTERN
        try:
            stat = path.stat()
        except OSError:
            return []
        key = (stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get((path, pattern))
        if cached and cached[0] == key:
            return cached[1]
        calls = parse_log(_tail(path, _LOG_TAIL_BYTES), pattern)
        self._cache[(path, pattern)] = (key, calls)
        return calls

    def changed_at(self, path: Path) -> float | None:
        try:
            return path.stat().st_mtime
        except OSError:
            return None


def _tail(path: Path, size: int) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - size))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    if length > size:
        text = text.split("\n", 1)[-1]  # the first line may be cut
    return text


def parse_log(text: str, pattern: str = DEFAULT_LOG_PATTERN) -> list[Call]:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        log.warning("token pattern %r: %s", pattern, e)
        return []
    calls = []
    for line in text.splitlines():
        m = regex.search(line)
        if not m:
            continue
        groups = m.groupdict()
        end = _local_time(groups.get("ts"))
        if end is None:
            continue
        usage = Usage(input=_num(groups.get("input")), output=_num(groups.get("output")),
                      cache_read=_num(groups.get("cache_read")), cache_write=_num(groups.get("cache_write")),
                      calls=1, cache_known="cache_read" in groups)
        model = groups.get("model")
        if model:
            usage.models.append(model)
        seconds = float(groups["seconds"]) if groups.get("seconds") else 0.0
        usage.first = end - seconds
        usage.last = end
        calls.append(Call(end=end, usage=usage, seconds=seconds))
    return calls


def _num(text) -> int:
    try:
        return int(text) if text else 0
    except ValueError:
        return 0


def _local_time(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text.replace(" ", "T")).timestamp()
    except ValueError:
        return None


def last_cycle(calls: list[Call], run_starts: list[float]) -> Usage | None:
    """The calls of the run that made the last call, added up. `run_starts` are the service's run start times
    (from the journal, any order); without them, calls close together in time count as one cycle."""
    if not calls:
        return None
    calls = sorted(calls, key=lambda c: c.end)
    last = calls[-1]
    starts = sorted(s for s in run_starts if s <= last.start + 1.0)
    chosen: list[Call]
    if starts:
        begin = starts[-1]
        later = [s for s in sorted(run_starts) if s > begin]
        end = later[0] if later else float("inf")
        chosen = [c for c in calls if begin - 1.0 <= c.start and c.end < end + 1.0]
        if last not in chosen:
            chosen.append(last)
    else:
        chosen = [last]
        for call in reversed(calls[:-1]):
            if chosen[0].start - call.end <= _GAP_S:
                chosen.insert(0, call)
            else:
                break
    total = Usage()
    for call in chosen:
        total.add(call.usage)
    return total
