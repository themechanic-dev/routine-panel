"""Phones paired with this panel, and the addresses they can reach it at.

Pairing: the window asks for a new key; the panel shows it in a QR code, once, with this computer's addresses; the
phone app keeps it and sends it with every request. Only a fingerprint of each key (SHA-256) is kept on this computer,
in ~/.config/routine-panel/phones.json (private to the user). A key nobody has used within ten minutes is dropped, so
a code left on the screen stops working. Removing a phone in the window takes its key away at once.

A phone only reads: its key opens the picture of the routines and nothing else (no pairing, no removing)."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

log = logging.getLogger("routine_panel")

SCHEME = "routinepanel"
UNUSED_FOR_S = 10 * 60
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
PRIVATE = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
_AGENT = re.compile(r"RoutinePanelApp/([\w.]+) \(([^;()]{1,400})\)")


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Phones:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._phones: list[dict] = []
        self._loaded_key = None
        self._live: dict[str, dict] = {}  # id → {seen, address, via}: kept in memory only
        self._load()

    # --- the file ---------------------------------------------------------------------------------------------
    def _stamp(self):
        try:
            stat = self.path.stat()
            return (stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None

    def _load(self) -> None:
        stamp = self._stamp()
        if stamp == self._loaded_key and stamp is not None:
            return
        phones = []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("phones"), list):
                phones = [p for p in data["phones"] if isinstance(p, dict) and p.get("id") and p.get("fingerprint")]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("paired phones not read (%s): %s", self.path, e)
        self._phones = phones
        self._loaded_key = stamp

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"phones": self._phones}, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)
        self._loaded_key = self._stamp()

    # --- pairing ----------------------------------------------------------------------------------------------
    def pair(self, now: float | None = None) -> tuple[dict, str]:
        """A new phone's record and its key (the key is never stored: only its fingerprint)."""
        now = time.time() if now is None else now
        key = secrets.token_urlsafe(32)
        record = {"id": secrets.token_hex(4), "name": "New phone", "paired": now, "used": False,
                  "fingerprint": _fingerprint(key)}
        with self._lock:
            self._load()
            self._drop_unused(now)
            self._phones.append(record)
            self._save()
        log.info("phone %s: pairing code made", record["id"])
        return dict(record), key

    def check(self, key: str, now: float | None = None) -> dict | None:
        """The phone a key belongs to; None for any other key."""
        if not key:
            return None
        now = time.time() if now is None else now
        given = _fingerprint(key)
        with self._lock:
            self._load()
            self._drop_unused(now)
            for phone in self._phones:
                if hmac.compare_digest(given, phone["fingerprint"]):
                    return phone
        return None

    def seen(self, phone: dict, address: str, agent: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        via = "Tailscale" if _in(address, [TAILSCALE]) else ("this network" if _in(address, PRIVATE) else "this computer")
        self._live[phone["id"]] = {"seen": now, "address": address, "via": via}
        m = _AGENT.search(_utf8(agent or ""))
        name = _phone_name(m.group(2)) if m else None
        if phone.get("used") and (name is None or name == phone.get("name")):
            return
        with self._lock:
            self._load()
            for record in self._phones:
                if record["id"] == phone["id"]:
                    first = not record.get("used")
                    record["used"] = True
                    record.setdefault("first_used", now)
                    if m:
                        record["name"] = name  # also when the phone was renamed since
                        record["app"] = m.group(1)
                    self._save()
                    phone.update(record)
                    if first:
                        log.info("phone %s paired", record["id"])
                    break

    def remove(self, phone_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._phones)
            self._phones = [p for p in self._phones if p.get("id") != phone_id]
            if len(self._phones) == before:
                return False
            self._save()
        self._live.pop(phone_id, None)
        log.info("phone %s removed", phone_id)
        return True

    def listed(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._lock:
            self._load()
            self._drop_unused(now)
            phones = [dict(p) for p in self._phones]
        out = []
        for phone in phones:
            phone.pop("fingerprint", None)
            phone.update(self._live.get(phone["id"], {}))
            out.append(phone)
        return out

    def _drop_unused(self, now: float) -> None:
        kept = [p for p in self._phones if p.get("used") or now - float(p.get("paired") or 0) < UNUSED_FOR_S]
        if len(kept) != len(self._phones):
            self._phones = kept
            self._save()


def _phone_name(text: str) -> str | None:
    """The phone's name from the app's User-Agent: percent-encoded UTF-8 since app 0.1.1. A name that came through a
    web view unencoded has "?" for every letter outside Latin-1 (measured 2026-11-16): such a name is not taken."""
    name = unquote(text.strip()) if "%" in text else text.strip()
    name = name.strip()[:60]
    if not name or "?" in name or "\ufffd" in name:
        return None
    return name


def _utf8(text: str) -> str:
    """A header as the phone meant it: Python reads header bytes as Latin-1, the app sends its name in UTF-8 (a Greek
    phone name came out as "Î¤Î¿ …", measured 2026-11-16)."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _in(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in n for n in networks)


def allowed_peer(address: str, lan: bool) -> bool:
    """Who may talk to the service at all: this computer, Tailscale, and (when allowed) the home network."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip in TAILSCALE:
        return True
    return lan and any(ip in n for n in PRIVATE)


# --- this computer's addresses ---------------------------------------------------------------------------------------
def addresses(lan: bool) -> list[tuple[str, str]]:
    """[(kind, address)]: the Tailscale address first (it works from anywhere), then the home network's (the one the
    default route leaves from), when allowed."""
    found: list[tuple[str, str]] = []
    tail = _tailscale_address()
    if tail:
        found.append(("tailscale", tail))
    if lan:
        home = _route_source("1.1.1.1")
        if home and _in(home, PRIVATE):
            found.append(("lan", home))
    return found


def _tailscale_address() -> str | None:
    if shutil.which("ip"):
        try:
            text = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5,
                                  check=False, stdin=subprocess.DEVNULL).stdout
            for link in json.loads(text or "[]"):
                for info in link.get("addr_info", []):
                    if _in(info.get("local", ""), [TAILSCALE]):
                        return info["local"]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return _route_source("100.100.100.100") if _in(_route_source("100.100.100.100") or "", [TAILSCALE]) else None


def _route_source(target: str) -> str | None:
    """The address this computer would send from to reach `target` (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def pairing_text(key: str, port: int, name: str, found: list[tuple[str, str]]) -> str:
    """What the QR code says: a link the app (or the phone's camera) opens."""
    hosts = ",".join(address for _, address in found)
    return f"{SCHEME}://pair?v=1&n={quote(name, safe='')}&p={port}&a={hosts}&k={key}"
