"""The window: Google Chrome without browser controls (--app) on the panel's address, with a profile of its own,
and its move to a chosen monitor.

Measured on GNOME Wayland (Ubuntu 26.04, Chrome 157, 2026-11-10):
  * the desktop's name for the window comes from the address Chrome opens, so the address is always the same;
  * a move made as soon as the window appears can be undone by the desktop's own placement, so the monitor is read
    back and the move made again until the window has stayed there for a second;
  * Chrome leaves some temporary files behind on every start (about 0.8 MB), so it gets a folder of its own for them,
    removed when it ends.

Windows are moved through `desk` (`vcdesk` on Windows), Voice Console's command; without it the window stays where
the desktop opens it."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from routine_panel import oshelp

log = logging.getLogger("routine_panel")
_WINDOWS = sys.platform.startswith("win")


class ChromeNotFound(Exception):
    pass


def find_chrome(given: str | None = None) -> str | None:
    """The browser that draws the window: Google Chrome where it is installed, otherwise Microsoft Edge on Windows
    (every Windows has it: a clean Windows 11 had Edge and no Chrome, measured 2026-11-16) or Chromium on Linux. All
    three open an app window (--app) with a profile of their own. `given`: a path from the settings ("chrome")."""
    if given:
        return given if os.path.isfile(given) else None
    if _WINDOWS:
        candidates = [Path("Google") / "Chrome" / "Application" / "chrome.exe",
                      Path("Microsoft") / "Edge" / "Application" / "msedge.exe"]
        for relative in candidates:
            for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                base = os.environ.get(variable)
                if base and (Path(base) / relative).is_file():
                    return str(Path(base) / relative)
        return None
    for name in ("google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    if os.path.isfile("/opt/google/chrome/chrome"):
        return "/opt/google/chrome/chrome"
    for name in ("chromium", "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


class Chrome:
    def __init__(self, process: subprocess.Popen, temporary: Path | None):
        self.process = process
        self.pid = process.pid
        self._temporary = temporary

    def alive(self) -> bool:
        return self.process.poll() is None

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def ask_to_close(self) -> None:
        if self.alive():
            try:
                self.process.terminate()  # SIGTERM: Chrome closes as it would for a person
            except OSError:
                pass

    def kill_if_alive(self) -> None:
        if self.alive():
            log.warning("chrome did not close when asked: ended")
            try:
                self.process.kill()
            except OSError:
                pass

    def close(self, timeout: float = 10.0) -> None:
        """Asks Chrome to close (it then writes its profile as it would for a person) and ends it harder only if
        it has not closed in time."""
        if self.alive():
            try:
                self.process.send_signal(signal.SIGTERM) if not _WINDOWS else self.process.terminate()
            except OSError:
                pass
            if self.wait(timeout) is None:
                log.warning("chrome did not close in %g s: ended", timeout)
                self.process.kill()
                self.wait(5)
        self.release()

    def release(self) -> None:
        if not self.alive() and self._temporary is not None:
            shutil.rmtree(self._temporary, ignore_errors=True)
            self._temporary = None


def launch(url: str, profile: Path, runtime: Path | None, chrome: str | None = None,
           window_size: tuple[int, int] | None = None) -> Chrome:
    program = find_chrome(chrome)
    if program is None:
        raise ChromeNotFound("Neither Google Chrome nor " + ("Microsoft Edge" if _WINDOWS else "Chromium") +
                             " was found. Install Google Chrome, or give a browser's path in the settings (\"chrome\").")
    profile.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(profile, 0o700)
    args = [program, f"--user-data-dir={profile}", f"--app={url}", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter", "--hide-crash-restore-bubble", "--disable-sync",
            "--password-store=basic"]
    if window_size:
        args.append(f"--window-size={int(window_size[0])},{int(window_size[1])}")
    env = dict(os.environ)
    temporary = None
    if runtime is not None and not _WINDOWS:
        runtime.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="t-", dir=runtime))
        env["TMPDIR"] = str(temporary)
    try:
        process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True, **oshelp.popen_flags())
    except OSError:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    log.info("chrome started (pid %d)", process.pid)
    return Chrome(process, temporary)


# --- the desktop: monitors --------------------------------------------------------------------------------------
def _desk() -> str | None:
    return shutil.which("vcdesk" if _WINDOWS else "desk")


def _run(desk: str, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return oshelp.run([desk, *args], timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("desk %s: %s", args[0], e)
        return None


def window_of(pid: int) -> dict | None:
    desk = _desk()
    if desk is None:
        return None
    answer = _run(desk, "windows", "--json")
    if answer is None or answer.returncode != 0:
        return None
    try:
        windows = json.loads(answer.stdout)
    except ValueError:
        return None
    for window in windows if isinstance(windows, list) else []:
        if isinstance(window, dict) and window.get("pid") == pid and window.get("id") is not None:
            return window
    return None


def to_screen(pid: int, screen: int, wait_s: float = 20.0, settle_s: float = 1.0) -> bool:
    """Moves the window of process `pid` to monitor `screen` and brings it to the front; whether it got there."""
    desk = _desk()
    if desk is None:
        log.info("window not moved: no desk command")
        return False
    deadline = time.monotonic() + wait_s
    moves = 0
    there_since = None
    while time.monotonic() < deadline:
        window = window_of(pid)
        if window is None:
            there_since = None
        elif window.get("monitor") == screen:
            if there_since is None:
                there_since = time.monotonic()
            elif time.monotonic() - there_since >= settle_s:
                _run(desk, "focus", str(window["id"]))
                log.info("window on monitor %d (moved %d time%s)", screen, moves, "" if moves == 1 else "s")
                return True
        else:
            there_since = None
            _run(desk, "screen", str(window["id"]), str(screen))
            moves += 1
        time.sleep(0.25)
    log.warning("window not on monitor %d after %g s (moved %d times)", screen, wait_s, moves)
    return False


MODES = ("normal", "maximized", "fullscreen")


def state_of(pid: int) -> dict | None:
    """Where the window of `pid` is: {monitor, mode, minimized}; None when it cannot be told."""
    window = window_of(pid)
    if window is None or not isinstance(window.get("monitor"), int):
        return None
    mode = "fullscreen" if window.get("fullscreen") else "maximized" if window.get("maximized") else "normal"
    return {"monitor": window["monitor"], "mode": mode, "minimized": bool(window.get("minimized"))}


def place(pid: int, screen: int | None, mode: str, wait_s: float = 20.0) -> bool:
    """Puts the window on monitor `screen` (None: where the desktop put it) and in `mode`; whether it got there.

    The move comes first and the full screen after it: a window made full screen on one monitor and then moved keeps
    the first monitor's shape. The desktop can undo a change made to a new window, so each is read back and made
    again until it holds (measured 2026-11-10 on GNOME: "moved 3 times")."""
    desk = _desk()
    if desk is None:
        return False
    if screen is not None and not to_screen(pid, screen, wait_s=wait_s):
        return False
    if mode not in ("maximized", "fullscreen"):
        return True
    command = "fullscreen" if mode == "fullscreen" else "maximize"
    deadline = time.monotonic() + 8.0
    tries = 0
    while time.monotonic() < deadline:
        window = window_of(pid)
        if window is not None and window.get(mode):
            time.sleep(0.6)
            again = window_of(pid)
            if again is not None and again.get(mode) and (screen is None or again.get("monitor") == screen):
                log.info("window %s on monitor %s (asked %d time%s)", mode, again.get("monitor"), tries,
                         "" if tries == 1 else "s")
                return True
        elif window is not None:
            _run(desk, command, str(window["id"]))
            tries += 1
        time.sleep(0.4)
    log.warning("window not %s after 8 s", mode)
    return False


class Watcher:
    """Notes, every two seconds while the window is open, which monitor it is on and whether it is maximized or
    full screen, so the next window opens the same way. A minimized window keeps the last place it was seen in."""

    def __init__(self, pid: int, every_s: float = 2.0):
        self.pid = pid
        self.every_s = every_s
        self.last: dict | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="window-watcher", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.every_s):
            state = state_of(self.pid)
            if state is not None and not state["minimized"]:
                self.last = state


def to_front(pid: int) -> bool:
    desk = _desk()
    window = window_of(pid) if desk else None
    if window is None:
        return False
    answer = _run(desk, "focus", str(window["id"]))
    return answer is not None and answer.returncode == 0
