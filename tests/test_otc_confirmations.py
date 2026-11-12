import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database.db import Database
from services.otc_confirmation_report import format_report, read_records, summarize
from services.otc_confirmations import VERSION, build, format_confirmations
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_strategy import Candle


def history():
    start = 1800000000
    bars = []
    for i in range(1000):
        price = 1 + i * 0.0001 + math.sin(i / 5) * 0.0003
        bars.append(
            Candle(
                start + 60 * i, price, price + 0.0002, price - 0.0002, price + 0.0001
            )
        )
    return bars


class ConfirmationTests(unittest.TestCase):
    def test_only_closed_bars_and_no_future_candle_changes(self):
        bars = history()
        asof = bars[-1].time + 60
        report = build(bars, "UP", 60, asof)
        future = Candle(asof, 100, 1000, 0.001, 999)
        self.assertEqual(report, build(bars + [future], "UP", 60, asof))
        self.assertEqual(report["available"], 5)
        self.assertEqual(report["count"], len(report["matched"]))
        self.assertNotIn("atr", report["checks"])
        self.assertNotIn("adx", report["checks"])
        self.assertIn("не вероятность", format_confirmations(report))

    def test_directional_checks_are_symmetric(self):
        bars = history()
        at = bars[-1].time + 60
        up, down = build(bars, "UP", 60, at), build(bars, "DOWN", 60, at)
        opposite = dict(agree="against", against="agree", neutral="neutral")
        for key in up["checks"]:
            self.assertEqual(down["checks"][key], opposite[up["checks"][key]])

    def test_gaps_stale_and_insufficient_data_are_unavailable(self):
        bars = history()
        at = bars[-1].time + 60
        for sample, cutoff in (
            (bars[-3:], at),
            (bars[:-10], at),
            (bars[:-5] + bars[-4:], at),
        ):
            with self.subTest(length=len(sample), cutoff=cutoff):
                report = build(sample, "UP", 60, cutoff)
                self.assertEqual(report["available"], 0)
                self.assertIn("недостаточно", format_confirmations(report))

    def test_m15_uses_closed_m1_alignment_and_m2_uses_m1(self):
        bars = history()
        at = bars[-1].time + 60
        report = build(bars, "UP", 900, at)
        self.assertEqual(report["alignment_period"], 60)
        self.assertEqual(build(bars, "UP", 120, at)["period"], 60)

    def test_groups_do_not_mix_modes_versions_horizons_or_missing_snapshots(self):
        report = build(history(), "UP", 60, history()[-1].time + 60)
        base = dict(
            mode="Основной",
            strategy="v5",
            seconds=60,
            confirmations=report,
            outcome="WIN",
        )
        records = [
            base,
            dict(base, outcome="LOSS"),
            dict(base, outcome="UNKNOWN"),
            dict(base, strategy="v6"),
            dict(base, mode="Groq"),
            dict(base, seconds=300),
            dict(base, confirmations=None),
            dict(base, confirmations=dict(report, available=4)),
        ]
        result = summarize(records)
        self.assertEqual(len(result["counts"]), 4)
        values = result["counts"][("Основной", "v5", 60, report["count"])]
        self.assertEqual(values["WIN"], 1)
        self.assertEqual(values["LOSS"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["incomplete"], 1)
        text = format_report(records)
        self.assertIn("50.0% (n=2)", text)
        self.assertIn("неизвестно 1", text)
        self.assertIn("мало данных", text)


class ConfirmationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_snapshot_matches_journal_and_survives_payout_change(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Database(root / "bot.db").init_db()
            journal = PreviewJournal(root / "primary-ready.db")
            bars = history()
            asof = bars[-1].time + 60
            now = asof + 42
            preview = MLPreview(journal, None, lambda: now)
            preview.snapshot = ({"A": bars}, {}, {}, None)
            selected = dict(
                symbol="A",
                name="Asset",
                direction="UP",
                payout=92,
                feature_asof=asof,
                entry_at=asof + 60,
                expires_at=asof + 120,
                model_sha256="v5",
            )
            bot = SimpleNamespace(
                send_message=AsyncMock(return_value=SimpleNamespace(message_id=42)),
                edit_message_text=AsyncMock(),
            )
            self.assertTrue(await preview.publish(selected, bot, 1))
            stored = journal.pending()[0]
            report = json.loads(stored["evidence"])["confirmations"]
            self.assertEqual(report, build(bars, "UP", 60, asof))
            self.assertIn(
                f"Индикаторы: {report['count']}", bot.send_message.call_args.args[1]
            )
            # Settlement ignores any later payout and uses the known price outcome.
            journal.finish(stored["id"], "WIN", 1, 1.1)
            await preview.update_cards(bot, 1)
            edit = bot.edit_message_text.call_args.kwargs
            self.assertEqual(edit["message_id"], 42)
            self.assertEqual(
                edit["text"], bot.send_message.call_args.args[1].replace("⬆️", "⬆️ ✅", 1)
            )
            self.assertEqual(journal.cards_to_update(), [])
            records = read_records(root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["confirmations"]["version"], VERSION)
            self.assertEqual(records[0]["outcome"], "WIN")
