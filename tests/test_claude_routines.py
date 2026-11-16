import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import local, utc_iso

from routine_panel.claude_routines import ClaudeRoutines
from routine_panel.tokens import Transcripts


class ClaudeRoutinesTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.sessions = root / "sessions"
        self.account = self.sessions / "org" / "acct"
        self.account.mkdir(parents=True)
        self.projects = root / "projects"
        (self.projects / "-p").mkdir(parents=True)
        skill = root / "SKILL.md"
        skill.write_text("---\nname: help\ndescription: Hourly: keep the worker alive.\n---\nbody\n", encoding="utf-8")
        self.skill = skill
        self.reader = ClaudeRoutines(self.sessions, Transcripts(self.projects))

    def tearDown(self):
        self.folder.cleanup()

    def tasks(self, *tasks):
        (self.account / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": list(tasks)}), encoding="utf-8")

    def run_file(self, name, **fields):
        (self.account / f"local_{name}.json").write_text(json.dumps(fields), encoding="utf-8")

    def transcript(self, session, total_out):
        line = {"type": "assistant", "timestamp": "2026-11-16T10:26:16.835Z",
                "message": {"id": "m1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}],
                            "usage": {"input_tokens": 6, "cache_creation_input_tokens": 50242,
                                      "cache_read_input_tokens": 222023, "output_tokens": total_out}}}
        (self.projects / "-p" / f"{session}.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    def help_task(self, **more):
        task = {"id": "keep-worker-alive", "displayName": "Keep the worker alive", "cronExpression": "17 * * * *",
                "enabled": True, "filePath": str(self.skill), "createdAt": local("2026-11-09 12:00:00") * 1000,
                "lastScheduledFor": utc_iso(local("2026-11-16 12:17:00")),
                "lastRunAt": utc_iso(local("2026-11-16 12:26:05"))}
        task.update(more)
        return task

    def item(self, now, app=True):
        [item] = self.reader.items(now, app)
        return item

    def test_waiting_with_usual_delay(self):
        self.tasks(self.help_task())
        start = local("2026-11-16 12:26:05") * 1000
        self.run_file("a", scheduledTaskId="keep-worker-alive", createdAt=start, latestUserFrameAt=start + 1000,
                      lastTurnEdgeAt=start + 14000, completedTurns=1, cliSessionId="s1", model="claude-opus-5-5",
                      postTurnSummary={"status_category": "review_ready", "status_detail": "worker running"})
        self.transcript("s1", 281)
        item = self.item(local("2026-11-16 13:20:00"))
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["about"], "Hourly: keep the worker alive.")
        self.assertEqual(item["schedule"], "hourly at :17 (starts ~9 min later)")
        self.assertEqual(item["next"], local("2026-11-16 13:26:05"))
        self.assertTrue(item["next_approx"])
        self.assertEqual(item["last"]["result"], "worker running")
        self.assertTrue(item["last"]["ok"])
        self.assertAlmostEqual(item["last"]["end"] - item["last"]["start"], 14.0)
        self.assertEqual(item["tokens"]["total"], 272552)
        self.assertFalse(item["tokens"]["live"])

    def test_running(self):
        self.tasks(self.help_task(lastScheduledFor=utc_iso(local("2026-11-16 13:17:00")),
                                  lastRunAt=utc_iso(local("2026-11-16 13:26:05"))))
        start = local("2026-11-16 13:26:05") * 1000
        # as the app writes it at the start (measured 16/11 13:26:05): no prompt time, no completed turn yet
        self.run_file("b", scheduledTaskId="keep-worker-alive", createdAt=start, lastActivityAt=start,
                      lastTurnEdgeAt=start, cliSessionId="s2")
        self.transcript("s2", 100)
        item = self.item(local("2026-11-16 13:26:20"))
        self.assertEqual(item["status"], "running")
        self.assertAlmostEqual(item["since"], start / 1000)
        self.assertEqual(item["next"], local("2026-11-16 14:26:05"))
        self.assertTrue(item["tokens"]["live"])
        self.assertIsNone(item["last"]["ok"])
        # and as it is written at the end of the turn (13:26:29)
        self.run_file("b", scheduledTaskId="keep-worker-alive", createdAt=start, lastTurnEdgeAt=start + 23639,
                      latestUserFrameAt=start + 1075, completedTurns=1, cliSessionId="s2",
                      postTurnSummary={"status_category": "completed", "status_detail": "worker already running"})
        item = self.item(local("2026-11-16 13:26:40"))
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["last"]["result"], "worker already running")
        self.assertAlmostEqual(item["last"]["end"] - item["last"]["start"], 23.639, places=3)
        self.assertFalse(item["tokens"]["live"])
        # a second prompt in the same session opens the turn again
        self.run_file("b", scheduledTaskId="keep-worker-alive", createdAt=start, lastTurnEdgeAt=start + 23639,
                      latestUserFrameAt=start + 60000, completedTurns=1, cliSessionId="s2")
        self.assertEqual(self.item(local("2026-11-16 13:27:10"))["status"], "running")
        # a run left open for longer than any run lasts is not shown as running
        self.run_file("b", scheduledTaskId="keep-worker-alive", createdAt=start, lastTurnEdgeAt=start, cliSessionId="s2")
        self.assertNotEqual(self.item(local("2026-11-16 14:20:00"))["status"], "running")

    def test_fired_but_no_file_yet(self):
        self.tasks(self.help_task(lastScheduledFor=utc_iso(local("2026-11-16 13:17:00")),
                                  lastRunAt=utc_iso(local("2026-11-16 13:26:05"))))
        item = self.item(local("2026-11-16 13:26:10"))
        self.assertEqual(item["status"], "running")
        self.assertEqual(item["last"]["result"], "starting")

    def test_error_is_a_fault(self):
        self.tasks(self.help_task())
        start = local("2026-11-16 12:26:05") * 1000
        self.run_file("c", scheduledTaskId="keep-worker-alive", createdAt=start, latestUserFrameAt=start + 1000,
                      error="You've hit your 5-hour limit", errorAt=start + 3000, lastTurnCutShort=True)
        item = self.item(local("2026-11-16 12:40:00"))
        self.assertEqual(item["status"], "fault")
        self.assertEqual(item["reason"], "You've hit your 5-hour limit")
        self.assertFalse(item["last"]["ok"])
        self.assertEqual(item["tokens"], {"none_yet": True})

    def test_blocked_needs_the_person_not_a_fault(self):
        self.tasks(self.help_task())
        start = local("2026-11-16 12:26:05") * 1000
        self.run_file("d", scheduledTaskId="keep-worker-alive", createdAt=start, latestUserFrameAt=start,
                      lastTurnEdgeAt=start + 9000, completedTurns=1,
                      postTurnSummary={"status_category": "blocked", "status_detail": "waits",
                                       "needs_action": "answer the proposal"})
        item = self.item(local("2026-11-16 12:40:00"))
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["attention"], "answer the proposal")

    def test_missed_run(self):
        self.tasks(self.help_task())  # last run 12:26; at 14:00 the 13:17 run (due ~13:26) never came
        item = self.item(local("2026-11-16 14:00:00"))
        self.assertEqual(item["status"], "fault")
        self.assertIn("13:26", item["reason"])
        # not yet missed at 13:35 (within the grace)
        self.assertEqual(self.item(local("2026-11-16 13:35:00"))["status"], "waiting")

    def test_app_closed(self):
        self.tasks(self.help_task())
        item = self.item(local("2026-11-16 13:00:00"), app=False)
        self.assertEqual(item["status"], "fault")
        self.assertIn("Claude app is closed", item["reason"])

    def test_off_and_odd_files(self):
        self.tasks(self.help_task(enabled=False), {"id": "bad", "cronExpression": "nonsense"}, "not a task")
        (self.account / "local_broken.json").write_text("{", encoding="utf-8")
        items = {i["id"]: i for i in self.reader.items(local("2026-11-16 13:00:00"), True)}
        self.assertEqual(items["claude:keep-worker-alive"]["status"], "off")
        self.assertIsNone(items["claude:keep-worker-alive"]["next"])
        self.assertEqual(items["claude:bad"]["status"], "fault")
        self.assertIn("Schedule not understood", items["claude:bad"]["reason"])

    def test_nothing_known_is_not_missed(self):
        # a routine with no creation time, no last run and no run file: nothing says it should have run already
        self.tasks({"id": "new", "displayName": "New", "cronExpression": "7 * * * *", "enabled": True})
        item = self.item(local("2026-11-16 13:40:00"))
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["next"], local("2026-11-16 14:07:00"))
        self.assertEqual(item["tokens"], {"none_yet": True})

    def test_no_folder(self):
        reader = ClaudeRoutines(Path(self.folder.name) / "nowhere", Transcripts(self.projects))
        self.assertEqual(reader.items(local("2026-11-16 13:00:00"), True), [])


if __name__ == "__main__":
    unittest.main()
