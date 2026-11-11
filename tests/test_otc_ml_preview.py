import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.otc_market import Asset
from services.otc_ml.preview import PreviewJournal, select_forecast, settle_preview
from services.otc_ml.provider_data import build_provider_dataset
from services.otc_strategy import Candle
from tests.test_otc_conditional import impulse_history


class FakeRuntime:
    def inspect(self, row, bars, payout):
        seconds = row["expires_at"] - row["entry_at"]
        p = 0.75 if seconds == 180 else 0.62
        return dict(probabilities=[1 - p, 0, p], model_sha256=str(seconds))


class PreviewTests(unittest.TestCase):
    def test_duration_is_selected_by_forecast_not_fixed_or_random(self):
        now = 1800000042
        asset = Asset("A", True, True, 92, None, "Asset", now)
        selected = select_forecast(
            FakeRuntime(), {"A": []}, {"A": asset}, {"A": now}, now
        )
        self.assertEqual(selected["expires_at"] - selected["entry_at"], 180)
        self.assertEqual(selected["direction"], "UP")
        self.assertEqual(selected["feature_asof"], now)
        self.assertEqual(selected["decision_lead_seconds"], 20)
        self.assertIsNone(
            select_forecast(FakeRuntime(), {"A": []}, {"A": asset}, {"A": now - 9}, now)
        )
        self.assertIsNone(
            select_forecast(
                FakeRuntime(), {"A": []}, {"A": asset}, {"A": now}, now + 10
            )
        )

    def test_no_model_or_weak_forecast_does_not_generate_signal(self):
        class Weak:
            def inspect(self, *args):
                return dict(probabilities=[0.49, 0.02, 0.49], model_sha256="weak")

        now = 1800000042
        asset = Asset("A", True, True, 92, None, "Asset", now)
        self.assertIsNone(
            select_forecast(Weak(), {"A": []}, {"A": asset}, {"A": now}, now)
        )

    def test_preview_training_excludes_forming_candle_for_twenty_second_lead(self):
        bars = impulse_history()
        for i in range(1, 20):
            bars.append(Candle(bars[900].time + i * 60, 102, 103, 101, 102))
        rows = [
            dict(asdict(b), symbol="A", source="otcharts", venue="otc", is_otc=True)
            for b in bars
        ]
        data, _ = build_provider_dataset(rows, preview=True)
        self.assertEqual(set(data), {60, 120, 180, 300, 900})
        altered = [dict(r) for r in rows]
        altered[900].update(open=200, high=210, low=190, close=200)
        changed, _ = build_provider_dataset(altered, preview=True)
        self.assertTrue(data[120])
        self.assertEqual(data[120][0].features, changed[120][0].features)

    def test_separate_journal_and_exact_expiry_avoid_fabricated_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "preview.db")
            selected = dict(
                symbol="A",
                entry_at=1800000060,
                expires_at=1800000240,
                direction="UP",
                payout=92,
                model_sha256="fixture",
            )
            identifier = journal.reserve(selected, 1800000042)
            self.assertIsNotNone(identifier)
            self.assertIsNone(
                journal.reserve({**selected, "entry_at": 1800000120}, 1800000043)
            )
            row = journal.pending()[0]
            self.assertEqual(
                settle_preview(row, [], row["entry_at"])[0], "NOT_DELIVERED"
            )
            journal.sent(identifier, 1800000043)
            row = journal.pending()[0]
            bars = [
                Candle(row["entry_at"], 1, 1.1, 0.9, 1),
                Candle(row["expires_at"], 1.2, 1.3, 1.1, 1.2),
            ]
            self.assertIsNone(settle_preview(row, bars, row["expires_at"]))
            result = settle_preview(row, bars, row["expires_at"] + 60)
            self.assertEqual(result[0], "WIN")
            journal.finish(identifier, *result)
            self.assertIn("Вин 1", journal.stats(180))
            self.assertIn("Вин 0", journal.stats(120))
            self.assertIn("100.0%", journal.stats())


class PreviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_timely_delivery_and_late_acknowledgement_are_separate(self):
        from services.otc_ml.preview import MLPreview

        for delay, expected in ((1, "PENDING"), (5, "LATE")):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as folder:
                now = [1800000042.0]
                asset = Asset("A", True, True, 92, None, "Asset", now[0])
                journal = PreviewJournal(Path(folder) / "preview.db")
                preview = MLPreview(journal, FakeRuntime(), lambda: now[0])

                async def send(*args):
                    now[0] += delay

                bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
                await preview.plan(
                    bot, 1, ({"A": []}, {"A": asset}, {"A": now[0]}, None)
                )
                self.assertEqual(bot.send_message.await_count, 1)
                import sqlite3

                with closing(sqlite3.connect(journal.path)) as db:
                    row = db.execute(
                        "SELECT outcome,expires_at-entry_at FROM predictions"
                    ).fetchone()
                self.assertEqual(row[0], expected)
                self.assertEqual(row[1], 180)
                self.assertIn("Вин 0", journal.stats())
