import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.otc_candidate_report import GATES, paired_day_comparison, report
from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_market import Asset, Tick
from services.otc_market_archive import MarketArchive
from services.otc_ml.preview import PreviewJournal
from services.otc_ready import ReadyPrimary, select_ready_v5 as select_ready
from services.otc_strategy import Candle

ENTRY = 1800000060


def candidate(symbol="A", seconds=60, **changes):
    return dict(
        dict(
            version="primary-ready-v5",
            symbol=symbol,
            entry_at=ENTRY,
            expires_at=ENTRY + seconds,
            observed_at=ENTRY - 20,
            feature_asof=ENTRY - 60,
            direction="UP",
            payout=90,
            reason="eligible",
            gates=dict.fromkeys(GATES, True),
            quality_score=0.3,
            regime="strong_trend",
            selected=True,
        ),
        **changes,
    )


def setup():
    return SimpleNamespace(
        direction="UP",
        evidence=dict(
            features=dict(
                atr14=0.01,
                adx14=0.3,
                ema20_gap=0.005,
                ema_spread=0.003,
                macd_hist=0.001,
                rsi14=0.6,
                bb_position=0.7,
            ),
            last_closed_candle=dict(close=1.0),
        ),
    )


class SelectionTests(unittest.TestCase):
    def test_rejections_and_micro_features_are_prospective(self):
        now = ENTRY - 20
        assets = {s: Asset(s, True, True, 90, None, s, now) for s in ("A", "B", "C")}
        weak = setup()
        weak.evidence["features"]["adx14"] = 0.1
        audit = []
        with patch("services.otc_ready.context_setup", side_effect=[setup(), weak]):
            selected = select_ready(
                dict(A=[], B=[], C=[]),
                assets,
                dict(A=now, B=now, C=now - 9),
                now,
                60,
                audit=audit,
                recent_ticks={"A": (0.995, now - 1, now), "B": (2, now + 1, now + 1)},
            )
        self.assertEqual(selected["symbol"], "A")
        self.assertEqual(
            [r["reason"] for r in audit],
            ["eligible", "filter_rejected", "asset_or_quote_ineligible"],
        )
        self.assertAlmostEqual(audit[0]["micro_move_atr"], -0.5)
        self.assertNotIn("micro_move_atr", audit[1])
        self.assertFalse(audit[1]["gates"]["adx"])
        self.assertFalse(audit[2]["selected"])


class SamplerTests(unittest.TestCase):
    def test_first_tick_and_receipt_time_are_both_required(self):
        sampler = TickSampler()
        sampler.register([candidate()], ENTRY - 20)
        sampler.observe(Tick("A", ENTRY - 1, 99), ENTRY)
        sampler.observe(Tick("A", ENTRY, 1), ENTRY + 0.5)
        sampler.observe(Tick("A", ENTRY + 0.5, 9), ENTRY + 0.6)
        sampler.observe(Tick("A", ENTRY + 1, 2), ENTRY + 4)  # arrived too late for +1
        sampler.observe(Tick("A", ENTRY + 5, float("nan")), ENTRY + 5)
        sampler.observe(Tick("A", ENTRY + 5, 3), ENTRY + 5)
        samples = sampler.drain(ENTRY + 7)
        prices = {r[1]: r[4] for r in samples}
        self.assertEqual(prices[ENTRY], 1)
        self.assertNotIn(ENTRY + 1, prices)
        self.assertEqual(prices[ENTRY + 5], 3)
        self.assertNotIn(ENTRY - 1, prices)
        sampler.restore(samples)
        self.assertEqual(sampler.drain(ENTRY + 7), samples)
        sampler.drain(ENTRY + 1000)
        self.assertEqual(sampler.targets, {})

    def test_restart_does_not_backfill_or_register_past_targets(self):
        sampler = TickSampler()
        sampler.register([candidate()], ENTRY + 10)
        sampler.observe(Tick("A", ENTRY, 1), ENTRY + 10)
        self.assertEqual(sampler.drain(ENTRY + 10), [])
        self.assertEqual(sampler.targets["A"], {ENTRY + 60})


class ResearchReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "candidates.db"
        self.archive_path = Path(self.temp.name) / "history.db"
        self.journal = CandidateJournal(self.path)
        self.archive = MarketArchive(self.archive_path)

    def bars(self, symbol="A", revisions=False):
        bars = [Candle(ENTRY, 1, 1, 1, 1), Candle(ENTRY + 60, 2, 2, 2, 2)]
        self.archive.bars(symbol, bars, ENTRY + 120)
        if revisions:
            self.archive.bars(symbol, [Candle(ENTRY, 1.1, 1.1, 1.1, 1.1)], ENTRY + 121)

    def test_missing_journal_is_read_only(self):
        missing = self.path.with_name("missing.db")
        data = report(missing)
        self.assertEqual(data["status"], "awaiting_prospective_data")
        self.assertFalse(missing.exists())

    def test_idempotence_nonfinite_and_missing_evidence(self):
        self.journal.record([candidate(payout=float("nan"))])
        self.journal.record([candidate(payout=95)])
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT payload FROM candidates").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(json.loads(rows[0][0])["payout"])
        data = report(self.path, self.archive_path, now=ENTRY + 200)
        stats = data["groups"][0]["policies"]["full"]["historical_open"]
        self.assertEqual(stats["outcomes"], {"UNKNOWN": 1})
        self.assertIsNone(stats["mean_net_units"])

    def test_delay_results_use_same_cohort_and_fixed_expiry(self):
        self.journal.record([candidate(), candidate("B")])
        self.bars()
        self.journal.samples(
            [
                ("A", ENTRY + delay, ENTRY + delay, ENTRY + delay, price)
                for delay, price in [(0, 1), (1, 1), (3, 1), (5, 3), (60, 2)]
            ]
        )
        data = report(self.path, self.archive_path, now=ENTRY + 200)
        group = data["groups"][0]
        self.assertEqual(group["paired_latency_candidates"], 1)
        self.assertEqual(group["delays"]["0"]["paired"]["win_rate"], 1)
        self.assertEqual(group["delays"]["5"]["paired"]["win_rate"], 0)
        self.assertEqual(group["delays"]["0"]["all"]["outcomes"]["UNKNOWN"], 1)
        self.assertEqual(
            group["policies"]["full"]["historical_open"]["mean_net_units"], 0.9
        )
        self.assertEqual(group["policies"]["full_micro"]["historical_open"]["n"], 0)

    def test_ablation_ranking_horizons_and_revision_exclusion(self):
        rejected = candidate(
            "B",
            gates=dict.fromkeys(GATES, True) | {"adx": False},
            reason="filter_rejected",
            quality_score=0.9,
        )
        self.journal.record([candidate(), rejected, candidate(seconds=300)])
        self.bars()
        self.bars("B", revisions=True)
        data = report(self.path, self.archive_path, now=ENTRY + 500)
        first, second = data["groups"]
        self.assertEqual(first["seconds"], 60)
        self.assertEqual(second["seconds"], 300)
        self.assertEqual(first["policies"]["full"]["historical_open"]["n"], 1)
        self.assertEqual(first["policies"]["without_adx"]["historical_open"]["n"], 2)
        self.assertEqual(
            first["policies"]["context"]["top_per_minute"]["outcomes"], {"UNKNOWN": 1}
        )

    def test_report_cutoff_does_not_see_future_samples_or_canonical_confirmation(self):
        self.journal.record([candidate()])
        self.bars()
        self.journal.samples(
            [
                ("A", ENTRY, ENTRY, ENTRY, 1),
                ("A", ENTRY + 60, ENTRY + 60, ENTRY + 61, 2),
            ]
        )
        group = report(self.path, self.archive_path, now=ENTRY + 60)["groups"][0]
        self.assertEqual(group["delays"]["0"]["all"]["outcomes"], {"PENDING": 1})
        self.assertEqual(
            group["policies"]["full"]["historical_open"]["outcomes"], {"PENDING": 1}
        )

    def test_canonical_confirmation_even_when_prices_identical_and_no_downgrade(self):
        bar = Candle(ENTRY, 1, 1, 1, 1)
        self.archive.bars("A", [bar], ENTRY + 60, "otcharts_ticks")
        self.archive.bars("A", [bar], ENTRY + 61, "otcharts_history")
        self.archive.bars(
            "A", [Candle(ENTRY, 99, 99, 99, 99)], ENTRY + 62, "otcharts_ticks"
        )
        with closing(sqlite3.connect(self.archive_path)) as db:
            row = db.execute("SELECT open,source,revisions FROM bars").fetchone()
        self.assertEqual(row, (1, "otcharts_history", 0))

    def test_block_comparison_needs_multiple_days(self):
        row = candidate(gates=dict.fromkeys(GATES, True) | {"adx": False})
        small = paired_day_comparison([row], ["WIN"], "full", "context")
        self.assertIsNone(small["day_bootstrap_95"])
        rows = [dict(row, entry_at=ENTRY + day * 86400) for day in range(7)]
        stats = paired_day_comparison(rows, ["WIN"] * 7, "full", "context")
        self.assertAlmostEqual(stats["delta_per_opportunity"], 0.9)
        self.assertIsNotNone(stats["day_bootstrap_95"])
        self.assertFalse(stats["automatic_promotion"])


class ReadySchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_waits_for_actual_ready_window_and_publishes(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "ready.db", per_horizon=True)
            clock = [ENTRY - 22]
            ready = ReadyPrimary(journal, clock=lambda: clock[0])
            ready.allowed_timeframes = (60,)
            ready.publish = AsyncMock(return_value=True)
            asset = Asset("A", True, True, 90, None, "A", ENTRY - 20)
            ready.observe({"A": []}, {"A": asset}, {"A": ENTRY - 20}, None)
            with (
                patch("services.otc_ready.context_setup", return_value=setup()),
                patch(
                    "services.otc_ready.select_ready",
                    side_effect=lambda *args: select_ready(*args[:8]),
                ),
            ):
                task = asyncio.create_task(ready.run(None, 1))
                try:
                    await asyncio.sleep(0.05)
                    self.assertIsNone(ready.attempted)
                    clock[0] = ENTRY - 19
                    for _ in range(30):
                        if ready.publish.await_count:
                            break
                        await asyncio.sleep(0.05)
                    ready.publish.assert_awaited_once()
                    self.assertEqual(ready.sent_count, 1)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            data = report(ready.research.path, now=ENTRY + 200)
            self.assertEqual(data["candidates"], 1)

    async def test_shadow_collection_does_not_bypass_pending_position(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "ready.db", per_horizon=True)
            journal.reserve(dict(candidate(), model_sha256="v5"), ENTRY - 40)
            ready = ReadyPrimary(journal, clock=lambda: ENTRY - 20)
            ready.allowed_timeframes = (60,)
            ready.publish = AsyncMock()
            asset = Asset("A", True, True, 90, None, "A", ENTRY - 20)
            snapshot = ({"A": []}, {"A": asset}, {"A": ENTRY - 20}, None)
            ready.observe(*snapshot)
            with (
                patch("services.otc_ready.context_setup", return_value=setup()),
                patch(
                    "services.otc_ready.select_ready",
                    side_effect=lambda *args: select_ready(*args[:8]),
                ),
            ):
                await ready.plan(None, 1, snapshot)
            ready.publish.assert_not_awaited()
            data = report(ready.research.path, now=ENTRY + 200)
            self.assertEqual(data["operational_blocks"], {"pending_position": 1})


if __name__ == "__main__":
    unittest.main()
