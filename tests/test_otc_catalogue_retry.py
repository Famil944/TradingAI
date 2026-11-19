"""The catalogue retry loop must never bypass original quote/payout guards."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from services.otc_data import (
    AccessUnavailable, OTChartsProvider, RetryableFeedError,
)
from services.otc_market import Asset
from services.otc_session import OTCSession


NOW = 1_800_000_000


def fresh_asset():
    return Asset("AAA_otc", True, True, 93, NOW, "AAA", NOW)


class CatalogueRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_retries_respect_retry_after_and_recover(self):
        failure = RetryableFeedError(30, reason="http_503")
        later = RetryableFeedError(reason="network")
        provider = SimpleNamespace(
            symbols=AsyncMock(side_effect=[
                failure, later, RetryableFeedError(reason="timeout"),
                [fresh_asset()],
            ])
        )
        cfg = SimpleNamespace(
            catalogue_refresh_seconds=45,
            payout_max_age=60,
            require_paid_metadata=True,
            test_mode=False,
            pair_rotation_quota_reserve=100,
        )
        session = OTCSession(provider=provider, config=cfg, clock=lambda: NOW)
        active = {}
        delays = []

        async def record_sleep(delay):
            delays.append(delay)
            if len(delays) == 5:
                raise asyncio.CancelledError

        with patch("services.otc_session.asyncio.sleep", side_effect=record_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await session._refresh_stream_assets(
                    active, {"AAA_otc": fresh_asset()},
                )
        self.assertEqual(delays, [45, 30, 10, 45, 45])
        self.assertIn("AAA_otc", active)
        self.assertEqual(provider.symbols.await_count, 4)
        self.assertEqual(session._health_counts["catalogue_refresh_failed"], 3)

    async def test_retry_budget_keeps_quota_reserve(self):
        provider = SimpleNamespace(
            symbols=AsyncMock(side_effect=RetryableFeedError(reason="timeout"))
        )
        cfg = SimpleNamespace(
            catalogue_refresh_seconds=45,
            payout_max_age=60,
            pair_rotation_quota_reserve=100,
        )
        session = OTCSession(provider=provider, config=cfg, clock=lambda: NOW)
        session.plan = SimpleNamespace(remaining=100)
        delays = []

        async def record_sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        with patch("services.otc_session.asyncio.sleep", side_effect=record_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await session._refresh_stream_assets({}, {})
        self.assertEqual(delays, [45, 45, 45])

    async def test_provider_malformed_catalogue_is_retriable_not_empty(self):
        provider = OTChartsProvider("offline-test")
        provider.catalogue = {"AAA_otc": fresh_asset()}
        provider._get = AsyncMock(side_effect=[
            {"bad_field": []},
            {"symbols": [{"symbol": "AAA_otc"}, "invalid"]},
        ])
        for _ in range(2):
            with self.assertRaises(RetryableFeedError) as err:
                await provider.symbols()
            self.assertEqual(err.exception.reason, "invalid_catalogue")
            self.assertEqual(set(provider.catalogue), {"AAA_otc"})

    async def test_server_errors_labelled_and_access_refusal_not_retried(self):
        for code, kind in [(503, "http_503"), (502, "http_5xx")]:
            response = SimpleNamespace(status=code, headers={"Retry-After": "25"})
            with self.assertRaises(RetryableFeedError) as err:
                OTChartsProvider._check(response)
            self.assertEqual(err.exception.reason, kind)
            if code == 503:
                self.assertEqual(err.exception.retry_after, 25)
        with self.assertRaises(AccessUnavailable):
            OTChartsProvider._check(SimpleNamespace(status=429, headers={}))

    async def test_startup_respects_provider_retry_after(self):
        provider = SimpleNamespace(
            symbols=AsyncMock(side_effect=[
                RetryableFeedError(30, reason="http_503"), [],
            ])
        )
        session = SimpleNamespace(provider=provider)
        delays = []

        async def record_sleep(seconds):
            delays.append(seconds)

        with patch("services.otc_session.asyncio.sleep", side_effect=record_sleep):
            result = await OTCSession._catalogue_with_retry(session)
        self.assertEqual(result, [])
        self.assertEqual(provider.symbols.await_count, 2)
        self.assertGreaterEqual(delays[0], 30)


if __name__ == "__main__":
    unittest.main()
