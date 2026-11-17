import unittest
from unittest.mock import patch

from services.otc_market import Asset
from services.otc_ml.preview import signal_card
from services.otc_ready import VERSION, select_ready
from tests.test_otc_confirmations import history


class OneConfirmationTests(unittest.TestCase):
    def test_m2_uses_closed_m1_features_and_two_minute_expiry(self):
        bars = history()
        now = bars[-1].time + 100
        asset = Asset("A", True, True, 90, None, "Asset", now)
        row = select_ready({"A": bars}, {"A": asset}, {"A": now}, now, 120)
        self.assertIsNotNone(row)
        self.assertEqual(row["expires_at"] - row["entry_at"], 120)
        self.assertEqual(row["confirmations"]["period"], 60)

    def test_m5_m15_do_not_analyze_between_boundaries(self):
        with patch("services.otc_ready.build") as build:
            for seconds in (300, 900):
                self.assertIsNone(select_ready({"A": []}, {}, {}, 1240, seconds))
            build.assert_not_called()

    def test_loss_cooldown_is_audited_before_selection(self):
        audit = []
        self.assertIsNone(
            select_ready(
                {"A": []}, {}, {}, 1780, 60, audit=audit, excluded_symbols={"A": 1900}
            )
        )
        self.assertEqual(audit[0]["reason"], "symbol_loss_cooldown")

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
            ):
                return select_ready(*args)
        return select_ready(*args)

    def reports(self, count):
        checks = dict.fromkeys(("ema", "macd", "candle", "rsi", "alignment"), "neutral")
        if count:
            checks["ema"] = "agree"
        up = dict(count=count, available=5, total=5, checks=checks)
        down = dict(
            up, count=0, checks=dict(checks, ema="against" if count else "neutral")
        )
        return up, down

    def test_one_vote_and_correlated_votes_no_longer_pass(self):
        self.assertIsNone(self.select(self.reports(1)))
        up, down = self.reports(2)
        up["checks"]["macd"] = "agree"
        self.assertIsNone(self.select((up, down)))

    def test_two_groups_pass_and_are_displayed(self):
        up, down = self.reports(2)
        up["checks"]["candle"] = "agree"
        selected = self.select((up, down))
        self.assertEqual(selected["model_sha256"], VERSION + "-strict")
        self.assertEqual(selected["direction"], "UP")
        self.assertIn("Индикаторы: 2/5", signal_card(selected, source="Основной"))

    def test_zero_votes_and_stale_quotes_do_not_pass(self):
        self.assertIsNone(self.select(self.reports(0)))
        self.assertIsNone(self.select(stale=True))

    def test_real_closed_indicators_supply_count(self):
        selected = self.select()
        self.assertIsNotNone(selected)
        count = selected["confirmations"]["count"]
        self.assertGreaterEqual(count, 1)
        self.assertIn(
            f"Индикаторы: {count}/5", signal_card(selected, source="Основной")
        )
