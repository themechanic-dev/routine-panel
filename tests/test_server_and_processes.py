import http.client
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import local  # noqa: F401 - fixes the time zone

from routine_panel import processes
from routine_panel.server import KEY_HEADER, PanelServer


class FakeCollector:
    def state(self):
        return {"now": 1.0, "sections": [], "counts": {}, "errors": []}


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.server = PanelServer(FakeCollector(), "secret-key", 0)
        self.server.start()
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()

    def ask(self, method, path, headers=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        for name, value in (headers or {}).items():
            connection.putheader(name, value)
        connection.endheaders()
        answer = connection.getresponse()
        body = answer.read()
        connection.close()
        return answer, body

    def test_page_and_files(self):
        for path in ("/", "/app.js", "/style.css", "/icon.svg"):
            answer, body = self.ask("GET", path)
            self.assertEqual(answer.status, 200, path)
            self.assertTrue(body)
            self.assertIn("default-src 'self'", answer.getheader("Content-Security-Policy"))
        self.assertEqual(self.ask("GET", "/../routine_panel/app.py")[0].status, 404)
        self.assertEqual(self.ask("GET", "/settings.json")[0].status, 404)

    def test_state_needs_the_key(self):
        self.assertEqual(self.ask("GET", "/api/state")[0].status, 401)
        self.assertEqual(self.ask("GET", "/api/state", {KEY_HEADER: "wrong"})[0].status, 401)
        answer, body = self.ask("GET", "/api/state", {KEY_HEADER: "secret-key"})
        self.assertEqual(answer.status, 200)
        self.assertEqual(json.loads(body)["now"], 1.0)
        # the window's own host name
        answer, _ = self.ask("GET", "/api/state", {KEY_HEADER: "secret-key"},
                             host=f"routine-panel.localhost:{self.port}")
        self.assertEqual(answer.status, 200)

    def test_other_hosts_are_turned_away(self):
        self.assertEqual(self.ask("GET", "/api/state", {KEY_HEADER: "secret-key"}, host="evil.example:80")[0].status, 403)
        self.assertEqual(self.ask("GET", "/", host="127.0.0.1.nip.io")[0].status, 403)

    def test_only_reads(self):
        for method in ("PUT", "DELETE", "PATCH"):
            answer, _ = self.ask(method, "/api/state", {KEY_HEADER: "secret-key", "Content-Length": "0"})
            self.assertEqual(answer.status, 405, method)
        # POST exists only for pairing phones, and the window's own server (no service) cannot pair
        answer, _ = self.ask("POST", "/api/state", {KEY_HEADER: "secret-key", "Content-Length": "0"})
        self.assertEqual(answer.status, 404)
        answer, body = self.ask("POST", "/api/phones/pair", {KEY_HEADER: "secret-key", "Content-Length": "0"})
        self.assertEqual(answer.status, 409)
        answer, _ = self.ask("POST", "/api/phones/pair", {"Content-Length": "0"})
        self.assertEqual(answer.status, 401)


class ProcessesTest(unittest.TestCase):
    def test_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            beat = Path(folder) / "heartbeat"
            beat.write_text("x")
            now = time.time()
            os.utime(beat, (now - 30, now - 30))
            services = {"w.service": {"Id": ["w.service"], "ActiveState": ["active"], "LoadState": ["loaded"],
                                      "ExecMainStartTimestamp": [f"@{int(now - 3600)}"], "Description": ["Worker"]}}
            entry = {"unit": "w.service", "heartbeat": str(beat), "every_s": 60}
            [item] = processes.process_items([entry], services, [], now)
            self.assertEqual(item["status"], "running")
            self.assertAlmostEqual(item["next"], now + 30, places=0)
            self.assertEqual(item["about"], "Worker")
            os.utime(beat, (now - 600, now - 600))
            [item] = processes.process_items([entry], services, [], now)
            self.assertEqual(item["status"], "fault")
            self.assertIn("No heartbeat for 10 min", item["reason"])
            services["w.service"]["ActiveState"] = ["failed"]
            services["w.service"]["Result"] = ["exit-code"]
            [item] = processes.process_items([entry], services, [], now)
            self.assertEqual(item["status"], "fault")
            [item] = processes.process_items([{"unit": "gone.service"}], {}, [], now)
            self.assertEqual(item["status"], "fault")

    def test_claude_app_detection(self):
        rewritten = [(1, ["/usr/lib/claude-desktop/claude-desktop --startup"])]
        helper = [(2, ["/usr/lib/claude-desktop/claude-desktop", "--type=renderer"])]
        self.assertTrue(processes.claude_app_running(rewritten, ("claude-desktop",)))
        self.assertFalse(processes.claude_app_running(helper, ("claude-desktop",)))
        self.assertIsNone(processes.claude_app_running(None, ("claude-desktop",)))


if __name__ == "__main__":
    unittest.main()
