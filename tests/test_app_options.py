import argparse
import unittest

from routine_panel import app


class StartOptionsTest(unittest.TestCase):
    def test_options_follow_the_key(self):
        def opts(**kw):
            return app.start_options(argparse.Namespace(**{"theme": None, "view": None, **kw}))
        self.assertEqual(opts(), "")
        self.assertEqual(opts(theme="light"), "&theme=light")
        self.assertEqual(opts(theme="dark", view="grid"), "&theme=dark&view=grid")


if __name__ == "__main__":
    unittest.main()
