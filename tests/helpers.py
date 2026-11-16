"""What the tests share: the computer's time zone fixed to Athens (cron and logs are in local time), and epoch
seconds for a local date and time."""

import datetime as dt
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

os.environ["TZ"] = "Europe/Athens"
if hasattr(time, "tzset"):  # not on Windows: there the tests run in the computer's own zone
    time.tzset()


def local(text: str) -> float:
    """Epoch seconds for "2026-11-16 13:17:00" in local (Athens) time."""
    return dt.datetime.fromisoformat(text).timestamp()


def utc_iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
