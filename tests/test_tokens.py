import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import local

from routine_panel import tokens

LOG = """\
2026-11-15 15:01:01,077 INFO my_app.writer: asked claude-opus-5-5: 56687 tokens in, 438 out, 8.9 s
2026-11-15 15:01:13,038 INFO my_app.writer: asked claude-opus-5-5: 56687 tokens in, 493 out, 10.1 s
2026-11-15 15:01:22,824 INFO my_app.writer: asked claude-opus-5-5: 35184 tokens in, 684 out, 8.4 s
2026-11-16 07:51:38,955 INFO my_app.writer: asked claude-opus-5-5: 4241 tokens in, 2988 out, 34.5 s
2026-11-16 07:51:39,416 INFO my_app.run: done: 4 new
2026-11-16 12:13:40,815 INFO my_app.writer: asked claude-opus-5-5: 6058 tokens in, 3512 out, 37.7 s
2026-11-16 12:14:20,000 INFO my_app.writer: asked claude-opus-5-5: 1000 tokens in, 10 out, 5.0 s
"""


class LogTokensTest(unittest.TestCase):
    def test_parse(self):
        calls = tokens.parse_log(LOG)
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0].usage.input, 56687)
        self.assertEqual(calls[0].usage.models, ["claude-opus-5-5"])
        self.assertAlmostEqual(calls[0].end, local("2026-11-15 15:01:01"), places=0)
        self.assertAlmostEqual(calls[0].seconds, 8.9)
        self.assertFalse(calls[0].usage.cache_known)

    def test_cycle_from_journal_runs(self):
        calls = tokens.parse_log(LOG)
        # runs started at 12:13:00 and 12:14:10: the last call belongs to the second run only
        usage = tokens.last_cycle(calls, [local("2026-11-16 12:13:00"), local("2026-11-16 12:14:10")])
        self.assertEqual((usage.input, usage.output, usage.calls), (1000, 10, 1))
        # one run from 12:13:00: both calls
        usage = tokens.last_cycle(calls, [local("2026-11-16 12:13:00")])
        self.assertEqual((usage.input, usage.output, usage.calls), (7058, 3522, 2))

    def test_cycle_without_journal(self):
        calls = tokens.parse_log(LOG)[:3]  # 15:01, three calls close together
        usage = tokens.last_cycle(calls, [])
        self.assertEqual((usage.input, usage.output, usage.calls), (148558, 1615, 3))
        self.assertEqual(usage.total, 148558 + 1615)

    def test_own_pattern(self):
        text = "2026-11-16T10:00:00 call model=x in=5 out=6 cr=7 cw=8\n"
        pattern = (r"^(?P<ts>\S+) call model=(?P<model>\S+) in=(?P<input>\d+) out=(?P<output>\d+) "
                   r"cr=(?P<cache_read>\d+) cw=(?P<cache_write>\d+)")
        [call] = tokens.parse_log(text, pattern)
        self.assertEqual((call.usage.input, call.usage.output, call.usage.cache_read, call.usage.cache_write),
                         (5, 6, 7, 8))
        self.assertTrue(call.usage.cache_known)
        self.assertEqual(tokens.parse_log(text, "(unclosed"), [])


class TranscriptTest(unittest.TestCase):
    def test_sum_counts_each_message_once(self):
        lines = [
            {"type": "queue-operation", "timestamp": "2026-11-16T10:26:06.820Z"},
            {"type": "assistant", "timestamp": "2026-11-16T10:26:11.846Z",
             "message": {"id": "msg_a", "model": "claude-opus-5-5", "content": [{"type": "thinking", "thinking": ""}],
                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 48280,
                                   "cache_read_input_tokens": 41221, "output_tokens": 113}}},
            # the same answer again, its next part (same id): not counted twice
            {"type": "assistant", "timestamp": "2026-11-16T10:26:11.900Z",
             "message": {"id": "msg_a", "model": "claude-opus-5-5", "content": [{"type": "tool_use"}],
                         "usage": {"input_tokens": 2, "cache_creation_input_tokens": 48280,
                                   "cache_read_input_tokens": 41221, "output_tokens": 113}}},
            {"type": "user", "timestamp": "2026-11-16T10:26:12.000Z", "message": {"content": "x"}},
            {"type": "assistant", "timestamp": "2026-11-16T10:26:16.835Z",
             "message": {"id": "msg_b", "model": "claude-opus-5-5",
                         "content": [{"type": "text", "text": "worker running"}],
                         "usage": {"input_tokens": 4, "cache_creation_input_tokens": 1962,
                                   "cache_read_input_tokens": 180802, "output_tokens": 168}}},
            {"type": "assistant", "timestamp": "2026-11-16T10:26:17.000Z",
             "message": {"id": "msg_c", "model": "<synthetic>", "content": [{"type": "text", "text": "x"}],
                         "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ]
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "-home-me-projects-worker"
            project.mkdir()
            path = project / "abc-123.jsonl"
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n", encoding="utf-8")
            reader = tokens.Transcripts(Path(folder))
            result = reader.read("abc-123")
            usage = result["usage"]
            self.assertEqual((usage.input, usage.cache_write, usage.cache_read, usage.output),
                             (6, 50242, 222023, 281))
            self.assertEqual(usage.calls, 3)  # msg_a, msg_b and the synthetic one
            self.assertEqual(usage.models, ["claude-opus-5-5"])
            self.assertEqual(usage.total, 272552)
            self.assertEqual(result["final"], "x")
            j = usage.as_json()
            self.assertEqual((j["in"], j["cache_read"], j["out"]), (50248, 222023, 281))
            self.assertIsNone(reader.read("missing"))
            self.assertIsNone(reader.read("../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
