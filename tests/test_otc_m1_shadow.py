"""Independent M1 flat/correction research must be causal and shadow-only."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_candidate_research import CandidateJournal
from services.otc_community_cycles import ShadowCycles
from services.otc_m1_shadow import (
    METHODS,
    VERSION,
    prepare,
    report,
    signals,
    text_report,
)
from services.otc_strategy import Candle

ASOF = 12000
ENTRY = ASOF + 60
NOW = ENTRY - 18


def bar(time, opened, closed, spread=0.2):
    return Candle(
        time,
        opened,
        max(opened, closed) + spread,
        min(opened, closed) - spread,
        closed,
    )


def flat_history(setup="support"):
    cycle = [98, 99, 100, 101, 102, 101, 100, 99, 98, 99]
    values = (cycle * 8)[:79]
    candles = [bar(ASOF - (80 - i) * 60, price, price) for i, price in enumerate(values)]
    if setup == "support":
        end = bar(ASOF - 60, 97.8, 98.4)
    elif setup == "resistance":
        end = bar(ASOF - 60, 102.2, 101.6)
    elif setup == "breakout":
        end = bar(ASOF - 60, 101.8, 103.6)
    elif setup == "false_break":
        end = Candle(ASOF - 60, 101.8, 104, 101.7, 101.9)
    else:
        raise ValueError(setup)
    return candles + [end]


def correction_history(direction="UP"):
    increasing = [90 + 0.2 * i for i in range(74)]
    pullback = [104.0, 103.4, 102.8, 102.2, 101.6]
    values = increasing + pullback
    if direction == "DOWN":
        values = [200 - v for v in values]
    candles = [
        bar(ASOF - (80 - i) * 60, value, value, 0.12)
        for i, value in enumerate(values)
    ]
    end = (
        bar(ASOF - 60, 101.6, 102.8, 0.12)
        if direction == "UP"
        else bar(ASOF - 60, 98.4, 97.2, 0.12)
    )
    return candles + [end]


def snapshot(bars, now=NOW, *, payout=92, open=True):
    asset = SimpleNamespace(
        name="Pair A", payout=payout, open=open, observed_at=now,
    )
    return ({"A": bars}, {"A": asset}, {"A": now}, None)


class M1ShadowTests(unittest.TestCase):
    def test_both_variants_are_independent_even_at_same_entry(self):
        decisions = {
            "flat": {"direction": "UP", "setup": "support_rebound"},
            "correction": {"direction": "DOWN", "setup": "bearish_pullback"},
        }
        with patch("services.otc_m1_shadow.signals", return_value=decisions):
            rows = prepare(snapshot(flat_history()), NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["direction"] for r in rows}, {"UP", "DOWN"})
        self.assertEqual(len({r["version"] for r in rows}), 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "study.db"
            journal = CandidateJournal(path)
            journal.record(rows)
            self.assertEqual(report(path, 0, NOW)["flat"]["PENDING"], 1)
            self.assertEqual(report(path, 0, NOW)["correction"]["PENDING"], 1)

    def test_support_resistance_and_breakout_have_distinct_rules(self):
        for setup, direction, label in (
            ("support", "UP", "support_rebound"),
            ("resistance", "DOWN", "resistance_rebound"),
            ("breakout", "UP", "breakout_up"),
        ):
            with self.subTest(setup=setup):
                decision = signals(flat_history(setup), ASOF).get("flat")
                self.assertIsNotNone(decision)
                self.assertEqual((decision["direction"], decision["setup"]),
                                 (direction, label))
        self.assertNotIn("flat", signals(flat_history("false_break"), ASOF))

    def test_correction_requires_preexisting_trend_and_confirmed_rsi_turn(self):
        for direction in ("UP", "DOWN"):
            with self.subTest(direction=direction):
                decision = signals(correction_history(direction), ASOF).get("correction")
                self.assertIsNotNone(decision)
                self.assertEqual(decision["direction"], direction)
                self.assertIn("rsi14", decision)

    def test_only_closed_continuous_valid_candles_are_used(self):
        bars = flat_history("breakout")
        expected = signals(bars, ASOF)
        self.assertTrue(expected)
        self.assertEqual(
            signals(bars + [bar(ASOF, 100, 1000)], ASOF), expected
        )
        self.assertEqual(signals(bars[:-1], ASOF), {})
        gap = list(bars)
        gap[40] = bar(gap[40].time + 60, 98, 98)
        self.assertEqual(signals(gap, ASOF), {})
        invalid = list(bars)
        invalid[10] = Candle(invalid[10].time, 1, 0.5, 1, 1)
        self.assertEqual(signals(invalid, ASOF), {})

    def test_rejects_late_missing_and_closed_market_metadata(self):
        candles = flat_history("breakout")
        self.assertEqual(prepare(snapshot(candles, NOW + 7), NOW + 7), [])
        self.assertEqual(prepare(snapshot(candles, payout=85), NOW), [])
        self.assertEqual(prepare(snapshot(candles, open=False), NOW), [])
        stale = snapshot(candles)
        stale[2]["A"] = NOW - 9
        self.assertEqual(prepare(stale, NOW), [])

    def test_prospective_journal_keeps_m1_results_and_unknown_separate(self):
        decision = prepare(snapshot(flat_history("breakout")), NOW)
        self.assertEqual(len(decision), 1)
        self.assertFalse(decision[0]["selected"])
        self.assertTrue(decision[0]["experiment"]["shadow_only"])
        self.assertEqual(decision[0]["expires_at"] - decision[0]["entry_at"], 60)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "community-cycles.db"
            journal = CandidateJournal(path)
            journal.record(decision)
            journal.record(decision)
            self.assertEqual(report(path, 0, ENTRY)["flat"]["PENDING"], 1)
            journal.samples([
                ("A", ENTRY, ENTRY, ENTRY, 103.6),
                ("A", ENTRY + 60, ENTRY + 60, ENTRY + 60, 104.0),
            ])
            counts = report(path, 0, ENTRY + 63)
            self.assertEqual(counts["flat"]["WIN"], 1)
            self.assertEqual(counts["correction"], {})
            self.assertIn("ФЛЭТ M1", text_report(counts))
            self.assertIn("КОРРЕКЦИЯ M1", text_report(counts))
            self.assertIn("WIN 1", text_report(counts))
            self.assertEqual(set(counts), set(METHODS))
            self.assertEqual(decision[0]["experiment"]["version"], VERSION)
            # Without an observed expiry tick a result must remain UNKNOWN.
            journal.record([
                dict(decision[0], version="m1-correction-shadow-v1",
                     experiment=dict(version=VERSION, method="correction",
                                     shadow_only=True),
                     entry_at=ENTRY + 120, expires_at=ENTRY + 180,
                     observed_at=ENTRY + 100)
            ])
            self.assertEqual(report(path, 0, ENTRY + 183)["correction"]["UNKNOWN"], 1)


class ShadowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_candidate_engine_does_not_restart_retired_m1_shadow(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "community-cycles.db"
            engine = ShadowCycles(path, clock=lambda: NOW)
            await engine.plan(snapshot(flat_history("breakout")))
            counts = report(path, 0, NOW)
            self.assertFalse(counts["flat"])
            self.assertFalse(counts["correction"])
            # No broker client, Telegram bot, or extra streaming session exists
            # in this engine: only the existing sampler sees the shared ticks.
            from services.otc_market import Tick

            engine.observe(Tick("A", ENTRY, 103.6), ENTRY)
            engine.observe(Tick("A", ENTRY + 60, 104.0), ENTRY + 60)
            # The engine's clock needs to move before sampler.flush().
            with patch.object(engine, "clock", return_value=ENTRY + 63):
                await engine.flush()
            self.assertFalse(report(path, 0, ENTRY + 63)["flat"])
