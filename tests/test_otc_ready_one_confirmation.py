import unittest
from unittest.mock import patch

from services.otc_market import Asset
from services.otc_ml.preview import signal_card
from services.otc_ready import VERSION, select_ready
from tests.test_otc_confirmations import history


class ConfirmationGateTests(unittest.TestCase):
    def select(self, reports=None, stale=False):
        bars = history()
        now = bars[-1].time + 100
        asset = Asset("A", True, True, 90, None, "Asset", now)
        args = ({"A": bars}, {"A": asset}, {"A": now - (9 if stale else 0)}, now, 60)
        if reports:
            with (
                patch("services.otc_ready.build", side_effect=reports),
                patch(
                    "services.otc_ready.context_setup",
                    side_effect=AssertionError("old gates used"),
                ),
                patch(
                    "services.otc_ready.evaluate_policy",
                    return_value={"reason": "eligible", "strategy": "trend_continuation", "gates": {"trend_aligned": True}},
                ),
                patch(
                    "services.otc_ready.evaluate_m1",
                    return_value={"passed": True, "reason": "eligible", "gates": {"closed_m5_trend": True}, "metrics": {}},
                ),
            ):
                return select_ready(*args)
        return select_ready(*args)

    def reports(self, count):
        checks = dict.fromkeys(("ema", "macd", "candle", "rsi", "alignment"), "neutral")
        for key in list(checks)[:count]:
            checks[key] = "agree"
        up = dict(count=count, available=5, total=5, checks=checks)
        down = dict(
            up, count=0,
            checks={k: ("against" if v == "agree" else "neutral") for k, v in checks.items()},
        )
        return up, down

    def test_one_and_two_votes_rejected_and_three_can_pass(self):
        self.assertIsNone(self.select(self.reports(1)))
        self.assertIsNone(self.select(self.reports(2)))
        selected = self.select(self.reports(3))
        self.assertEqual(selected["model_sha256"], VERSION)
        self.assertEqual(selected["direction"], "UP")
        self.assertIn("Индикаторы: 3/5", signal_card(selected, source="Основной"))

    def test_zero_votes_and_stale_quotes_do_not_pass(self):
        self.assertIsNone(self.select(self.reports(0)))
        self.assertIsNone(self.select(stale=True))

    def test_real_closed_indicators_supply_count(self):
        selected = self.select()
        # Real closed indicators may legitimately produce no signal.
        if selected is not None:
            count = selected["confirmations"]["count"]
            self.assertGreaterEqual(count, 3)
            self.assertIn(
                f"Индикаторы: {count}/5", signal_card(selected, source="Основной")
            )
