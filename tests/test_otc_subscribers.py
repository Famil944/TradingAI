"""Regression tests for viewer-only menus, opt-in signal relay and privacy."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault

from bot import community, otc_commands
from services.otc_subscribers import SubscriberRelay, Subscribers
from services.otc_strategies_live import LiveStrategyJournal, display_name, load_live_signals


def journal(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE predictions(
                id TEXT PRIMARY KEY, symbol TEXT, created_at REAL, entry_at REAL,
                expires_at REAL, direction TEXT, model_sha TEXT, evidence TEXT,
                sent_at REAL, outcome TEXT, resolved_at REAL
            )
        """)
        for index, row in enumerate(rows):
            at = row.get("created", 1000.0)
            db.execute(
                "INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row.get("id", f"s{index}"), "BTC", at, at + 20, at + 380,
                    "UP", "v1", json.dumps({
                        "name": "Bitcoin OTC",
                        "cycle_seconds": 120,
                        "attempt_outcomes": row.get("attempts", []),
                        "virtual": {"private": "DO_NOT_DISCLOSE", "stakes": [123456]},
                    }), at + 1, row.get("outcome", "PENDING"), at + 380,
                ),
            )


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))

    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
        self.edited.append((chat_id, message_id, text))


class DisplayNameTests(unittest.TestCase):
    def test_legacy_symbols_are_prettified(self):
        self.assertEqual(display_name("#JNJ_otc"), "JNJ OTC")
        self.assertEqual(display_name("AUDCHF_otc"), "AUD/CHF OTC")
        self.assertEqual(
            display_name("#PFE_otc", "Pfizer Inc OTC"),
            "Pfizer Inc OTC",
        )

    def test_live_journal_persists_catalogue_name_and_migrates_existing_db(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "community-live.db"
            # Simulate a pre-update database without display_name.
            with sqlite3.connect(path) as db:
                db.execute("""
                    CREATE TABLE signals(
                        id TEXT PRIMARY KEY,
                        strategy TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        seconds INTEGER NOT NULL,
                        created_at REAL NOT NULL,
                        entry_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        direction TEXT NOT NULL,
                        version TEXT NOT NULL,
                        sent_at REAL,
                        message_id INTEGER,
                        outcome TEXT NOT NULL DEFAULT 'RESERVED',
                        attempts_json TEXT NOT NULL DEFAULT '[]',
                        attempt_sources_json TEXT NOT NULL DEFAULT '[]',
                        resolved_at REAL,
                        card_sha TEXT,
                        UNIQUE(strategy,seconds,entry_at)
                    )
                """)

            journal = LiveStrategyJournal(path)
            candidate = {
                "symbol": "#PFE_otc",
                "name": "Pfizer Inc OTC",
                "entry_at": 1020,
                "expires_at": 1380,
                "direction": "UP",
                "experiment": {
                    "method": "ema_pullback",
                    "cycle_seconds": 120,
                },
            }
            ident = journal.reserve(candidate, 1000)
            self.assertIsNotNone(ident)
            journal.sent(ident, 1001, 7, "sha")

            rows = load_live_signals(path, 0)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "Pfizer Inc OTC")


class SubscriberStoreTests(unittest.TestCase):
    def test_register_unsubscribe_and_rejoin_are_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "users.db"
            store = Subscribers(path)
            store.subscribe(41, now=1000)
            store.subscribe(41, now=1020)
            self.assertEqual(store.active(), [(41, 1000.0)])
            store.unsubscribe(41, now=1030)
            self.assertEqual(store.active(), [])
            Subscribers(path).subscribe(41, now=1040)
            self.assertEqual(store.active(), [(41, 1040.0)])

    def test_users_only_explicitly_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Subscribers(Path(folder) / "users.db")
            self.assertIsNone(store.min_active_since())
            self.assertEqual(store.active(), [])


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_signal_sent_automatically_and_updated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            journal(root / "data" / "bollinger-cycles.db", [
                {"id": "new", "created": 1000, "outcome": "PENDING"}
            ])
            current = [1005]
            relay = SubscriberRelay(root, store=store, clock=lambda: current[0])
            bot = FakeBot()
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(bot.sent[0][0], 41)
            self.assertNotIn("DO_NOT_DISCLOSE", bot.sent[0][1])
            self.assertNotIn("123456", bot.sent[0][1])
            with sqlite3.connect(root / "data" / "bollinger-cycles.db") as db:
                db.execute(
                    "UPDATE predictions SET outcome='WIN',evidence=? WHERE id='new'",
                    (json.dumps({
                        "name": "Bitcoin OTC", "cycle_seconds": 120,
                        "attempt_outcomes": ["LOSS", "WIN"],
                        "virtual": {"private": "DO_NOT_DISCLOSE"}
                    }),),
                )
            await relay.update(bot)
            current[0] = 1410
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(len(bot.edited), 1)
            self.assertIn("✅", bot.edited[0][2])

    async def test_candidate_created_before_join_but_sent_after_join_is_delivered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=1005)
            journal(root / "data" / "bollinger-cycles.db", [
                {"id": "new_after_join", "created": 1000, "outcome": "PENDING"}
            ])
            with sqlite3.connect(root / "data" / "bollinger-cycles.db") as db:
                db.execute(
                    "UPDATE predictions SET sent_at=? WHERE id=?",
                    (1006, "new_after_join"),
                )
            relay = SubscriberRelay(root, store=store, clock=lambda: 1007)
            bot = FakeBot()
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(bot.sent[0][0], 41)

    async def test_publish_now_sends_all_new_strategy_signals_without_poll_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            relay = SubscriberRelay(root, store=store, clock=lambda: 1005)
            bot = FakeBot()
            for index, label in enumerate((
                "Боллинджер ×3",
                "Возврат внутрь",
                "Пробой/ретест",
                "Откат EMA9/19",
            )):
                await relay.publish_now(
                    bot,
                    {
                        "id": f"signal-{index}",
                        "origin": "main" if index == 0 else "live",
                        "strategy": "bollinger_live" if index == 0 else "test",
                        "version": "v1",
                        "label": label,
                        "seconds": 120,
                        "symbol": "TEST OTC",
                        "direction": "UP",
                        "created_at": 980,
                        "sent_at": 1004,
                        "entry_at": 1020,
                        "expires_at": 1380,
                        "resolved_at": 1380,
                        "outcome": "PENDING",
                        "attempts": [],
                    },
                )
            self.assertEqual(len(bot.sent), 4)
            self.assertEqual(
                {text.split(" · M", 1)[0] for _, text in bot.sent},
                {
                    "Боллинджер ×3",
                    "Возврат внутрь",
                    "Пробой/ретест",
                    "Откат EMA9/19",
                },
            )

    async def test_no_backfill_and_disabled_user_cannot_receive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=1200)
            journal(root / "data" / "bollinger-cycles.db", [
                {"id": "already_started", "created": 1000},
            ])
            relay = SubscriberRelay(root, store=store, clock=lambda: 1203)
            bot = FakeBot()
            await relay.update(bot)
            self.assertEqual(bot.sent, [])
            store.unsubscribe(41, now=1205)
            await relay.update(bot)
            self.assertEqual(bot.sent, [])

    async def test_no_late_signal_after_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            journal(root / "data" / "bollinger-cycles.db", [
                {"id": "late", "created": 1000},
            ])
            relay = SubscriberRelay(root, store=store, clock=lambda: 1021)
            bot = FakeBot()
            await relay.update(bot)
            self.assertEqual(bot.sent, [])


class LiveAttemptTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_attempt_progress_is_mirrored_without_wallet(self):
        from services.otc_public_broadcast import safe_card
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Subscribers(root / "data" / "subscribers.db")
            store.subscribe(41, now=990)
            journal(root / "data" / "bollinger-cycles.db", [
                {"id": "pending1", "created": 1000, "outcome": "PENDING"}
            ])
            live = SimpleNamespace(card_cache={"pending1": ["LOSS", "PENDING", "PENDING"]})
            relay = SubscriberRelay(
                root, store=store, clock=lambda: 1005, live_cycle=live
            )
            bot = FakeBot()
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertIn("❌", bot.sent[0][1])
            self.assertNotIn("DO_NOT_DISCLOSE", bot.sent[0][1])
            # The overall result is still PENDING. An attempt change must edit.
            live.card_cache["pending1"] = ["LOSS", "WIN", "PENDING"]
            await relay.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertEqual(len(bot.edited), 1)
            self.assertIn("✅", bot.edited[0][2])
            self.assertNotIn("DO_NOT_DISCLOSE", bot.edited[0][2])


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_shows_admin_commands_only_to_owner(self):
        from main import COMMANDS, SUBSCRIBER_COMMANDS, initialize_telegram

        class MockTelegram:
            def __init__(self):
                self.scopes = []
            async def set_my_commands(self, commands, scope=None, request_timeout=None):
                self.scopes.append((scope, [x.command for x in commands]))
            async def get_me(self, request_timeout=None):
                return SimpleNamespace(username="TradingBot")

        bot = MockTelegram()
        await initialize_telegram(bot)
        self.assertEqual(len(bot.scopes), 3)
        self.assertIsInstance(bot.scopes[0][0], BotCommandScopeDefault)
        self.assertIsInstance(bot.scopes[1][0], BotCommandScopeAllPrivateChats)
        self.assertIsInstance(bot.scopes[2][0], BotCommandScopeChat)
        self.assertEqual(set(bot.scopes[0][1]), {x[0] for x in SUBSCRIBER_COMMANDS})
        self.assertEqual(set(bot.scopes[2][1]), {x[0] for x in COMMANDS})
        self.assertNotIn("otc", bot.scopes[0][1])
        self.assertNotIn("otc_stats", bot.scopes[1][1])
        self.assertNotIn("suggestions", bot.scopes[0][1])
        self.assertNotIn("stop_signals", bot.scopes[2][1])

    def test_public_menu_has_no_admin_controls(self):
        buttons = [
            x.text
            for row in community.home(owner=False).inline_keyboard
            for x in row
        ]
        self.assertTrue(any("винрейт" in x.lower() for x in buttons))
        for x in buttons:
            self.assertNotIn("Запустить", x)
            self.assertNotIn("Остановить", x)
            self.assertNotIn("Административное", x)
            self.assertNotIn("баланс", x.lower())

    async def test_subscriber_start_only_registers_and_never_starts_engine(self):
        from unittest.mock import Mock
        from bot import community
        registry = SimpleNamespace(subscribe=Mock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=41),
            chat=SimpleNamespace(id=41, type="private"),
            answer=AsyncMock(),
        )
        engine_start = AsyncMock()
        with patch.object(community, "subscribers", return_value=registry), patch.object(
            community, "settings", SimpleNamespace(admin_chat_id=999)
        ), patch.object(otc_commands, "show_menu", engine_start):
            await community.start(message)
        registry.subscribe.assert_called_once_with(41)
        engine_start.assert_not_awaited()
        sent = message.answer.await_args.args[0]
        self.assertIn("Подписка", sent)
        self.assertNotIn("Вирт.", sent)

    async def test_owner_start_remains_in_private_admin_router(self):
        handler = AsyncMock()
        owner = SimpleNamespace(id=999)
        message = SimpleNamespace(
            from_user=owner, chat=SimpleNamespace(id=999, type="private")
        )
        with patch.object(community, "settings", SimpleNamespace(admin_chat_id=999)), patch.object(
            otc_commands, "show_menu", handler
        ):
            await community.start(message)
        handler.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
