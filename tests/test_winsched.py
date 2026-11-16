import unittest

from tests.helpers import local

from routine_panel import winsched

DATA = {
    "me": "user",
    "claude": ["C:\\Program Files\\WindowsApps\\Claude_1.2.3.0_x64__pzs8sxrjxfjjc\\app\\claude.exe"],
    "tasks": [
        {"name": "Backup photos", "path": "\\", "state": "Ready", "about": "Copies the photos to the NAS",
         "author": "PC\\user", "user": "user", "actions": ["C:\\tools\\backup.exe --photos"],
         "triggers": [{"kind": "MSFT_TaskDailyTrigger", "start": "2026-11-01T04:30:00", "enabled": True, "days": 1}],
         "last": "2026-11-16T02:30:05.0000000Z", "next": "2026-11-17T02:30:00.0000000Z", "result": 0, "missed": 0},
        {"name": "Sync", "path": "\\Mine\\", "state": "Running", "author": "user",
         "triggers": [{"kind": "MSFT_TaskTimeTrigger", "start": "2026-11-01T00:00:00", "every": "PT15M"}],
         "last": "2026-11-16T11:00:00Z", "next": None, "result": 267009},
        {"name": "Broken", "path": "\\", "state": "Ready", "author": "PC\\user",
         "triggers": [{"kind": "MSFT_TaskWeeklyTrigger", "start": "2026-11-02T11:00:00", "dow": 2, "delay": "PT30M"}],
         "last": "2026-11-16T09:00:00Z", "next": "2026-11-23T09:00:00Z", "result": 2147942402},
        {"name": "Old", "path": "\\", "state": "Disabled", "author": "PC\\user",
         "triggers": [{"kind": "MSFT_TaskLogonTrigger"}], "last": "1999-11-30T00:00:00Z", "result": 267011},
        {"name": "GoogleUpdaterTaskSystem", "path": "\\", "state": "Ready", "author": "Google LLC",
         "triggers": [{"kind": "MSFT_TaskDailyTrigger", "start": "2026-11-01T10:00:00"}], "result": 0},
    ],
}


class WinSchedTest(unittest.TestCase):
    def rows(self, show_all=False):
        return {r["name"]: r for r in winsched.items(DATA, show_all, local("2026-11-16 13:00:00"))}

    def test_own_tasks_only(self):
        self.assertEqual(set(self.rows()), {"Backup photos", "Mine › Sync", "Broken", "Old"})
        self.assertIn("GoogleUpdaterTaskSystem", self.rows(show_all=True))

    def test_states(self):
        rows = self.rows()
        backup = rows["Backup photos"]
        self.assertEqual(backup["status"], "waiting")
        self.assertEqual(backup["schedule"], "daily 04:30")
        self.assertEqual(backup["last"]["result"], "finished")
        self.assertTrue(backup["last"]["ok"])
        self.assertEqual(backup["id"], "task:\\Backup photos")
        sync = rows["Mine › Sync"]
        self.assertEqual(sync["status"], "running")
        self.assertEqual(sync["next_note"], "after this run")
        self.assertEqual(sync["schedule"], "every 15 minutes")
        self.assertEqual(sync["id"], "task:\\Mine\\Sync")
        broken = rows["Broken"]
        self.assertEqual(broken["status"], "fault")
        self.assertEqual(broken["reason"], "Last run ended with code 0x80070002")
        self.assertEqual(broken["schedule"], "Mondays 11:00 (+ up to 30 minutes at random)")
        self.assertTrue(broken["next_approx"])
        old = rows["Old"]
        self.assertEqual(old["status"], "off")
        self.assertIsNone(old["last"])  # 1999 = never
        self.assertEqual(old["schedule"], "at log on")

    def test_claude_app(self):
        self.assertTrue(winsched.claude_running(DATA))
        self.assertFalse(winsched.claude_running({"claude": ["C:\\Users\\u\\.local\\bin\\claude.exe"]}))
        self.assertIsNone(winsched.claude_running({}))

    def test_words(self):
        self.assertEqual(winsched._span("PT1H30M"), 5400)
        self.assertEqual(winsched._span("P1D"), 86400)
        self.assertIsNone(winsched._span("soon"))
        self.assertEqual(winsched.describe([{"kind": "MSFT_TaskDailyTrigger", "start": "2026-11-01T09:00:00",
                                             "every": "PT1H"}]), "daily 09:00, then every hour")
        self.assertEqual(winsched.describe([{"kind": "MSFT_TaskWeeklyTrigger", "start": "2026-11-02T07:30:00",
                                             "dow": 0b0111110}]), "weekdays 07:30")
        self.assertEqual(winsched.describe([]), "no schedule")


if __name__ == "__main__":
    unittest.main()
