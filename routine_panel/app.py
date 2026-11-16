"""Starting and ending Routine Panel.

    routine-panel                  opens the window (or brings the open one to the front)
    routine-panel --screen 3       on monitor 3 (Voice Console's `desk` numbers)
    routine-panel --theme dark     light or dark from the start, whatever the system's theme
    routine-panel --view grid      the grid (every routine fits in the window) from the start
    routine-panel --serve          only the server: prints the address to open, ends with Ctrl+C
    routine-panel --service        the service for phones (systemd starts it at login, see install.sh --phone)
    routine-panel --print          prints one picture of the routines as JSON and ends

Without the service, the window has a server of its own on 127.0.0.1 that ends when the window closes. With the
service running, the window opens on the service instead (its address and the window's key are in the runtime
folder's service.json, private to the user), and phones paired in the window can reach it too."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

from routine_panel import APP_NAME, HOST_NAME, __version__, settings as settings_mod
from routine_panel.collect import Collector

log = logging.getLogger("routine_panel")
PORT_TRIES = 10


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="routine-panel", description=f"{APP_NAME}: every routine of this computer.")
    parser.add_argument("--screen", type=int, help="monitor to open the window on (desk's numbers)")
    parser.add_argument("--theme", choices=("auto", "light", "dark"), help="the window's theme at start")
    parser.add_argument("--view", choices=("list", "grid"), help="the window's view at start")
    parser.add_argument("--port", type=int, help="the local server's port")
    parser.add_argument("--serve", action="store_true", help="only the server; print the address")
    parser.add_argument("--service", action="store_true", help="the service for phones (runs until stopped)")
    parser.add_argument("--print", action="store_true", help="print one picture of the routines as JSON")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    dirs = settings_mod.app_dirs().make()
    _logging(dirs.data / ("service.log" if args.service else "routine-panel.log"), verbose=args.serve or args.print)
    settings = settings_mod.load(dirs.config / "settings.json")
    collector = Collector(settings, theme=args.theme, view=args.view)

    if args.print:
        if sys.stdout is None:
            return 2
        json.dump(collector.snapshot(), sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write("\n")
        return 0

    if args.service:
        return _service(args, dirs, settings, collector)

    lock = None
    if not args.serve:  # --serve opens no window, so it can run beside an open one (on the next free port)
        lock = _one_copy(dirs.runtime)
        if lock is None:
            return _bring_forward(dirs.runtime)

    from routine_panel.server import PanelServer
    service = None if args.serve else _running_service(dirs.runtime)
    if service is not None:
        return _window(args, dirs, settings,
                       f"http://{HOST_NAME}:{service['port']}/#{service['key']}{start_options(args)}", None)
    key = secrets.token_urlsafe(24)
    # The window's own server starts one above the panel's port: that port belongs to the service, whose address the
    # paired phones keep (measured 2026-11-16: with the window open first, the service had moved to 47614).
    port = (args.port or settings.get("port") or 47613) + 1
    server = None
    for attempt in range(PORT_TRIES):
        try:
            server = PanelServer(collector, key, port + attempt)
            break
        except OSError as e:
            log.warning("port %d: %s", port + attempt, e)
    if server is None:
        _say(f"{APP_NAME} could not open its local server (ports {port}–{port + PORT_TRIES - 1} are taken).")
        return 1
    server.start()
    # the first reading takes a few seconds (every run file of the Claude app); it starts while the window opens
    threading.Thread(target=collector.state, name="first-reading", daemon=True).start()
    # The window's address. Its host names the window on the desktop ("chrome-<host>__-Default" on GNOME Wayland,
    # measured 2026-11-10): "127.0.0.1" would be shared with any other local Chrome app, so the panel has a host of
    # its own. Chrome sends every *.localhost name to this computer without asking any DNS (RFC 6761).
    url = f"http://{HOST_NAME}:{server.port}/#{key}{start_options(args)}"

    if args.serve:
        if sys.stdout is not None:
            print(f"{APP_NAME} {__version__}: open {url}  (Ctrl+C ends it)", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        server.stop()
        return 0
    return _window(args, dirs, settings, url, server)


def start_options(args) -> str:
    """--theme and --view, written after the key in the window's address ("#<key>&theme=light"), so they reach the
    page whichever server answers it: the window's own, or the service, which was started without them (measured
    2026-11-16: with the service running, "--theme light" opened in the remembered theme)."""
    parts = [f"{name}={value}" for name, value in (("theme", args.theme), ("view", args.view)) if value]
    return "".join("&" + part for part in parts)


def _window(args, dirs, settings: dict, url: str, server) -> int:
    """Opens Chrome on `url` and waits for the window to close; `server` (the window's own, if it has one) ends
    with it."""
    from routine_panel import window
    size = settings.get("window_size")
    size = tuple(size) if isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) for v in size) else None
    try:
        chrome = window.launch(url, dirs.data / "profile", dirs.runtime, chrome=settings.get("chrome"),
                               window_size=size)
    except window.ChromeNotFound:
        return _in_browser(url, server, dirs)
    except OSError as e:
        _say(str(e))
        if server is not None:
            server.stop()
        return 1
    _note(dirs.runtime, chrome.pid)

    def end(_signum=None, _frame=None):
        # Only asks: the main thread is already waiting for Chrome, and a second wait from inside this handler
        # would sit on the same lock until it timed out (measured 2026-11-16: 10 s, then Chrome was killed).
        chrome.ask_to_close()
        timer = threading.Timer(10.0, chrome.kill_if_alive)
        timer.daemon = True
        timer.start()

    signal.signal(signal.SIGTERM, end)
    signal.signal(signal.SIGINT, end)
    screen, mode = _placement(args, dirs, settings)
    watcher = window.Watcher(chrome.pid)

    def place_then_watch():
        window.place(chrome.pid, screen, mode)
        watcher.start()  # only after: a window closed while it is still being placed keeps the old place

    threading.Thread(target=place_then_watch, name="place-window", daemon=True).start()
    if os.name == "nt":
        # the taskbar shows the window as Routine Panel, with its icon (taken from the program, where the build put
        # it), and opens it again with this command
        from routine_panel import winwindow
        if getattr(sys, "frozen", False):
            command, icon = f'"{sys.executable}"', f"{sys.executable},0"
        else:
            command, icon = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"', ""
        threading.Thread(target=winwindow.name_window, args=(chrome.pid, icon, command), name="name-window",
                         daemon=True).start()

    code = chrome.wait()
    log.info("window closed (chrome exit %s)", code)
    watcher.stop()
    if watcher.last is not None:
        _write_private(dirs.data / PLACE_FILE, {"screen": watcher.last["monitor"], "mode": watcher.last["mode"]})
        log.info("place kept: monitor %d, %s", watcher.last["monitor"], watcher.last["mode"])
    chrome.release()
    if server is not None:
        server.stop()
    try:
        (dirs.runtime / "state.json").unlink()
    except OSError:
        pass
    return 0


# --- where the window opens ----------------------------------------------------------------------------------------
PLACE_FILE = "window.json"


def _placement(args, dirs, settings: dict) -> tuple[int | None, str]:
    """The monitor and the mode (normal, maximized, full screen) the window opens in: --screen when given; else where
    the last window was when it closed; else the settings ("screen", "window_mode")."""
    from routine_panel import window
    kept = {}
    try:
        data = json.loads((dirs.data / PLACE_FILE).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            kept = data
    except (OSError, ValueError):
        pass
    screen = args.screen
    if screen is None and _is_int(kept.get("screen")):
        screen = kept["screen"]
    if screen is None and _is_int(settings.get("screen")):
        screen = settings["screen"]
    mode = kept.get("mode") if kept.get("mode") in window.MODES else settings.get("window_mode")
    if mode not in window.MODES:
        mode = "normal"
    return screen, mode


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- one copy at a time -----------------------------------------------------------------------------------------
def _one_copy(runtime: Path):
    handle = open(runtime / "lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle  # kept open (and locked) for as long as this process runs; the system lets go when it ends


def _note(runtime: Path, pid: int | None, url: str | None = None) -> None:
    _write_private(runtime / "state.json", {"chrome_pid": pid, "url": url})


IDLE_S = 60.0  # in a browser tab: the page asks every 2 s, so a minute without a question means it was closed
FIRST_S = 180.0


def _in_browser(url: str, server, dirs) -> int:
    """Without Chrome, Chromium or Edge (a clean Ubuntu has only Firefox, measured 2026-11-16): the panel opens in a
    tab of the default browser. A tab cannot be watched like a window, so the panel's own server ends when the page
    has stopped asking for a minute (or was never opened)."""
    import webbrowser
    opened = False
    try:
        opened = webbrowser.open(url, new=1)
    except Exception as e:  # noqa: BLE001 - whatever the browser module meets, the person is told
        log.warning("default browser: %s", e)
    if not opened:
        _say("No browser could be opened. Install Google Chrome (or Chromium), or open Routine Panel's address in a "
             "browser by hand: " + url.split("#", 1)[0])
        if server is not None:
            server.stop()
        return 1
    log.info("opened in the default browser")
    if server is None:  # on the service, which stays
        return 0
    _note(dirs.runtime, None, url)
    started = time.monotonic()
    while True:
        time.sleep(2.0)
        last = server.last_request
        if last is None and time.monotonic() - started > FIRST_S:
            break
        if last is not None and time.monotonic() - last > IDLE_S:
            break
    log.info("the page stopped asking: server ends")
    server.stop()
    try:
        (dirs.runtime / "state.json").unlink()
    except OSError:
        pass
    return 0


def _write_private(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


# --- the service for phones -------------------------------------------------------------------------------------
SERVICE_FILE = "service.json"


def _service(args, dirs, settings: dict, collector) -> int:
    from routine_panel.phones import Phones
    from routine_panel.server import PanelServer
    if _running_service(dirs.runtime) is not None:
        log.info("the service already runs")
        return 0
    key = _service_key(dirs.runtime)
    port = args.port or settings.get("port") or 47613
    lan = settings.get("lan") is not False
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    # Always this one port: paired phones keep it. While something else holds it, wait for it instead of moving.
    server = None
    said = False
    while server is None and not stop.is_set():
        try:
            server = PanelServer(collector, key, port, remote=True, lan=lan, service=True,
                                 phones=Phones(dirs.config / "phones.json"))
        except OSError as e:
            if not said:
                log.warning("port %d: %s; waiting for it", port, e)
                said = True
            stop.wait(3.0)
    if server is None:
        return 0
    server.start()
    _write_private(dirs.runtime / SERVICE_FILE, {"port": server.port, "key": key, "pid": os.getpid()})
    log.info("service ready (version %s)", __version__)
    stop.wait()
    server.stop()
    # service.json stays: the next start reads the window's key from it (a window looking for the service first
    # checks that it answers, so a stale file is harmless)
    log.info("service stopped")
    return 0


def _service_key(runtime: Path) -> str:
    """The window's key: the one the last service run wrote, when there is one, so an open window keeps working when
    the service restarts (measured 2026-11-16: a new key at every start left the open window refused, in red). The
    runtime folder is the user's own and empties at logout."""
    try:
        data = json.loads((runtime / SERVICE_FILE).read_text(encoding="utf-8"))
        key = data.get("key") if isinstance(data, dict) else None
        if isinstance(key, str) and len(key) >= 24 and key.replace("-", "").replace("_", "").isalnum():
            return key
    except (OSError, ValueError):
        pass
    return secrets.token_urlsafe(24)


def _running_service(runtime: Path) -> dict | None:
    """The service's port and the window's key, if a service runs and answers to that key."""
    from routine_panel.server import ping
    try:
        data = json.loads((runtime / SERVICE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("port"), int) or not isinstance(data.get("key"), str):
        return None
    answer = ping(data["port"], data["key"])
    if not answer or answer.get("role") != "window":
        return None
    return data


def _bring_forward(runtime: Path) -> int:
    try:
        state = json.loads((runtime / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    pid = state.get("chrome_pid") if isinstance(state, dict) else None
    if isinstance(pid, int):
        from routine_panel import window, winwindow
        if window.to_front(pid) or winwindow.to_front(pid):
            log.info("already open: brought to the front")
            return 0
    url = state.get("url") if isinstance(state, dict) else None
    if isinstance(url, str) and url.startswith("http://"):
        import webbrowser
        webbrowser.open(url, new=1)  # open in a browser tab: show it again
        return 0
    log.info("already open")
    return 0


# --- telling the person -----------------------------------------------------------------------------------------
def _say(message: str) -> None:
    log.error("%s", message)
    if sys.stderr is not None:  # a windowed program on Windows has none
        print(message, file=sys.stderr)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
        return
    import shutil
    import subprocess
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "--app-name", APP_NAME, APP_NAME, message], timeout=5, check=False,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def _logging(path: Path, verbose: bool) -> None:
    root = logging.getLogger("routine_panel")
    root.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    if verbose and sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(console)
