"""Routine Panel: every routine and automation of this computer in one window, with its state (running, waiting,
fault, off), a countdown to its next run and the tokens its last cycle used.

It only reads. Nothing in it starts, stops or changes a routine: the server has no request that writes, and the
collectors only read files and ask systemd and the journal questions.

The window is Google Chrome without browser controls (--app) on a small local server (127.0.0.1) that lives only as
long as the window is open: no daemon, no service, no packages beyond Python's standard library."""

__version__ = "0.2.0"

APP_NAME = "Routine Panel"
APP_ID = "store.themechanic.RoutinePanel"  # the launcher's name
POSIX_NAME = "routine-panel"  # ~/.config/routine-panel, ~/.local/share/routine-panel, $XDG_RUNTIME_DIR/routine-panel
WINDOWS_NAME = "RoutinePanel"
HOST_NAME = "routine-panel.localhost"  # the window's address (app.py says why)
DEFAULT_PORT = 47613  # fixed, so the window's address (and with it the desktop's name for the window) stays the same
