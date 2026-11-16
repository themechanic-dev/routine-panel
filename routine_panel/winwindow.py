"""Windows: the panel's window under its own name and icon on the taskbar.

The window is Chrome's, so the taskbar would group it with Chrome, under Chrome's icon. Windows groups windows by an
AppUserModelID: the installer gives the Start menu shortcut "themechanic.RoutinePanel", and here the same ID, with the
icon and the command to open it again, is put on the Chrome window itself (its property store). Then the taskbar shows
the panel as its own app, and pinning it pins the panel, not Chrome.

Win32 through ctypes, every function with its argument types declared (without them a window handle can be cut to
32 bits: memory ctypes-win32-argtypes-hwnd)."""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes

log = logging.getLogger("routine_panel")

APP_USER_MODEL_ID = "themechanic.RoutinePanel"
_PKEY_FMTID = "{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"
_PID_RELAUNCH_COMMAND, _PID_RELAUNCH_ICON, _PID_RELAUNCH_NAME, _PID_ID = 2, 3, 4, 5
_VT_LPWSTR = 31


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.WinDLL("ole32").CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _windows_of(pid: int) -> list[int]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found: list[int] = []

    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if name.value.startswith("Chrome_WidgetWin"):
                found.append(hwnd)
        return True

    user32.EnumWindows(proc(each), 0)
    return found


def _set_properties(hwnd: int, values: dict[int, str]) -> None:
    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitialize(None)
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    iid = _guid("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")  # IPropertyStore
    store = ctypes.c_void_p()
    hr = shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(iid), ctypes.byref(store))
    if hr != 0 or not store.value:
        raise OSError(f"SHGetPropertyStoreForWindow: 0x{hr & 0xFFFFFFFF:08X}")
    vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PROPERTYKEY),
                                   ctypes.POINTER(_PROPVARIANT))(vtable[6])
    commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    fmtid = _guid(_PKEY_FMTID)
    try:
        # the ID last: Windows reads the relaunch details when the ID changes
        for pid in sorted(values, key=lambda p: p == _PID_ID):
            text = ctypes.create_unicode_buffer(values[pid])
            key = _PROPERTYKEY(fmtid, pid)
            variant = _PROPVARIANT(_VT_LPWSTR, 0, 0, 0, ctypes.cast(text, ctypes.c_void_p), None)
            hr = set_value(store, ctypes.byref(key), ctypes.byref(variant))
            if hr != 0:
                raise OSError(f"IPropertyStore.SetValue({pid}): 0x{hr & 0xFFFFFFFF:08X}")
        commit(store)
    finally:
        release(store)


def name_window(pid: int, icon: str, command: str, wait_s: float = 20.0) -> bool:
    """Gives the Chrome window of `pid` the panel's AppUserModelID, icon and relaunch command; whether it could."""
    if not sys.platform.startswith("win"):
        return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            windows = _windows_of(pid)
        except OSError as e:
            log.warning("windows of %d: %s", pid, e)
            return False
        if windows:
            try:
                for hwnd in windows:
                    _set_properties(hwnd, {_PID_RELAUNCH_COMMAND: command, _PID_RELAUNCH_ICON: icon,
                                           _PID_RELAUNCH_NAME: "Routine Panel", _PID_ID: APP_USER_MODEL_ID})
                log.info("window named %s (%d window%s)", APP_USER_MODEL_ID, len(windows), "" if len(windows) == 1 else "s")
                return True
            except OSError as e:
                log.warning("window not named: %s", e)
                return False
        time.sleep(0.3)
    log.warning("no Chrome window of %d after %g s", pid, wait_s)
    return False


def to_front(pid: int) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        windows = _windows_of(pid)
        if not windows:
            return False
        user32 = ctypes.WinDLL("user32")
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow(windows[0], 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(windows[0]))
    except OSError:
        return False
