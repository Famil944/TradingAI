"""Offline diagnostics must not disclose raw logs or query a live endpoint."""
import unittest

from scripts.otc_feed_check import summarize


class LocalStreamSummaryTests(unittest.TestCase):
    def test_counts_real_reconnects_not_informational_backfill_logs(self):
        lines = [
            "2026-11-19 09:50:00,001 INFO services.otc_session OTC health: OTC: поток активен | Тариф: Lite; осталось запросов: 6000 из 7000 за day; reasons={}",
            "2026-11-19 09:51:00,001 INFO services.otc_session OTC reconnect: no history backfill needed or permitted; gaps remain invalid",
            "2026-11-19 09:52:00,001 INFO services.otc_session OTC pair rotated: A -> B (catalogue_ineligible); active=B",
            "2026-11-19 09:53:00,001 WARNING services.otc_session OTC reconnect: stream_no_fresh_ticks (TimeoutError)",
            "2026-11-19 09:54:00,001 INFO services.otc_session OTC fresh stream confirmed",
        ]
        summary = summarize(lines, 30)
        self.assertIn("Реальные обрывы потока: 1", summary)
        self.assertIn("Плановые замены пар: 1", summary)
        self.assertIn("Подтверждения свежего потока: 1", summary)
        self.assertIn("осталось 6000 из 7000", summary)
        self.assertNotIn("A -> B", summary)

    def test_no_timestamp_is_explained(self):
        self.assertIn("Нет строк журнала", summarize(["just an old log"], 30))


if __name__ == "__main__":
    unittest.main()
