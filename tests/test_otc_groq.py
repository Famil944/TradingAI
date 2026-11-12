import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from services.otc_groq import GroqClient, GroqPreview, market_packet
from services.otc_market import Asset
from services.otc_ml.preview import PreviewJournal
from services.otc_strategy import Candle


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class GroqTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "groq.db"
        self.journal = PreviewJournal(self.path, label="Groq")
        self.now = [1800000030.0]
        self.client = GroqClient("secret", self.path, clock=lambda: self.now[0])
        self.packet = dict(
            assets=[dict(symbol="A")],
            durations_minutes=[1, 2, 3],
            entry_at=1800000060,
            asof=self.now[0],
        )

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_rate_limit_only_pauses_groq_and_survives_restart(self):
        session = Mock()
        session.post.return_value = Response(429, headers={"retry-after": "120"})
        self.client.session = session
        self.assertIsNone(await self.client.ask(self.packet))
        self.assertIsNone(await self.client.ask(self.packet))
        session.post.assert_called_once()
        restarted = GroqClient("secret", self.path, clock=lambda: self.now[0])
        self.assertIsNone(restarted.reserve(10))
        self.assertEqual(self.journal.pending(), [])

    async def test_network_error_and_invalid_output_do_not_escape(self):
        session = Mock()
        session.post.side_effect = TimeoutError()
        self.client.session = session
        self.assertIsNone(await self.client.ask(self.packet))
        self.now[0] += 301
        session.post.side_effect = None
        session.post.return_value = Response(
            200,
            dict(
                choices=[
                    dict(
                        message=dict(
                            content='{"action":"UP","symbol":"INVENTED","minutes":2,"reason":"x"}'
                        )
                    )
                ]
            ),
        )
        self.assertIsNone(await self.client.ask(self.packet))

    async def test_budgets_are_persistent_and_attempts_are_charged(self):
        limited = GroqClient(
            "secret",
            self.path,
            max_requests=1,
            max_tokens=1000,
            clock=lambda: self.now[0],
        )
        self.assertIsNotNone(limited.reserve(100))
        self.now[0] += 301
        self.assertIsNone(limited.reserve(100))
        limited = GroqClient(
            "secret",
            self.path,
            max_requests=10,
            max_tokens=101,
            clock=lambda: self.now[0],
        )
        self.assertIsNone(limited.reserve(2))

    async def test_packet_excludes_future_and_stale_assets(self):
        now = 1800000030.0
        bars = [
            Candle(
                1800000000 - 60 * i,
                1 + i * 0.001,
                1.2 + i * 0.001,
                0.9 + i * 0.001,
                1.01 + i * 0.001,
            )
            for i in range(100)
        ]
        bars.sort(key=lambda b: b.time)
        asset = Asset("A", True, True, 92, None, "A", now)
        packet = market_packet({"A": bars}, {"A": asset}, {"A": now}, now, None)
        self.assertEqual(len(packet["assets"]), 1)
        self.assertTrue(
            all(b[0] + 60 <= now for b in packet["assets"][0]["candles_m1"])
        )
        self.assertNotIn(1800000000, [b[0] for b in packet["assets"][0]["candles_m1"]])
        self.assertFalse(
            market_packet({"A": bars}, {"A": asset}, {"A": now - 9}, now, None)[
                "assets"
            ]
        )

    async def test_success_is_audited_without_displaying_probability(self):
        session = Mock()
        session.post.return_value = Response(
            200,
            dict(
                id="response",
                usage=dict(total_tokens=100),
                choices=[
                    dict(
                        message=dict(
                            content='{"action":"SKIP","symbol":"","minutes":1,"reason":"mixed"}'
                        )
                    )
                ],
            ),
        )
        self.client.session = session
        result = await self.client.ask(self.packet)
        self.assertEqual(result["decision"]["action"], "SKIP")
        self.assertNotIn("secret", str(result))

    async def test_late_answer_never_publishes(self):
        now = [1800000030.0]
        client = SimpleNamespace(model="model")

        async def late(packet):
            now[0] = packet["entry_at"] - 14
            return dict(decision=dict(action="UP", symbol="A", minutes=1))

        client.ask = late
        preview = GroqPreview(self.journal, client, clock=lambda: now[0])
        preview.publish = AsyncMock()
        await preview.plan(Mock(), 1, ({}, {}, {}, None))
        preview.publish.assert_not_awaited()

    async def test_journal_stats_are_separate(self):
        self.assertIn("Groq", self.journal.stats())
        self.assertNotIn("XGBoost", self.journal.stats())
