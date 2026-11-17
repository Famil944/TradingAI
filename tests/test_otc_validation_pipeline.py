import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import CandidateJournal
from services.otc_community_patterns import route
from services.otc_ml.preview import select_forecast
from services.otc_readiness import DataReadiness, downtime
from services.otc_strategy import Candle
from services.otc_validation_report import replay, report


class ValidationPipelineTests(unittest.TestCase):
    def test_disconnect_invalidates_copied_preview_quotes_immediately(self):
        from unittest.mock import Mock

        from services.otc_session import OTCSession

        session = SimpleNamespace(
            _quotes={"A": 100},
            clock=lambda: 101,
            data_readiness=Mock(),
        )
        OTCSession._clear_quotes(session)
        self.assertEqual(session._quotes, {})
        session.data_readiness.reset.assert_called_once()

    def test_recovery_requires_stability_history_and_records_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "readiness.db"
            gate = DataReadiness(path, 7180)
            bars = [Candle(i * 60, 100, 101, 99, 100.5) for i in range(59, 119)]
            assets = {"A": SimpleNamespace(open=True, payout=90, observed_at=7180)}
            for now in (7180, 7185):
                self.assertEqual(
                    gate.observe(
                        SimpleNamespace(symbol="A"),
                        now,
                        {"A": bars},
                        assets,
                        {"A": now},
                    ),
                    {},
                )
            self.assertEqual(
                gate.observe(
                    SimpleNamespace(symbol="A"), 7190, {"A": bars}, assets, {"A": 7190}
                ),
                {"A": 7190},
            )
            self.assertEqual(downtime(path, 0, 7200)["seconds"], 10)
            gate.reset(7191, "network")
            self.assertEqual(
                gate.observe(
                    SimpleNamespace(symbol="A"), 7192, {"A": bars}, assets, {"A": 7192}
                ),
                {},
            )
            resumed = DataReadiness(path, 7195)
            self.assertEqual(downtime(path, 0, 7200)["intervals"], 2)
            for now in (7195, 7200, 7205):
                self.assertEqual(
                    resumed.observe(
                        SimpleNamespace(symbol="A"),
                        now,
                        {"A": bars[:-1]},
                        assets,
                        {"A": now},
                    ),
                    {},
                )

    def test_router_is_fixed_and_does_not_fallback(self):
        decisions = {
            "ema_pullback": {"direction": "UP"},
            "break_retest": {"direction": "DOWN"},
            "bb_rsi_reentry": {"direction": "DOWN"},
        }
        self.assertEqual(route(decisions, "uptrend")["routed_method"], "ema_pullback")
        self.assertIsNone(route(decisions, "downtrend"))
        self.assertEqual(route(decisions, "range")["routed_method"], "bb_rsi_reentry")
        self.assertEqual(
            route(decisions, "transition")["routed_method"], "break_retest"
        )
        self.assertIsNone(route(decisions, "unknown"))
        self.assertIsNone(route({"break_retest": {"direction": "UP"}}, "range"))

    def test_rejected_boost_forecast_is_preserved_and_not_selected(self):
        runtime = SimpleNamespace(
            inspect=lambda *args: dict(
                probabilities=[0.48, 0, 0.52],
                model_sha256="model",
                calibration_sha256="cal",
            )
        )
        asset = SimpleNamespace(open=True, payout=90, observed_at=1240, name="A")
        audit = []
        with patch(
            "services.otc_ml.preview.build", return_value=dict(count=1, available=5)
        ):
            self.assertIsNone(
                select_forecast(
                    runtime, {"A": []}, {"A": asset}, {"A": 1240}, 1240, audit=audit
                )
            )
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["reason"], "model_ev_below_margin")
        self.assertFalse(audit[0]["selected"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            journal = CandidateJournal(root / "xgboost-candidates.db")
            journal.record(audit)
            journal.samples(
                [("A", 1260, 1260, 1260, 100), ("A", 1380, 1380, 1380, 101)]
            )
            data = report(root, 0, 1400)
            self.assertEqual(data["xgboost"][0]["outcomes"], {"WIN": 1})
            self.assertEqual(data["calibration"][0]["n"], 1)
            self.assertEqual(data["calibration"][0]["observed"], 1)
            self.assertEqual(report(root, 0, 1300)["calibration"], [])

    def test_replay_does_not_apply_loss_before_resolution(self):
        def row(at, seconds, symbol="A", score=1):
            return dict(
                symbol=symbol,
                entry_at=at,
                expires_at=at + seconds,
                observed_at=at - 20,
                direction="UP",
                payout=90,
                quality_score=score,
            )

        first = row(1000, 120)
        # LOSS at 1120 is only considered known at 1180.
        before = row(1160, 300)
        after = row(1220, 900)
        prices = {("A", 1000): 100, ("A", 1120): 99, ("A", 1160): 99, ("A", 1460): 100}
        chosen, blocked = replay([first, before, after], prices, 2500)
        self.assertEqual([r["entry_at"] for r in chosen], [1000, 1160])
        self.assertEqual(blocked["symbol_loss_pause"], 1)
        chosen, blocked = replay(
            [row(1000, 120), row(1000, 300), row(1060, 120, "B")], prices, 2500
        )
        self.assertEqual(len(chosen), 1)
        self.assertEqual(blocked["duplicate_pair_entry"], 1)
        self.assertEqual(blocked["pending_or_result_pause"], 1)
