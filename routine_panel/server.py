"""The small server behind the window (and the phones): the page and its files, and the picture of the routines.

Who may ask what:
  * The window, from this computer, with its key (the part of its address after "#", which never leaves the
    browser), sent back in a header: the picture, and pairing and removing phones.
  * A paired phone, over Tailscale or (when allowed) the home network, with its own key: the picture only.
  * Anyone else: nothing. Connections from other addresses are turned away before reading a request, and the Host
    header must name the address the request came in on (or, from this computer, the panel's own names), so a web
    page that renames itself cannot reach the panel through someone's browser.

Nothing here starts, stops or changes a routine: the only requests that change anything change the list of paired
phones, and only the window can make them."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from routine_panel import HOST_NAME, __version__, phones as phones_mod, qr

log = logging.getLogger("routine_panel")

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css",
          "/icon.svg": "icon.svg"}
KEY_HEADER = "X-Panel-Key"
MAX_BODY = 4096
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    panel: "PanelServer"

    def verify_request(self, request, client_address) -> bool:
        allowed = phones_mod.allowed_peer(client_address[0], self.panel.lan)
        if not allowed:
            log.info("turned away %s", client_address[0])
        return allowed


class PanelServer:
    """`remote`: also listen beyond this computer (all IPv4 addresses, with the peer check above): the service's
    way, for phones. Without it only 127.0.0.1, as the window's own server."""

    def __init__(self, collector, key: str, port: int, *, remote: bool = False, lan: bool = False,
                 phones: phones_mod.Phones | None = None, service: bool = False):
        self.collector = collector
        self.key = key
        self.remote = remote
        self.lan = lan
        self.phones = phones
        self.service = service
        handler = type("Handler", (_Handler,), {"panel": self})
        self.httpd = _Server(("0.0.0.0" if remote else "127.0.0.1", port), handler)
        self.httpd.panel = self
        self.port = self.httpd.server_address[1]
        self.local_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"{HOST_NAME}:{self.port}"}
        self.last_request: float | None = None  # monotonic time of the last answered question with a key
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="routine-panel-server", daemon=True)
        self._thread.start()
        log.info("server on %s:%d%s", "0.0.0.0" if self.remote else "127.0.0.1", self.port,
                 " (phones: Tailscale" + (" and this network)" if self.lan else ")") if self.remote else "")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def pairing(self) -> dict:
        if self.phones is None or not self.remote:
            raise PermissionError("phones can pair only while Routine Panel's service runs")
        found = phones_mod.addresses(self.lan)
        if not found:
            raise OSError("this computer has no Tailscale address" + (" or home network address" if self.lan else ""))
        record, key = self.phones.pair()
        text = phones_mod.pairing_text(key, self.port, socket.gethostname(), found)
        return {"phone": {k: v for k, v in record.items() if k != "fingerprint"}, "text": text,
                "svg": qr.svg(text), "addresses": [{"kind": k, "address": a} for k, a in found],
                "expires": record["paired"] + phones_mod.UNUSED_FOR_S}


class _Handler(BaseHTTPRequestHandler):
    panel: PanelServer
    server_version = "RoutinePanel"
    sys_version = ""  # one request per connection (HTTP/1.0): nothing left over from a refused request

    def log_message(self, format, *args):  # noqa: A002 - the base class's name
        log.debug("http %s %s", self.client_address[0], format % args)

    def do_GET(self):  # noqa: N802
        self._answer("GET")

    def do_HEAD(self):  # noqa: N802
        self._answer("HEAD")

    def do_POST(self):  # noqa: N802
        self._answer("POST")

    def _refuse(self):
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"Not allowed\n", "text/plain; charset=utf-8",
                   extra={"Allow": "GET, HEAD, POST"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse  # noqa: N815

    # --- who is asking ----------------------------------------------------------------------------------------
    def _local(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        if self._local():
            return host in self.panel.local_hosts
        try:
            here = self.connection.getsockname()[0]
        except OSError:
            return False
        return host == f"{here}:{self.panel.port}"

    def _role(self) -> tuple[str | None, dict | None]:
        """("window", None) for the window's key from this computer, ("phone", record) for a paired phone's key."""
        given = self.headers.get(KEY_HEADER, "")
        if not given:
            return None, None
        if self._local() and hmac.compare_digest(given.encode(), self.panel.key.encode()):
            return "window", None
        if self.panel.phones is not None and self.panel.remote:
            phone = self.panel.phones.check(given)
            if phone is not None:
                self.panel.phones.seen(phone, self.client_address[0], self.headers.get("User-Agent", ""))
                return "phone", phone
        return None, None

    # --- answers ----------------------------------------------------------------------------------------------
    def _answer(self, method: str):
        head = method == "HEAD"
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"Forbidden\n", "text/plain; charset=utf-8", head=head)
            return
        path = self.path.split("?", 1)[0]
        if method != "POST" and path in STATIC:
            self._static(path, head)
            return
        if not path.startswith("/api/"):
            self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8", head=head)
            return
        role, phone = self._role()
        if role is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "key"}, head)
            return
        self.panel.last_request = time.monotonic()
        if method == "POST":
            if role != "window":
                self._json(HTTPStatus.FORBIDDEN, {"error": "only the window on this computer can do that"}, head)
                return
            self._post(path)
            return
        if path == "/api/state":
            self._state(role, head)
        elif path == "/api/ping":
            found = phones_mod.addresses(self.panel.lan) if self.panel.remote else []
            self._json(HTTPStatus.OK, {"ok": True, "name": socket.gethostname(), "version": __version__,
                                       "role": role, "addresses": [a for _, a in found]}, head)
        elif path == "/api/phones" and role == "window":
            listed = self.panel.phones.listed() if self.panel.phones is not None else []
            how = ("the Setup's option \u201cLet my phone see the panel\u201d" if os.name == "nt"
                   else "./install.sh --phone")
            self._json(HTTPStatus.OK, {"service": self.panel.service, "lan": self.panel.lan, "phones": listed,
                                       "how": how}, head)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, head)

    def _post(self, path: str):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "body"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "body"})
            return
        if path == "/api/phones/pair":
            try:
                self._json(HTTPStatus.OK, self.panel.pairing())
            except PermissionError as e:
                self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            except OSError as e:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(e)})
        elif path == "/api/phones/remove":
            phone_id = body.get("id")
            done = isinstance(phone_id, str) and self.panel.phones is not None and self.panel.phones.remove(phone_id)
            self._json(HTTPStatus.OK if done else HTTPStatus.NOT_FOUND, {"removed": bool(done)})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _state(self, role: str, head: bool):
        try:
            state = dict(self.panel.collector.state())  # a shallow copy: the cached picture is shared
            state["role"] = role
            state["phones"] = {"service": self.panel.service}
            body = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception as e:  # noqa: BLE001 - the window should say what went wrong, not go blank
            log.exception("state")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"reading failed: {e}"}, head)
            return
        self._send(HTTPStatus.OK, body, "application/json", head=head)

    def _static(self, path: str, head: bool):
        file = WEB / STATIC[path]
        try:
            body = file.read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"Not found\n", "text/plain; charset=utf-8", head=head)
            return
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
            kind += "; charset=utf-8"
        self._send(HTTPStatus.OK, body, kind, head=head)

    def _json(self, status, data: dict, head: bool = False):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json", head=head)

    def _send(self, status, body: bytes, kind: str, head: bool = False, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def ping(port: int, key: str, timeout: float = 2.0) -> dict | None:
    """Asks a panel server on this computer whether it answers to `key`; its answer, or None."""
    import http.client
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request("GET", "/api/ping", headers={KEY_HEADER: key, "Host": f"{HOST_NAME}:{port}"})
        answer = connection.getresponse()
        data = answer.read()
        connection.close()
        if answer.status != 200:
            return None
        return json.loads(data)
    except (OSError, ValueError):
        return None


def wait_ready(port: int, key: str, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if ping(port, key, timeout=1.0):
            return True
        time.sleep(0.1)
    return False
