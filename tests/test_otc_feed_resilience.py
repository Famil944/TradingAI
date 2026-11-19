"""Feed-resilience tests. No trading filters or signal selection are changed."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.otc_data import Plan, RetryableFeedError
from services.otc_delivery_guard import check_data
from services.otc_history_repair import (
    contiguous_tail_minutes, reconcile_closed_history,
)
from services.otc_market import Asset
from services.otc_session import OTCSession
from services.otc_strategy import Candle


NOW = 1_800_000_060  # aligned UTC minute for reproducible closed candles


def bar(at, price=1.0):
    return Candle(at, price, price + 0.02, price - 0.02, price + 0.01)


def history(n=950):
    return [bar(NOW - 60 * i) for i in range(n, 0, -1)]


def asset(at=NOW, payout=92):
    return Asset("AAA_otc", True, True, payout, None, "AAA OTC", at)


class HistoryReconciliationTests(unittest.TestCase):
    def test_partial_canonical_page_does_not_erase_closed_observations(self):
        old = history()
        corrected = bar(old[300].time, 1.1)
        merged = reconcile_closed_history(old, [corrected], NOW)
        self.assertEqual(len(merged), len(old))
        self.assertEqual(merged[300], corrected)
        self.assertEqual(merged[-1], old[-1])
        self.assertEqual(contiguous_tail_minutes(merged), len(old))

    def test_never_fills_gaps_or_accepts_future_and_bad_bars(self):
        old = [bar(NOW - 240), bar(NOW - 180), bar(NOW - 60)]
        invalid = Candle(NOW - 120, -1, 2, -2, 0)
        result = reconcile_closed_history(
            old, [bar(NOW), invalid, bar(NOW - 180, 1.2)], NOW,
        )
        self.assertEqual([x.time for x in result],
                         [NOW - 240, NOW - 180, NOW - 60])
        self.assertEqual(contiguous_tail_minutes(result), 1)
        self.assertEqual(result[1].open, 1.2)

    def test_original_data_gate_still_rejects_gap(self):
        existing = history(65)
        broken = existing[:35] + existing[36:]
        joined = reconcile_closed_history(broken, [], NOW)
        snap = ({"AAA_otc": joined}, {"AAA_otc": asset()}, {"AAA_otc": NOW}, None)
        selected = dict(symbol="AAA_otc", expires_at=NOW + 360,
                        feature_asof=NOW)
        self.assertEqual(check_data(selected, snap, NOW), "history_gap")
        repaired = reconcile_closed_history(joined, [existing[35]], NOW)
        snap = ({"AAA_otc": repaired}, {"AAA_otc": asset()}, {"AAA_otc": NOW}, None)
        self.assertIsNone(check_data(selected, snap, NOW))


class CatalogRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_catalogue_error_preserves_valid_cache_not_ttl(self):
        a = asset(NOW - 40)
        config = SimpleNamespace(
            catalogue_refresh_seconds=0,
            payout_max_age=60, require_paid_metadata=True, test_mode=False,
        )
        provider = SimpleNamespace(
            symbols=AsyncMock(side_effect=RetryableFeedError())
        )
        session = OTCSession(provider=provider, config=config, clock=lambda: NOW)
        # Finish after the single failed refresh instead of waiting forever.
        class ExitLoop(Exception):
            pass
        session._event = AsyncMock(side_effect=ExitLoop())
        active = {a.symbol: a}
        with self.assertRaises(ExitLoop):
            await session._refresh_stream_assets(active, dict(active))
        self.assertIn(a.symbol, active)
        selected = dict(symbol=a.symbol, expires_at=NOW + 360,
                        feature_asof=NOW)
        snap = ({a.symbol: history()}, active, {a.symbol: NOW}, None)
        self.assertIsNone(check_data(selected, snap, NOW))
        # A fresh quote cannot revive the expired catalogue observation.
        late = ({a.symbol: history()}, active, {a.symbol: NOW + 21}, None)
        self.assertEqual(
            check_data(selected, late, NOW + 21), "stale_or_ineligible_asset"
        )

    async def test_explicit_access_denial_still_clears_active_assets(self):
        from services.otc_data import AccessUnavailable
        config = SimpleNamespace(catalogue_refresh_seconds=0)
        provider = SimpleNamespace(symbols=AsyncMock(
            side_effect=AccessUnavailable("denied")
        ))
        session = OTCSession(provider=provider, config=config, clock=lambda: NOW)
        active = {"AAA_otc": asset()}
        with self.assertRaises(AccessUnavailable):
            await session._refresh_stream_assets(active, dict(active))
        self.assertEqual(active, {})

    async def test_partial_history_repair_merges_not_replaces(self):
        config = SimpleNamespace(pair_rotation_quota_reserve=100)
        existing = history()
        corrected = bar(existing[300].time, 1.1)
        provider = SimpleNamespace(
            usage=AsyncMock(return_value=Plan(
                "fake", 500, 7000, "day", NOW + 50000, 0, 1, 5, ("otc",)
            )),
            candles=AsyncMock(return_value=[corrected]),
        )
        session = OTCSession(provider=provider, config=config, clock=lambda: NOW)
        session._event = AsyncMock()
        cache = {"AAA_otc": existing[:]}
        await session._repair_history("AAA_otc", cache)
        self.assertEqual(len(cache["AAA_otc"]), 950)
        self.assertEqual(cache["AAA_otc"][300], corrected)
        self.assertEqual(contiguous_tail_minutes(cache["AAA_otc"]), 950)
        session._event.assert_awaited_with("history_repaired", "AAA_otc")


    async def test_reconnect_cannot_erase_history_if_provider_returns_a_partial_page(self):
        old = history()
        missing = old[400]
        cache = {"AAA_otc": old[:400] + old[401:]}
        provider = SimpleNamespace(
            usage=AsyncMock(return_value=Plan(
                "Lite", 600, 7000, "day", NOW + 30000, 0, 1, 5, ("otc",)
            )),
            symbols=AsyncMock(return_value=[asset()]),
            candles=AsyncMock(return_value=[missing]),
            catalogue={},
        )
        config = SimpleNamespace(
            pair_rotation_quota_reserve=100,
            payout_max_age=60, require_paid_metadata=True, test_mode=False,
        )
        session = OTCSession(provider=provider, config=config, clock=lambda: NOW)
        active = await session._restore_stream_history({"AAA_otc": asset()}, cache)
        self.assertIn("AAA_otc", active)
        self.assertEqual(len(cache["AAA_otc"]), len(old))
        self.assertEqual(contiguous_tail_minutes(cache["AAA_otc"]), len(old))
        provider.candles.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
