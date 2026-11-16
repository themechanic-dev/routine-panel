import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import local  # noqa: F401 - fixes the time zone

from routine_panel import app


class PlacementTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.dirs = SimpleNamespace(data=Path(self.folder.name))

    def tearDown(self):
        self.folder.cleanup()

    def place(self, screen_arg=None, settings=None):
        return app._placement(SimpleNamespace(screen=screen_arg), self.dirs, settings or {})

    def test_first_time_from_settings(self):
        self.assertEqual(self.place(settings={"screen": 3, "window_mode": "fullscreen"}), (3, "fullscreen"))
        self.assertEqual(self.place(), (None, "normal"))
        self.assertEqual(self.place(settings={"screen": True, "window_mode": "huge"}), (None, "normal"))

    def test_kept_place_wins_over_settings(self):
        (self.dirs.data / app.PLACE_FILE).write_text(json.dumps({"screen": 1, "mode": "maximized"}))
        self.assertEqual(self.place(settings={"screen": 3, "window_mode": "fullscreen"}), (1, "maximized"))
        # --screen given at start wins over both; the kept mode stays
        self.assertEqual(self.place(screen_arg=0, settings={"screen": 3}), (0, "maximized"))

    def test_broken_file(self):
        (self.dirs.data / app.PLACE_FILE).write_text("{")
        self.assertEqual(self.place(settings={"screen": 3, "window_mode": "fullscreen"}), (3, "fullscreen"))


class ServiceKeyTest(unittest.TestCase):
    def test_kept_across_restarts(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            first = app._service_key(runtime)
            self.assertGreaterEqual(len(first), 24)
            (runtime / app.SERVICE_FILE).write_text(json.dumps({"port": 47613, "key": first, "pid": 1}))
            self.assertEqual(app._service_key(runtime), first)
            (runtime / app.SERVICE_FILE).write_text(json.dumps({"key": "short"}))
            self.assertNotEqual(app._service_key(runtime), "short")
            (runtime / app.SERVICE_FILE).write_text("{")
            self.assertGreaterEqual(len(app._service_key(runtime)), 24)


if __name__ == "__main__":
    unittest.main()
