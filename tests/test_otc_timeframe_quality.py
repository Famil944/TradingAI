import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from database.db import Database
from services.otc_conditional import Setup, context_setup, make_plan, message
from services.otc_evaluation import assess
from services.otc_market import Asset
from services.otc_quality_report import build_report
from services.otc_session import OTCSession
from services.otc_strategy import Candle
from tests.test_otc_conditional import impulse_history


class TimeframeQualityTests(unittest.TestCase):
    def expanded(self, seconds):
        bars = []
        for i, bar in enumerate(impulse_history()):
            start = 1800000000 + i * seconds
            count = seconds // 60
            for k in range(count):
                opening = bar.open + (bar.close - bar.open) * k / count
                close = bar.open + (bar.close - bar.open) * (k + 1) / count
                bars.append(
                    Candle(
                        start + k * 60,
                        opening,
                        max(bar.high, opening, close),
                        min(bar.low, opening, close),
                        close,
                    )
                )
        return bars

    def test_higher_frame_features_are_causal_and_gaps_rejected(self):
        for seconds in (300, 900):
            bars = self.expanded(seconds)
            at = bars[-1].time + 60
            setup = context_setup(bars, at, seconds)
            self.assertIsNotNone(setup)
            self.assertEqual(setup.evidence["analysis_timeframe"], seconds)
            self.assertEqual(setup.evidence["last_closed_candle"]["time"] + seconds, at)
            self.assertIn("timeframe-v3", setup.rule)
            future = [Candle(at + i * 60, 1, 100000, 1, 10000) for i in range(30)]
            self.assertEqual(setup, context_setup(bars + future, at, seconds))
            self.assertIsNone(context_setup(bars[:-20] + bars[-19:], at, seconds))
            self.assertIsNone(context_setup(bars[-200:], at, seconds))

    def test_schedule_uses_real_higher_frame_boundary_and_full_condition(self):
        at = 1800000060
        for seconds in (60, 300, 900):
            plan = make_plan(
                "A", at, Setup("UP", "red", 0.5, "fixture"), at, seconds=seconds
            )
            self.assertEqual(plan.entry_at % seconds, 0)
            self.assertGreaterEqual(plan.entry_at - at, 180)
            self.assertEqual(plan.entry_at - plan.condition_from, seconds)
            self.assertEqual(plan.expires_at - plan.entry_at, seconds)
            self.assertIn(f"свеча M{seconds // 60}", message(plan, "fixture"))
            self.assertIsNone(
                make_plan("A", at, plan.setup, at, plan.expires_at - 1, seconds=seconds)
            )

    def test_assessment_uses_whole_condition_not_last_minute(self):
        at = 1800000060
        for seconds in (300, 900):
            plan = make_plan(
                "A", at, Setup("UP", "red", 0.5, "fixture"), at, seconds=seconds
            )
            bars = [
                Candle(t, 2, 2.1, 0.8, 0.9)
                for t in range(plan.condition_from, plan.entry_at, 60)
            ]
            # Last M1 is green, but the full M5/M15 is red: its open is 2, close is 1.
            bars[-1] = Candle(bars[-1].time, 0.9, 1.1, 0.8, 1)
            bars += [
                Candle(plan.entry_at, 1, 1.1, 0.9, 1),
                Candle(plan.expires_at, 1.2, 1.3, 1.1, 1.2),
            ]
            row = {
                **asdict(plan),
                "direction": "UP",
                "colour": "red",
                "invalidation": 0.5,
                "reason": "fixture",
                "lifecycle": "ACTIVE",
                "delivery": "SENT",
            }
            self.assertEqual(assess(row, bars, plan.expires_at + 65)["outcome"], "WIN")
            self.assertEqual(
                assess(row, bars[1:], plan.expires_at + 65)["outcome"], "UNKNOWN"
            )
            bars[0] = Candle(bars[0].time, 0.8, 2.1, 0.7, 0.9)
            self.assertEqual(
                assess(row, bars, plan.expires_at + 65)["outcome"], "SKIPPED"
            )

    def test_paid_metadata_gate_rejects_unknown_low_closed_or_old(self):
        session = OTCSession(
            None,
            None,
            SimpleNamespace(
                test_mode=True, require_paid_metadata=True, otcharts_api_key=""
            ),
        )
        good = Asset("GOOD", True, True, 92, None, "Good", 100)
        bad = [
            Asset("LOW", True, True, 70, None, observed_at=100),
            Asset("CLOSED", True, False, 92, None, observed_at=100),
            Asset("UNKNOWN", True, None, None, None),
            Asset("OLD", True, True, 92, None, observed_at=0),
        ]
        self.assertEqual(session._eligible([good] + bad, 100, 60), [good])
        self.assertEqual(session._eligible([good], 161, 60), [])

    def test_monitor_does_not_claim_edge_or_mix_instruments(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "bot.db")
            db.init_db()
            at = 1800000000
            for i, symbol in enumerate(("A", "B")):
                plan = make_plan(
                    symbol,
                    at + i * 300,
                    Setup("UP", "red", 0.5, "fixture"),
                    at + i * 300,
                )
                db.prepare_plan(plan, at + i * 300)
            report = build_report(db, at + 1000)
            self.assertEqual(len(report["groups"]), 2)
            self.assertFalse(report["ml_live_enabled"])
            self.assertTrue(all(not g["advantage_confirmed"] for g in report["groups"]))
