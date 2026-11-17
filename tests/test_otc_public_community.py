"""Offline regression tests: public forecast stats and private suggestions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.otc_feedback import FeedbackStore
from services.otc_public_stats import (
    combined_cycles, describe, main_signals, metric_units, metrics,
)
from services.otc_public_broadcast import public_text, PublicPublisher


def signal(identifier, created, outcome, *, secs=120, version="v1", strat="a",
           resolved=None, origin="main", attempts=None):
    return {
        "id": identifier, "origin": origin, "strategy": strat, "version": version,
        "seconds": secs, "label": "ТЕСТ", "symbol": identifier,
        "direction": "UP", "created_at": created, "entry_at": created + 20,
        "expires_at": created + 20 + secs * 3,
        "resolved_at": resolved if resolved is not None else created + secs * 3 + 20,
        "outcome": outcome, "attempts": attempts or [],
    }


class CombinedTests(unittest.TestCase):
    def test_loss_then_win_gives_one_x6_win(self):
        a = signal("avalanche", 1000, "LOSS", resolved=1380,
                   attempts=["LOSS", "LOSS", "LOSS"])
        b = signal("bitcoin", 1390, "WIN", resolved=1770,
                   attempts=["LOSS", "LOSS", "WIN"])
        cycles = combined_cycles([b, a], now=2000)
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["outcome"], "WIN")
        self.assertEqual(cycles[0]["parts"], 2)
        self.assertEqual(cycles[0]["attempts_count"], 6)
        self.assertEqual(metrics(cycles)["wr"], 100.0)
        self.assertEqual(metrics(metric_units([a, b], "A"))["win"], 1)
        self.assertEqual(metrics(metric_units([a, b], "A"))["loss"], 5)

    def test_second_loss_is_one_x6_loss(self):
        rows = [
            signal("a", 1000, "LOSS", resolved=1380),
            signal("b", 1400, "LOSS", resolved=1780),
        ]
        self.assertEqual([r["outcome"] for r in combined_cycles(rows, now=2000)], ["LOSS"])

    def test_first_win_ends_early(self):
        rows = [
            signal("a", 1000, "WIN"),
            signal("b", 1400, "WIN"),
        ]
        result = combined_cycles(rows, now=2000)
        self.assertEqual(len(result), 2)
        self.assertTrue(all(r["parts"] == 1 for r in result))

    def test_no_pairing_before_first_loss_resolves(self):
        rows = [
            signal("a", 1000, "LOSS", resolved=1800),
            signal("b", 1400, "WIN", resolved=1750),
        ]
        result = combined_cycles(rows, now=2000)
        self.assertEqual(len(result), 2)
        self.assertEqual([r["outcome"] for r in result], ["WAIT_SECOND", "WIN"])

    def test_no_cross_version_or_expiry_or_strategy(self):
        rows = [
            signal("a", 1000, "LOSS", resolved=1300),
            signal("b", 1400, "WIN", version="v2"),
            signal("c", 1500, "WIN", secs=300),
            signal("d", 1600, "WIN", strat="other"),
        ]
        result = combined_cycles(rows, now=1700)
        self.assertEqual(len(result), 4)
        self.assertEqual(sum(r["outcome"] == "WIN" for r in result), 3)

    def test_stale_loss_is_incomplete_not_loss(self):
        first = signal("a", 1000, "LOSS", resolved=1380)
        later = signal("b", 9400, "WIN")
        result = combined_cycles([first, later], now=10000, wait_seconds=3600)
        self.assertEqual([r["outcome"] for r in result], ["INCOMPLETE", "WIN"])

    def test_neutral_not_in_winrate_denominator(self):
        rows = [signal("a", 1000, "DRAW"), signal("b", 1400, "UNKNOWN")]
        m = metrics(combined_cycles(rows, now=2500))
        self.assertIsNone(m["wr"])
        self.assertEqual(m["known"], 0)
        self.assertEqual(m["draw"], 1)
        self.assertEqual(m["unknown"], 1)


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "bollinger-cycles.db"
        with sqlite3.connect(self.source) as db:
            db.execute("""
                CREATE TABLE predictions(
                    id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,
                    expires_at REAL,direction TEXT,model_sha TEXT,evidence TEXT,
                    sent_at REAL,outcome TEXT,resolved_at REAL
                )
            """)

    def write(self, identifier, now, outcome="WIN", attempts=None, virtual="PRIVATE-123"):
        evidence = {
            "name": "Bitcoin OTC", "cycle_seconds": 120,
            "attempt_outcomes": attempts or ["LOSS", "WIN", "PENDING"],
            "virtual": {"status": virtual, "stakes": [999999, 888888]},
        }
        with sqlite3.connect(self.source) as db:
            db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, "BTCOTC", now, now+20, now+380, "UP",
                "bollinger-live-three-attempts-v1",
                json.dumps(evidence), now+1, outcome, now+380,
            ))

    def test_public_never_includes_wallet_or_private_card_lines(self):
        self.write("id1", 1000)
        rows = main_signals(self.source)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempts"], ["LOSS", "WIN"])
        for text in [str(rows), public_text(rows[0]), describe(rows, "B", 120)]:
            self.assertNotIn("PRIVATE-123", text)
            self.assertNotIn("999999", text)
            self.assertNotIn("Вирт.", text)

    def test_cancels_and_undelivered_are_excluded(self):
        self.write("id1", 1000)
        self.write("id2", 2000, "NOT_DELIVERED")
        self.assertEqual(len(main_signals(self.source)), 1)

    def test_unsupported_expiry_not_added(self):
        self.write("id1", 1000)
        with sqlite3.connect(self.source) as db:
            payload = db.execute(
                "SELECT evidence FROM predictions WHERE id='id1'"
            ).fetchone()[0]
            item = json.loads(payload)
            item["cycle_seconds"] = 60
            db.execute("UPDATE predictions SET evidence=? WHERE id='id1'", (json.dumps(item),))
        self.assertEqual(main_signals(self.source), [])


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = FeedbackStore(Path(self.tmp.name) / "feedback.db")

    def test_private_inbox_dedup_rate_limit_and_moderation(self):
        self.db.begin(100, now=1000)
        self.assertEqual(self.db.state(100)["mode"], "SUGGEST")
        self.assertEqual(self.db.submit(100, 100, 42, "Предлагаю улучшить M2", now=1000), 1)
        self.assertEqual(self.db.submit(100, 100, 42, "Предлагаю улучшить M2", now=1000), 1)
        with self.assertRaises(ValueError):
            self.db.submit(100, 100, 43, "Добавить фильтр", now=1300)
        self.assertEqual(self.db.submit(100, 100, 44, "Другой вариант", now=1700), 2)
        self.assertTrue(self.db.set_status(1, "REVIEW", now=1800))
        self.assertEqual(self.db.get(1)["status"], "REVIEW")
        self.assertEqual(len(self.db.list()), 2)

    def test_length_and_status_validation(self):
        with self.assertRaises(ValueError):
            self.db.submit(1, 1, 1, "x")
        with self.assertRaises(ValueError):
            self.db.set_status(1, "ADMIN")
        self.db.begin(1)
        self.db.cancel(1)
        self.assertIsNone(self.db.state(1))


class PublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_ack_tracking_and_no_double_publish(self):
        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edited = []
            async def send_message(self, chat_id, text, parse_mode=None):
                self.sent.append(text)
                class R:
                    message_id = len(self.sent)
                return R()
            async def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
                self.edited.append(text)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            publisher = PublicPublisher(root, -100123)
            now = publisher.enabled_at + 1
            with sqlite3.connect(root / "data/bollinger-cycles.db") as db:
                db.execute("""
                    CREATE TABLE predictions(
                        id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,
                        expires_at REAL,direction TEXT,model_sha TEXT,evidence TEXT,
                        sent_at REAL,outcome TEXT,resolved_at REAL
                    )
                """)
                db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    "id1", "BTC", now, now+20, now+380, "UP", "v1",
                    json.dumps({"name": "Bitcoin OTC", "cycle_seconds": 120,
                                "virtual": {"private": "NEVER-LEAK"}}),
                    now+1, "PENDING", now+380,
                ))
            bot = FakeBot()
            await publisher.update(bot)
            await publisher.update(bot)
            self.assertEqual(len(bot.sent), 1)
            self.assertNotIn("NEVER-LEAK", bot.sent[0])
            with sqlite3.connect(root / "data/bollinger-cycles.db") as db:
                db.execute("UPDATE predictions SET outcome='WIN' WHERE id='id1'")
            await publisher.update(bot)
            await publisher.update(bot)
            self.assertEqual(len(bot.edited), 1)



class BatchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_unknown_does_not_rewrite_sent_batch_or_repeat_it(self):
        class FakeBot:
            def __init__(self):
                self.sent = []
                self.edited = []
            async def send_message(self, chat_id, text, parse_mode=None):
                self.sent.append(text)
                class Ack:
                    message_id = len(self.sent)
                return Ack()
            async def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
                self.edited.append(text)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            publisher = PublicPublisher(root, -1001234567)
            initial = publisher.enabled_at + 1
            journal = root / "data/bollinger-cycles.db"
            with sqlite3.connect(journal) as db:
                db.execute("""
                    CREATE TABLE predictions(
                        id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,
                        expires_at REAL,direction TEXT,model_sha TEXT,evidence TEXT,
                        sent_at REAL,outcome TEXT,resolved_at REAL
                    )
                """)
                for index in range(11):
                    at = initial + index * 600
                    outcome = "UNKNOWN" if index == 0 else "WIN"
                    db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        f"b{index}", "BTC", at, at+20, at+380, "UP", "v1",
                        json.dumps({
                            "name": "Bitcoin OTC", "cycle_seconds": 120,
                            "attempt_outcomes": ["WIN"] if outcome == "WIN" else [],
                        }), at+1, outcome, at+380,
                    ))
            bot = FakeBot()
            await publisher.update(bot)
            with sqlite3.connect(publisher.path) as db:
                initial_reports = db.execute("SELECT COUNT(*) FROM sent_batches").fetchone()[0]
            self.assertEqual(initial_reports, 3)  # A, B, C x 10 wins
            await publisher.update(bot)
            with sqlite3.connect(publisher.path) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM sent_batches").fetchone()[0], 3
                )
            with sqlite3.connect(journal) as db:
                db.execute("UPDATE predictions SET outcome='WIN',evidence=? WHERE id='b0'", (
                    json.dumps({
                        "name": "Bitcoin OTC", "cycle_seconds": 120,
                        "attempt_outcomes": ["WIN"],
                    }),
                ))
                for index in range(11, 20):
                    at = initial + index * 600
                    db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        f"b{index}", "BTC", at, at+20, at+380, "UP", "v1",
                        json.dumps({
                            "name": "Bitcoin OTC", "cycle_seconds": 120,
                            "attempt_outcomes": ["WIN"],
                        }), at+1, "WIN", at+380,
                    ))
            await publisher.update(bot)
            with sqlite3.connect(publisher.path) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM sent_batches").fetchone()[0], 6
                )
            await publisher.update(bot)
            with sqlite3.connect(publisher.path) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM sent_batches").fetchone()[0], 6
                )

    async def test_publisher_refuses_non_channel_destinations(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                PublicPublisher(folder, 1234567)
            with self.assertRaises(ValueError):
                PublicPublisher(folder, -12345)


class PublicationFlagsTests(unittest.TestCase):
    def test_owner_shadow_switch_is_durable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a = PublicPublisher(root, -100123456)
            before = a.flags()["ema_pullback"]
            after = a.toggle_shadow("ema_pullback")
            self.assertEqual(after, not bool(before))
            b = PublicPublisher(root, -100123456)
            self.assertEqual(bool(b.flags()["ema_pullback"]), after)
            self.assertEqual(b.toggle_shadow("ema_pullback"), bool(before))
            with self.assertRaises(ValueError):
                a.toggle_shadow("admin")


class ExportTests(unittest.TestCase):
    def test_daily_csv_exports_known_and_unknown_without_wallet(self):
        import csv
        from scripts.recover_otc_history import export
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "data").mkdir()
            path = root / "data/bollinger-cycles.db"
            with sqlite3.connect(path) as db:
                db.execute("""
                    CREATE TABLE predictions(
                        id TEXT PRIMARY KEY,symbol TEXT,created_at REAL,entry_at REAL,
                        expires_at REAL,direction TEXT,model_sha TEXT,evidence TEXT,
                        sent_at REAL,outcome TEXT,resolved_at REAL
                    )
                """)
                for i, state in enumerate(("WIN", "UNKNOWN")):
                    db.execute("INSERT INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                        "s"+str(i), "BTC", 1000+i*400, 1020+i*400, 1380+i*400,
                        "UP", "v1", json.dumps({
                            "cycle_seconds": 120,
                            "attempt_outcomes": ["WIN"] if state == "WIN" else [],
                            "virtual": {"balance": "TOP-SECRET"}
                        }), 1001+i*400, state, 1380+i*400
                    ))
            result = export(root, root / "data/exports", with_shadow=False)
            self.assertEqual(result["signals"], 2)
            self.assertEqual(result["confirmed"], 1)
            text = (root / "data/exports/signals.csv").read_text(encoding="utf-8-sig")
            self.assertNotIn("TOP-SECRET", text)
            self.assertNotIn("balance", text)
            with (root / "data/exports/daily.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            self.assertTrue(any(row["metric"] == "B" for row in rows))


if __name__ == "__main__":
    unittest.main()
