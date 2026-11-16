import http.client
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tests.helpers import local  # noqa: F401 - fixes the time zone

from routine_panel import phones as phones_mod
from routine_panel.server import KEY_HEADER, PanelServer


class FakeCollector:
    def state(self):
        return {"now": 1.0, "sections": [], "counts": {}, "errors": []}


class PhonesTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "phones.json"

    def tearDown(self):
        self.folder.cleanup()

    def test_pair_check_seen_remove(self):
        phones = phones_mod.Phones(self.path)
        record, key = phones.pair(now=1000.0)
        if os.name == "posix":  # Windows keeps the file private by the user's own folder instead
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertNotIn(key, self.path.read_text())  # only the fingerprint is kept
        self.assertEqual(phones.check(key, now=1001.0)["id"], record["id"])
        self.assertIsNone(phones.check("wrong", now=1001.0))
        phone = phones.check(key, now=1001.0)
        phones.seen(phone, "100.90.10.20", "Mozilla/5.0 RoutinePanelApp/0.1.0 (Galaxy S22 Ultra)", now=1002.0)
        [listed] = phones.listed(now=1003.0)
        self.assertTrue(listed["used"])
        self.assertEqual(listed["name"], "Galaxy S22 Ultra")
        self.assertEqual(listed["via"], "Tailscale")
        self.assertNotIn("fingerprint", listed)
        # another process (the window's) sees the same file
        again = phones_mod.Phones(self.path)
        self.assertEqual(again.check(key, now=5000.0)["name"], "Galaxy S22 Ultra")
        # a Greek name, as Python's http.server hands the header over (UTF-8 bytes read as Latin-1), and a rename
        greek = "Mozilla/5.0 RoutinePanelApp/0.1.0 (Το κινητό μου)".encode("utf-8").decode("latin-1")
        phones.seen(phones.check(key, now=1004.0), "192.168.0.31", greek, now=1004.0)
        self.assertEqual(phones.listed(now=1005.0)[0]["name"], "Το κινητό μου")
        self.assertEqual(phones.listed(now=1005.0)[0]["via"], "this network")
        # the same name as the web view sends it (letters outside Latin-1 turned into "?"): not taken
        phones.seen(phones.check(key, now=1006.0), "192.168.0.31",
                    "Mozilla/5.0 (Linux) RoutinePanelApp/0.1.0 (?? ?????? ???)", now=1006.0)
        self.assertEqual(phones.listed(now=1007.0)[0]["name"], "Το κινητό μου")
        # app 0.1.1: percent-encoded UTF-8, the same on every way
        phones.seen(phones.check(key, now=1008.0), "192.168.0.31",
                    "Mozilla/5.0 (Linux) RoutinePanelApp/0.1.1 (%CE%A4%CE%BF%20S22%20Ultra)", now=1008.0)
        self.assertEqual(phones.listed(now=1009.0)[0]["name"], "Το S22 Ultra")
        self.assertTrue(phones.remove(record["id"]))
        self.assertIsNone(again.check(key, now=5001.0))
        self.assertFalse(phones.remove(record["id"]))

    def test_unused_code_expires(self):
        phones = phones_mod.Phones(self.path)
        _, key = phones.pair(now=1000.0)
        self.assertIsNotNone(phones.check(key, now=1000.0 + 599))
        self.assertIsNone(phones.check(key, now=1000.0 + 601))
        self.assertEqual(phones.listed(now=1000.0 + 601), [])

    def test_used_phone_does_not_expire(self):
        phones = phones_mod.Phones(self.path)
        _, key = phones.pair(now=1000.0)
        phones.seen(phones.check(key, now=1001.0), "192.168.0.31", "", now=1001.0)
        self.assertIsNotNone(phones.check(key, now=1000.0 + 86400 * 30))

    def test_peers(self):
        self.assertTrue(phones_mod.allowed_peer("127.0.0.1", lan=False))
        self.assertTrue(phones_mod.allowed_peer("100.90.10.20", lan=False))
        self.assertFalse(phones_mod.allowed_peer("192.168.0.31", lan=False))
        self.assertTrue(phones_mod.allowed_peer("192.168.0.31", lan=True))
        self.assertFalse(phones_mod.allowed_peer("8.8.8.8", lan=True))
        self.assertFalse(phones_mod.allowed_peer("172.32.0.1", lan=True))

    def test_pairing_text(self):
        text = phones_mod.pairing_text("K3y_-x", 47613, "my-desktop",
                                       [("tailscale", "100.101.102.103"), ("lan", "192.168.0.20")])
        url = urlparse(text)
        self.assertEqual((url.scheme, url.netloc), ("routinepanel", "pair"))
        q = parse_qs(url.query)
        self.assertEqual(q["k"], ["K3y_-x"])
        self.assertEqual(q["a"], ["100.101.102.103,192.168.0.20"])
        self.assertEqual(q["p"], ["47613"])
        self.assertEqual(q["n"], ["my-desktop"])


class ServiceServerTest(unittest.TestCase):
    """The service's server, asked from this computer: the window's key may pair; a phone's key may only read."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.phones = phones_mod.Phones(Path(self.folder.name) / "phones.json")
        self.server = PanelServer(FakeCollector(), "window-key", 0, remote=True, lan=True, phones=self.phones,
                                  service=True)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()
        self.folder.cleanup()

    def ask(self, method, path, key=None, body=None, address="127.0.0.1", host=None):
        connection = http.client.HTTPConnection(address, self.port, timeout=5)
        headers = {"Host": host or f"routine-panel.localhost:{self.port}"}
        if key:
            headers[KEY_HEADER] = key
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        answer = connection.getresponse()
        raw = answer.read()
        connection.close()
        try:
            return answer.status, json.loads(raw)
        except ValueError:
            return answer.status, raw

    def test_window_pairs_phone_reads(self):
        status, state = self.ask("GET", "/api/state", "window-key")
        self.assertEqual((status, state["role"], state["phones"]["service"]), (200, "window", True))
        status, pairing = self.ask("POST", "/api/phones/pair", "window-key", {})
        if status == 503:
            self.skipTest("this computer has no Tailscale or home address")
        self.assertEqual(status, 200)
        self.assertTrue(pairing["svg"].startswith("<svg"))
        key = parse_qs(urlparse(pairing["text"]).query)["k"][0]
        # the phone's key reads …
        status, state = self.ask("GET", "/api/state", key)
        self.assertEqual((status, state["role"]), (200, "phone"))
        status, ping = self.ask("GET", "/api/ping", key)
        self.assertEqual((status, ping["role"]), (200, "phone"))
        # … and nothing else
        self.assertEqual(self.ask("POST", "/api/phones/pair", key, {})[0], 403)
        self.assertEqual(self.ask("POST", "/api/phones/remove", key, {"id": pairing["phone"]["id"]})[0], 403)
        self.assertEqual(self.ask("GET", "/api/phones", key)[0], 404)
        # the window removes it: the key stops working at once
        status, listed = self.ask("GET", "/api/phones", "window-key")
        self.assertEqual(len(listed["phones"]), 1)
        self.assertEqual(self.ask("POST", "/api/phones/remove", "window-key", {"id": pairing["phone"]["id"]})[0], 200)
        self.assertEqual(self.ask("GET", "/api/state", key)[0], 401)

    def test_refusals(self):
        self.assertEqual(self.ask("GET", "/api/state")[0], 401)
        self.assertEqual(self.ask("GET", "/api/state", "nope")[0], 401)
        self.assertEqual(self.ask("GET", "/api/state", "window-key", host="evil.example")[0], 403)
        self.assertEqual(self.ask("PUT", "/api/state", "window-key")[0], 405)
        self.assertEqual(self.ask("POST", "/api/phones/pair", "window-key", body=None)[0] in (200, 503), True)

    def test_window_key_only_from_this_computer(self):
        found = phones_mod.addresses(lan=True)
        lan = [a for k, a in found if k == "lan"]
        if not lan:
            self.skipTest("no home network address")
        # from the computer's own home-network address (not loopback): Host must name that address, and the window's
        # key is not accepted there
        status, _ = self.ask("GET", "/api/state", "window-key", address=lan[0], host=f"{lan[0]}:{self.port}")
        self.assertEqual(status, 401)
        status, _ = self.ask("GET", "/", address=lan[0], host=f"routine-panel.localhost:{self.port}")
        self.assertEqual(status, 403)
        status, _ = self.ask("GET", "/", address=lan[0], host=f"{lan[0]}:{self.port}")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
