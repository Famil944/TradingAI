import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from services.otc_ml.preview import MLPreview
from services.otc_strategy import Candle


class CanonicalSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_opens_override_opposite_tick_result(self):
        row = dict(
            id="p",
            symbol="A",
            entry_at=600,
            expires_at=660,
            direction="UP",
            sent_at=580,
        )
        journal = Mock()
        journal.pending.return_value = [row]
        provider = SimpleNamespace(
            usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
            candles=AsyncMock(
                return_value=[Candle(600, 2, 2, 2, 2), Candle(660, 1, 1, 1, 1)]
            ),
        )
        preview = MLPreview(
            journal, None, clock=lambda: 720, settlement_provider=provider
        )
        await preview.settle_pending(
            {"A": [Candle(600, 1, 1, 1, 1), Candle(660, 2, 2, 2, 2)]}
        )
        journal.finish.assert_called_once_with("p", "LOSS", 2, 1)

    async def test_missing_history_never_falls_back_to_ticks(self):
        row = dict(
            id="p",
            symbol="A",
            entry_at=600,
            expires_at=660,
            direction="UP",
            sent_at=580,
        )
        journal = Mock()
        journal.pending.return_value = [row]
        provider = SimpleNamespace(usage=AsyncMock(side_effect=TimeoutError()))
        clock = [720]
        preview = MLPreview(
            journal, None, clock=lambda: clock[0], settlement_provider=provider
        )
        ticks = {"A": [Candle(600, 1, 1, 1, 1), Candle(660, 2, 2, 2, 2)]}
        await preview.settle_pending(ticks)
        journal.finish.assert_not_called()
        clock[0] = 840
        await preview.settle_pending(ticks)
        journal.finish.assert_called_once_with("p", "UNKNOWN", None, None)
