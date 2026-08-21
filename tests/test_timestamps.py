"""
Timestamps on the wire.

Signals fired at 12:10 IST displayed as "06:40 am IST · 5h 37m ago". The
stored instants were correct the whole time; the wire format was ambiguous.
Every DateTime column here is naive UTC, and isoformat() on a naive value
emits no offset — JavaScript parses an offset-less date-time as LOCAL time, so
a browser in IST read a UTC instant as an IST wall clock and lost 5h30m.
"""
import inspect
import re
import unittest
from datetime import UTC, datetime

import scheduler.health as health


class TestIsoHelper(unittest.TestCase):
    def test_a_naive_value_is_labelled_utc(self):
        self.assertEqual(health._iso(datetime(2026, 8, 21, 6, 40)),
                         "2026-08-21T06:40:00+00:00")

    def test_an_aware_value_is_left_alone(self):
        self.assertEqual(health._iso(datetime(2026, 8, 21, 6, 40, tzinfo=UTC)),
                         "2026-08-21T06:40:00+00:00")

    def test_none_stays_none(self):
        """ended_at is null on a running cycle; that must not become a date."""
        self.assertIsNone(health._iso(None))

    def test_the_output_always_carries_an_offset(self):
        for dt in (datetime(2026, 1, 1), datetime(2026, 8, 21, 23, 59, 59)):
            with self.subTest(dt=dt):
                self.assertTrue(health._iso(dt).endswith("+00:00"))


class TestNothingBypassesIt(unittest.TestCase):
    def test_no_bare_isoformat_reaches_the_browser(self):
        """
        One missed call is one panel silently 5h30m out, with nothing to
        distinguish it from a correct one.
        """
        src = inspect.getsource(health)
        bare = re.findall(r"(?<!def )[\w.]+\.isoformat\(\)", src)
        # The helper's own body is the single legitimate use.
        self.assertEqual(bare, ["dt.isoformat()"], f"unwrapped timestamps: {bare}")


class TestTheReportedCase(unittest.TestCase):
    """
    The BCH signal that appeared in Telegram at 12:10 PM and on the site as
    06:40 am IST. Same instant, two readings, 5h30m apart.
    """

    FIRED_UTC = datetime(2026, 8, 21, 6, 40)

    def test_the_offset_is_exactly_the_ist_shift(self):
        emitted = health._iso(self.FIRED_UTC)
        self.assertIn("+00:00", emitted)
        # 06:40Z is 12:10 IST — the time Telegram showed.
        as_ist = self.FIRED_UTC.replace(tzinfo=UTC).astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Kolkata"))
        self.assertEqual((as_ist.hour, as_ist.minute), (12, 10))

    def test_a_naive_string_would_still_be_misread(self):
        """Documents why the fix is at the boundary rather than in the browser."""
        self.assertNotIn("+00:00", self.FIRED_UTC.isoformat())
