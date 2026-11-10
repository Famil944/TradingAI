import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User
from aiohttp import web

from bot import otc_commands as commands
from services.otc_data import AccessUnavailable, OTChartsProvider


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    def message(self, user=1, chat=1, kind="private"):
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat, type=kind),
            from_user=User(id=user, is_bot=False, first_name="Owner"),
            text="/otc",
        )

    async def test_owner_message_and_callback_authorization(self):
        handler = AsyncMock()
        middleware = commands.OwnerOnly()
        config = SimpleNamespace(admin_chat_id=1)
        with (
            patch.object(commands, "settings", config),
            patch.object(Message, "answer", new_callable=AsyncMock),
            patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
        ):
            for message in [
                self.message(2, 2),
                self.message(2, 1),
                self.message(1, -1, "group"),
            ]:
                await middleware(handler, message, {})
            self.assertEqual(handler.await_count, 0)
            await middleware(handler, self.message(), {})
            self.assertEqual(handler.await_count, 1)
            callback = CallbackQuery(
                id="1",
                from_user=User(id=2, is_bot=False, first_name="Other"),
                chat_instance="1",
                message=self.message(),
                data="otc:start:0",
            )
            await middleware(handler, callback, {})
            self.assertEqual(handler.await_count, 1)

    async def test_timeframe_selection_and_required_commands(self):
        from main import COMMANDS

        self.assertEqual(
            {c for c, _ in COMMANDS},
            {
                "start",
                "help",
                "otc",
                "otc_status",
                "otc_stop",
                "otc_stats",
                "otc_diagnostics",
            },
        )
        commands.choices[1] = (300,)
        keyboard = commands.menu(1, conditional=False)
        labels = [
            b.text
            for row in keyboard.inline_keyboard
            for b in row
            if b.text.startswith("✅")
        ]
        self.assertEqual(labels, ["✅ M5"])
        commands.choices.clear()


class LoggingTests(unittest.TestCase):
    def test_exception_urls_and_keys_are_redacted(self):
        import logging

        from core.logging import SecretFormatter

        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            1,
            "URL /bot%s request key=%s",
            ("example-token", "example-key"),
            None,
        )
        text = SecretFormatter(("example-token", "example-key")).format(record)
        self.assertNotIn("example-token", text)
        self.assertNotIn("example-key", text)
        self.assertIn("[REDACTED]", text)


class HTTPContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.headers = []

        async def usage(request):
            self.headers.append(request.headers.get("Authorization"))
            return web.json_response(
                dict(
                    planName="fake",
                    books=["otc"],
                    requests=dict(remaining=100, quota=200, per="month", resets=200),
                    streams=dict(open=0, limit=1, instrumentsPerStream=3),
                )
            )

        async def symbols(request):
            self.assertEqual(request.query["venue"], "otc")
            return web.json_response(
                {"symbols": [dict(symbol="A", payout=95, isOtc=True, open=None)]}
            )

        async def candles(request):
            self.assertEqual(request.query["tf"], "60")
            return web.json_response(
                {"candles": [dict(time=1800007200, open=1, high=2, low=0.5, close=1.5)]}
            )

        async def stream(request):
            return web.Response(
                text=':keepalive\n\ndata: {"time":1800007200,"symbol":"A","price":1.5}\n\n',
                content_type="text/event-stream",
            )

        async def refusal(request):
            return web.Response(status=403, text="private server details must not leak")

        app = web.Application()
        for path, handler in [
            ("/v1/usage", usage),
            ("/v1/symbols", symbols),
            ("/v1/candles", candles),
            ("/v1/stream", stream),
            ("/refused", refusal),
        ]:
            app.router.add_get(path, handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.provider = OTChartsProvider("offline-test-key")
        session = await self.provider._http()
        original = session.get

        def get(url, **kwargs):
            return original(url.replace("https://otcharts.com", self.base), **kwargs)

        self.patcher = patch.object(session, "get", side_effect=get)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        await self.provider.close()
        await self.runner.cleanup()

    async def test_official_rest_sse_contract_and_unknown_payout_age(self):
        plan = await self.provider.usage()
        self.assertEqual(plan.per, "month")
        self.assertEqual(plan.remaining, 100)
        self.assertEqual(self.headers, ["Bearer offline-test-key"])
        assets = await self.provider.symbols()
        self.assertIsNone(assets[0].payout_at)
        self.assertIsNone(assets[0].open)
        bars = await self.provider.candles("A", 1800000060)
        self.assertEqual(bars[0].time, 1800000000)
        ticks = [t async for t in self.provider.ticks(["A"])]
        self.assertEqual(ticks[0].time, 1800000000)
        self.assertEqual(ticks[0].symbol, "A")
        with self.assertRaises(AccessUnavailable) as error:
            await self.provider._get("/refused")
        self.assertNotIn("private server details", str(error.exception))


if __name__ == "__main__":
    unittest.main()
