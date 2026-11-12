import unittest

from services.otc_supervisor import restart_reason


class SupervisorTests(unittest.TestCase):
    def test_restarts_stalled_loop_but_not_manual_stop_or_quota(self):
        now = 1000
        self.assertEqual(restart_reason({}, now, 0, "run"), "heartbeat_stalled")
        healthy = dict(
            at=now, run_id="run", running=True, quote_age=1, catalogue_age=1, error=""
        )
        self.assertIsNone(restart_reason(healthy, now, 0, "run"))
        stopped = dict(healthy, running=False, user_stopped=True)
        self.assertIsNone(restart_reason(stopped, now, 0, "run"))
        quota = dict(healthy, running=False, error="quota exhausted")
        self.assertIsNone(restart_reason(quota, now, 0, "run"))
        self.assertEqual(
            restart_reason(dict(healthy, catalogue_age=400), now, 0, "run"),
            "market_metadata_stalled",
        )
        transient = dict(healthy, running=False, error="после повторных попыток")
        self.assertEqual(
            restart_reason(transient, now, 0, "run"), "transient_session_stopped"
        )

    def test_startup_grace_period_avoids_restart_storm(self):
        self.assertIsNone(restart_reason(None, 100, 0, "run"))
