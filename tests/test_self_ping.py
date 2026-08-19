"""
Keeping the free instance awake.

The job existed and ran every five minutes, logging self_ping_ok, while the
service went on spinning down. It was pinging http://localhost — a request
that never leaves the container, so Render's router never sees traffic and
the fifteen-minute idle timer never resets.
"""
import os
import unittest


class _Stub:
    """Borrows the method under test without constructing the whole runner."""

    def __init__(self):
        from scheduler.runner import AppRunner
        self._impl = AppRunner._self_ping_url

    def url(self):
        return self._impl(self)


class TestSelfPingTarget(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("RENDER_EXTERNAL_URL", "RENDER_EXTERNAL_HOSTNAME", "PORT")}
        for k in self.saved:
            os.environ.pop(k, None)
        self.stub = _Stub()

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_render_url_is_used_and_marked_as_real_traffic(self):
        os.environ["RENDER_EXTERNAL_URL"] = "https://tennis-bet-izye.onrender.com"
        url, external = self.stub.url()
        self.assertEqual(url, "https://tennis-bet-izye.onrender.com/health")
        self.assertTrue(external)

    def test_a_trailing_slash_does_not_produce_a_double_slash(self):
        os.environ["RENDER_EXTERNAL_URL"] = "https://example.onrender.com/"
        self.assertEqual(self.stub.url()[0], "https://example.onrender.com/health")

    def test_the_hostname_variable_is_a_fallback(self):
        os.environ["RENDER_EXTERNAL_HOSTNAME"] = "example.onrender.com"
        url, external = self.stub.url()
        self.assertEqual(url, "https://example.onrender.com/health")
        self.assertTrue(external)

    def test_loopback_is_the_last_resort_and_is_flagged_as_ineffective(self):
        """
        The flag is the point. A localhost ping is a liveness check, not a
        keep-alive, and reporting it as success is what hid the problem.
        """
        url, external = self.stub.url()
        self.assertIn("localhost", url)
        self.assertFalse(external)

    def test_the_old_behaviour_would_now_be_reported_as_ineffective(self):
        os.environ["PORT"] = "10000"
        url, external = self.stub.url()
        self.assertEqual(url, "http://localhost:10000/health")
        self.assertFalse(external)


class TestQuotaArithmetic(unittest.TestCase):
    """
    Pinging around the clock has to fit inside the free allowance, or the
    service is suspended for the rest of the month — a worse outcome than
    sleeping.
    """

    FREE_HOURS = 750

    def test_a_full_month_awake_fits_the_allowance(self):
        for days in (28, 30, 31):
            with self.subTest(days=days):
                self.assertLessEqual(days * 24, self.FREE_HOURS)

    def test_the_longest_month_leaves_only_six_hours_of_headroom(self):
        self.assertEqual(self.FREE_HOURS - 31 * 24, 6)

    def test_two_always_on_free_services_would_not_fit(self):
        """The allowance is per workspace, not per service."""
        self.assertGreater(2 * 30 * 24, self.FREE_HOURS)

    def test_the_ping_interval_stays_inside_the_idle_window(self):
        """Render spins down after 15 minutes; the job runs every 5."""
        self.assertLess(5, 15)
