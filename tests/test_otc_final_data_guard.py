import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.otc_delivery_guard import check_data
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_shadow_variants import prepare
from services.otc_strategy import Candle
from tests.test_otc_shadow_variants import candidate


class FinalDataTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        row = dict(
            symbol="A",
            name="A",
            entry_at=7200,
            expires_at=7320,
            direction="UP",
            payout=90,
            model_sha256="test",
            feature_asof=7140,
            evidence=dict(confirmations=dict(count=1, available=5, total=5)),
        )
        bars = [Candle(7140 - i * 60, 100, 101, 99, 100.5) for i in range(60, 0, -1)]
        snapshot = (
            {"A": bars},
            {"A": SimpleNamespace(open=True, payout=90, observed_at=7180)},
            {"A": 7180},
            None,
        )
        return row, snapshot

    def test_fresh_contiguous_closed_data_and_distinct_failure_reasons(self):
        row, snapshot = self.fixture()
        self.assertIsNone(check_data(row, snapshot, 7180))
        self.assertEqual(check_data(row, None, 7180), "missing_snapshot")
        self.assertEqual(check_data(row, snapshot, 7189), "stale_quote")
        bars, assets, quotes, end = snapshot
        self.assertEqual(
            check_data(row, ({"A": bars["A"][:-1]}, assets, quotes, end), 7180),
            "insufficient_closed_history",
        )
        broken = list(bars["A"])
        b = broken[30]
        broken[30] = Candle(b.time - 60, b.open, b.high, b.low, b.close)
        self.assertEqual(
            check_data(row, ({"A": broken}, assets, quotes, end), 7180), "history_gap"
        )
        self.assertEqual(check_data(row, (*snapshot[:3], 7300), 7180), "session_ending")

    async def test_data_changed_while_claiming_prevents_message(self):
        with tempfile.TemporaryDirectory() as folder:
            row, snapshot = self.fixture()
            journal = PreviewJournal(Path(folder) / "preview.db")
            preview = MLPreview(journal, None, clock=lambda: 7180)
            preview.snapshot = snapshot

            def claim(*args):
                snapshot[2].clear()
                return True

            bot = SimpleNamespace(send_message=AsyncMock())
            with patch("services.otc_delivery_guard.claim", side_effect=claim):
                await preview.publish(row, bot, 1)
            bot.send_message.assert_not_awaited()
            self.assertEqual(journal.pending(), [])

    def test_two_votes_and_two_families_are_different_shadow_hypotheses(self):
        row = candidate(120)
        row["evidence"] = dict(
            confirmations=dict(
                count=2,
                available=5,
                checks=dict(
                    ema="agree",
                    macd="agree",
                    rsi="neutral",
                    candle="against",
                    alignment="against",
                ),
            )
        )
        result = prepare([row], {})[0]
        self.assertEqual(result["experiment"]["masks"], {})
        self.assertTrue(result["selected"])
        self.assertNotIn("experiment", row)
