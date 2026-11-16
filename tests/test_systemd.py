import unittest

from tests.helpers import local  # noqa: F401 - fixes the time zone

from routine_panel import systemd

SHOW = """Id=photo-sync.timer
TimersCalendar={ OnCalendar=*-*-* *:*:30 ; next_elapse=Mon 2026-11-16 13:08:30 EET }
RandomizedDelayUSec=0

Id=mirror-notes.timer
TimersMonotonic={ OnUnitActiveUSec=5min ; next_elapse=1d 22h 2min 21.733261s }
TimersMonotonic={ OnStartupUSec=1min ; next_elapse=1min 12.874847s }
RandomizedDelayUSec=0

Id=weekly-check.timer
TimersCalendar={ OnCalendar=Mon *-*-* 11:00:00 ; next_elapse=Mon 2026-11-23 11:00:00 EET }
RandomizedDelayUSec=30min
"""


class ShowTest(unittest.TestCase):
    def test_parse_and_words(self):
        units = systemd.parse_show(SHOW)
        self.assertEqual(set(units), {"photo-sync.timer", "mirror-notes.timer", "weekly-check.timer"})
        self.assertEqual(len(units["mirror-notes.timer"]["TimersMonotonic"]), 2)
        self.assertEqual(systemd.describe_timer(units["photo-sync.timer"]), "every minute at :30")
        self.assertEqual(systemd.describe_timer(units["mirror-notes.timer"]), "every 5 minutes")
        self.assertEqual(systemd.describe_timer(units["weekly-check.timer"]),
                         "Mondays 11:00 (+ up to 30 minutes at random)")

    def test_calendar_words(self):
        self.assertEqual(systemd.describe_calendar("*-*-* *:00/15:00"), "every 15 minutes")
        self.assertEqual(systemd.describe_calendar("*-*-* 04:30:00"), "daily 04:30")
        self.assertEqual(systemd.describe_calendar("*-*-* *:05:00"), "hourly at :05")
        self.assertEqual(systemd.describe_calendar("*-*-* *:*:00"), "every minute")
        self.assertEqual(systemd.describe_calendar("Sat,Sun *-*-* 10:00:00"), "Sat,Sun *-*-* 10:00:00")
        self.assertEqual(systemd.span_seconds("1h 30min"), 5400)
        self.assertEqual(systemd.span_seconds("0"), 0)

    def test_failure_words(self):
        self.assertIsNone(systemd.failure({"ActiveState": ["inactive"], "Result": ["success"]}))
        self.assertEqual(systemd.failure({"ActiveState": ["failed"], "Result": ["exit-code"], "ExecMainStatus": ["1"]}),
                         "Last run ended with an error (exit code 1)")
        self.assertEqual(systemd.failure({"ActiveState": ["failed"], "Result": ["timeout"]}),
                         "Last run ran past its time limit")
        self.assertEqual(systemd.timestamp({"X": ["@1794826834"]}, "X"), 1794826834)
        self.assertIsNone(systemd.timestamp({"X": [""]}, "X"))

    def test_runs_from_journal(self):
        entries = [
            {"t": 100.0, "text": "Starting x.service - X...", "job": "start", "result": None, "comm": "systemd"},
            {"t": 101.0, "text": "IDLE: nothing", "job": None, "result": None, "comm": "python3"},
            {"t": 101.5, "text": "Finished x.service - X.", "job": "start", "result": "done", "comm": "systemd"},
            {"t": 160.0, "text": "Starting x.service - X...", "job": "start", "result": None, "comm": "systemd"},
            {"t": 161.0, "text": "Traceback ...", "job": None, "result": None, "comm": "python3"},
            {"t": 161.2, "text": "x.service: Main process exited, code=exited, status=1/FAILURE", "job": None,
             "result": None, "comm": "systemd"},
            {"t": 161.3, "text": "Failed to start x.service - X.", "job": "start", "result": "failed", "comm": "systemd"},
        ]
        runs = systemd.runs_from(entries)
        self.assertEqual([(r.start, r.end) for r in runs], [(100.0, 101.5), (160.0, 161.3)])
        self.assertEqual(runs[0].lines, [(101.0, "IDLE: nothing")])
        self.assertEqual(len(runs[1].system), 2)


if __name__ == "__main__":
    unittest.main()
