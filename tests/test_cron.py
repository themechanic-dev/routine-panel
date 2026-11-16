import unittest

from tests.helpers import local

from routine_panel import cron


class CronTest(unittest.TestCase):
    def test_daily(self):
        c = cron.parse("0 5 * * *")
        self.assertEqual(c.next_after(local("2026-11-16 13:00:00")), local("2026-11-17 05:00:00"))
        self.assertEqual(c.next_after(local("2026-11-16 04:59:59")), local("2026-11-16 05:00:00"))
        self.assertEqual(c.next_after(local("2026-11-16 05:00:00")), local("2026-11-17 05:00:00"))  # strictly after
        self.assertEqual(c.describe(), "daily 05:00")

    def test_hourly_and_steps(self):
        c = cron.parse("17 * * * *")
        self.assertEqual(c.next_after(local("2026-11-16 13:17:25")), local("2026-11-16 14:17:00"))
        self.assertEqual(c.last_at_or_before(local("2026-11-16 13:16:59")), local("2026-11-16 12:17:00"))
        self.assertEqual(c.describe(), "hourly at :17")
        c = cron.parse("*/15 * * * *")
        self.assertEqual(c.next_after(local("2026-11-16 13:00:00")), local("2026-11-16 13:15:00"))
        self.assertEqual(c.describe(), "every 15 minutes")
        self.assertEqual(cron.parse("* * * * *").describe(), "every minute")
        self.assertEqual(cron.parse("5 */2 * * *").describe(), "every 2 hours at :05")

    def test_weekdays_and_names(self):
        c = cron.parse("0 11 * * mon")
        self.assertEqual(c.next_after(local("2026-11-16 12:00:00")), local("2026-11-23 11:00:00"))  # 16/11 is a Monday
        self.assertEqual(c.describe(), "Mondays 11:00")
        self.assertEqual(cron.parse("30 7 * * 1-5").describe(), "weekdays 07:30")
        self.assertEqual(cron.parse("0 9 * * 0").weekdays, cron.parse("0 9 * * 7").weekdays)  # 7 is Sunday too

    def test_day_fields_or(self):
        # both day fields given: either matches (Vixie cron)
        c = cron.parse("0 12 1 * 3")  # the 1st of the month, or Wednesdays
        self.assertEqual(c.next_after(local("2026-11-16 13:00:00")), local("2026-11-18 12:00:00"))  # Wed 18/11
        self.assertEqual(c.next_after(local("2026-11-25 13:00:00")), local("2026-12-01 12:00:00"))

    def test_bad(self):
        for text in ("", "* * * *", "61 * * * *", "*/0 * * * *", "x * * * *", "5-1 * * * *"):
            with self.assertRaises(cron.CronError, msg=text):
                cron.parse(text)
        self.assertEqual(cron.parse("1,2 3 * 1 *").describe(), "cron 1,2 3 * 1 *")


if __name__ == "__main__":
    unittest.main()
