from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from services.otc_bollinger_live import CycleJournal, card
from services.otc_strategies_live import LiveStrategyJournal
from services.otc_unknown_recovery import UnknownRecovery


def archive(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS bars("
            "symbol TEXT,time INTEGER,open REAL,high REAL,low REAL,close REAL,"
            "first_observed REAL,last_observed REAL,revisions INTEGER,source TEXT,"
            "PRIMARY KEY(symbol,time))"
        )
        for symbol, at, price in rows:
            db.execute(
                "INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?,?,?,?)",
                (symbol, at, price, price, price, price, at, at, 0, "otcharts_history"),
            )


class FakeGroq:
    def __init__(self):
        self.calls = []

    async def audit_recovery(self, records):
        self.calls.append(records)
        return {"mode": "audit"}


class NoopProvider:
    async def usage(self):
        return SimpleNamespace(remaining=1000)

    async def candles(self, symbol, now):
        return []


class UnknownRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_session_scans_local_unknown_without_using_quota(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = LiveStrategyJournal(data / "community-live.db")
            plan = {
                "symbol": "BTC_otc", "direction": "UP", "entry_at": 1200,
                "expires_at": 1560, "payout": 92,
                "experiment": {"method": "ema_pullback", "cycle_seconds": 120},
            }
            ident = journal.reserve(plan, 1180)
            journal.sent(ident, 1181, 5)
            journal.save_outcome(ident, ["UNKNOWN"], ["unconfirmed"], "UNKNOWN", 1600)
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=2000)),
                candles=AsyncMock(return_value=[]),
            )
            running = [True]
            foreground = SimpleNamespace(running=lambda: running[0])
            recovery = UnknownRecovery(root, provider, foreground=foreground, clock=lambda: 1700)
            self.assertEqual(await recovery.sweep(), (0, 1))
            self.assertTrue(recovery.paused_for_signals)
            provider.usage.assert_not_awaited()
            provider.candles.assert_not_awaited()
            running[0] = False
            self.assertEqual(await recovery.sweep(), (0, 1))
            self.assertFalse(recovery.paused_for_signals)
            provider.candles.assert_awaited_once()

    async def test_s1_recovers_during_live_session_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = CycleJournal(data / "bollinger-cycles.db", per_horizon=True)
            plan = dict(symbol="A", direction="UP", entry_at=1200, expires_at=1560,
                        cycle_seconds=120, payout=95)
            ident = journal.reserve(plan, 1180)
            journal.sent(ident, 1181, 9)
            journal.complete(ident, ["UNKNOWN"], "UNKNOWN", 1500, ["unconfirmed"])
            archive(data / "market-history.db", [("A",1200,100), ("A",1320,101)])
            provider = SimpleNamespace(usage=AsyncMock(), candles=AsyncMock())
            groq = SimpleNamespace(audit_recovery=AsyncMock())
            recovery = UnknownRecovery(root, provider, groq, clock=lambda:1700,
                foreground=SimpleNamespace(running=lambda:True), allow_backfill=False)
            self.assertEqual(await recovery.sweep(), (1,0))
            with closing(sqlite3.connect(journal.path)) as db:
                state, evidence = db.execute("SELECT outcome,evidence FROM predictions WHERE id=?", (ident,)).fetchone()
            self.assertEqual(state, "WIN")
            self.assertEqual(json.loads(evidence)["attempt_sources"], ["otcharts_history"])
            self.assertEqual(await recovery.sweep(), (0,0))
            provider.usage.assert_not_awaited()
            provider.candles.assert_not_awaited()
            groq.audit_recovery.assert_not_awaited()

    async def test_local_only_does_not_fetch_missing_prices_while_idle(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = SimpleNamespace(usage=AsyncMock(), candles=AsyncMock())
            recovery = UnknownRecovery(folder, provider, allow_backfill=False)
            await recovery._backfill({"A"})
            provider.usage.assert_not_awaited()
            provider.candles.assert_not_awaited()

    async def test_recovery_reserves_quota_for_new_signals(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = SimpleNamespace(
                usage=AsyncMock(return_value=SimpleNamespace(remaining=100)),
                candles=AsyncMock(return_value=[]),
            )
            recovery = UnknownRecovery(folder, provider, quota_reserve=100)
            await recovery._backfill({"BTC_otc", "ETH_otc"})
            provider.candles.assert_not_awaited()
            provider.usage.return_value = SimpleNamespace(remaining=101)
            await recovery._backfill({"BTC_otc", "ETH_otc"})
            self.assertEqual(provider.candles.await_count, 1)

    async def test_live_unknown_becomes_win_from_canonical_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = LiveStrategyJournal(data / "community-live.db")
            plan = {
                "symbol": "AUDCHF_otc",
                "name": "AUD/CHF OTC",
                "direction": "UP",
                "entry_at": 1020,
                "expires_at": 1380,
                "payout": 92,
                "experiment": {"method": "bb_reentry", "cycle_seconds": 120},
            }
            ident = journal.reserve(plan, 1000)
            journal.sent(ident, 1001, 1)
            journal.save_outcome(
                ident, ["UNKNOWN"], ["unconfirmed"], "UNKNOWN", 1400
            )
            archive(
                data / "market-history.db",
                [
                    ("AUDCHF_otc", 1020, 1.0000),
                    ("AUDCHF_otc", 1140, 1.0010),
                ],
            )
            groq = FakeGroq()
            recovery = UnknownRecovery(
                root, NoopProvider(), groq, clock=lambda: 1500
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 1)
            self.assertEqual(pending, 0)
            with closing(sqlite3.connect(journal.path)) as db, db:
                outcome, attempts, sources = db.execute(
                    "SELECT outcome,attempts_json,attempt_sources_json "
                    "FROM signals WHERE id=?",
                    (ident,),
                ).fetchone()
            self.assertEqual(outcome, "WIN")
            self.assertEqual(json.loads(attempts), ["WIN"])
            self.assertEqual(json.loads(sources), ["otcharts_history"])
            self.assertEqual(len(groq.calls), 1)
            self.assertEqual(groq.calls[0][0]["outcome"], "WIN")

    async def test_bollinger_unknown_cycle_is_reconstructed_until_win(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = CycleJournal(
                data / "bollinger-cycles.db",
                label="Боллинджер ×3",
                per_horizon=True,
            )
            plan = {
                "symbol": "PFE_otc",
                "name": "Pfizer Inc OTC",
                "entry_at": 1200,
                "expires_at": 1560,
                "cycle_seconds": 120,
                "direction": "UP",
                "payout": 92,
                "model_sha256": "test",
            }
            ident = journal.reserve(plan, 1180)
            journal.sent(ident, 1181, message_id=5)
            journal.complete(
                ident,
                ["UNKNOWN", "PENDING", "PENDING"],
                "UNKNOWN",
                1400,
            )
            archive(
                data / "market-history.db",
                [
                    ("PFE_otc", 1200, 100.0),
                    ("PFE_otc", 1320, 99.0),   # attempt 1 LOSS
                    ("PFE_otc", 1440, 101.0),  # attempt 2 WIN
                ],
            )
            recovery = UnknownRecovery(
                root, NoopProvider(), None, clock=lambda: 1500
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 2)
            self.assertEqual(pending, 0)
            with closing(sqlite3.connect(journal.path)) as db, db:
                outcome, evidence = db.execute(
                    "SELECT outcome,evidence FROM predictions WHERE id=?",
                    (ident,),
                ).fetchone()
            payload = json.loads(evidence)
            self.assertEqual(outcome, "WIN")
            self.assertEqual(payload["attempt_outcomes"], ["LOSS", "WIN"])
            self.assertEqual(payload["stopped_after_index"], 1)
            rendered = card(payload, payload["attempt_outcomes"], outcome)
            self.assertIn("1. 03:20 · ❌", rendered)
            self.assertIn("2. 03:22 · ✅", rendered)
            self.assertEqual(rendered.count("— отменена"), 1)

    async def test_live_unknown_cycle_recovers_all_three_losses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = LiveStrategyJournal(data / "community-live.db")
            plan = {
                "symbol": "TSLA_otc",
                "name": "Tesla OTC",
                "direction": "DOWN",
                "entry_at": 1800,
                "expires_at": 2160,
                "payout": 92,
                "experiment": {"method": "ema_pullback", "cycle_seconds": 120},
            }
            ident = journal.reserve(plan, 1780)
            journal.sent(ident, 1781, 1)
            journal.save_outcome(
                ident,
                ["LOSS", "UNKNOWN"],
                ["otcharts_tick", "unconfirmed"],
                "UNKNOWN",
                2100,
            )
            archive(
                data / "market-history.db",
                [
                    ("TSLA_otc", 1920, 100.0),
                    ("TSLA_otc", 2040, 101.0),  # attempt 2 LOSS for DOWN
                    ("TSLA_otc", 2160, 102.0),  # attempt 3 LOSS for DOWN
                ],
            )
            recovery = UnknownRecovery(
                root, NoopProvider(), None, clock=lambda: 2200
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 2)
            self.assertEqual(pending, 0)
            with closing(sqlite3.connect(journal.path)) as db, db:
                outcome, attempts = db.execute(
                    "SELECT outcome,attempts_json FROM signals WHERE id=?",
                    (ident,),
                ).fetchone()
            self.assertEqual(outcome, "LOSS")
            self.assertEqual(json.loads(attempts), ["LOSS", "LOSS", "LOSS"])

    async def test_recovery_stays_unknown_when_next_required_price_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = LiveStrategyJournal(data / "community-live.db")
            plan = {
                "symbol": "AEDCNY_otc",
                "name": "AED/CNY OTC",
                "direction": "UP",
                "entry_at": 1800,
                "expires_at": 2160,
                "payout": 92,
                "experiment": {"method": "break_retest", "cycle_seconds": 120},
            }
            ident = journal.reserve(plan, 1780)
            journal.sent(ident, 1781, 1)
            journal.save_outcome(
                ident,
                ["LOSS", "UNKNOWN"],
                ["otcharts_tick", "unconfirmed"],
                "UNKNOWN",
                2100,
            )
            archive(
                data / "market-history.db",
                [("AEDCNY_otc", 1920, 100.0)],
            )
            recovery = UnknownRecovery(
                root, NoopProvider(), None, clock=lambda: 2200
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 0)
            self.assertEqual(pending, 1)
            with closing(sqlite3.connect(journal.path)) as db, db:
                outcome = db.execute(
                    "SELECT outcome FROM signals WHERE id=?", (ident,)
                ).fetchone()[0]
            self.assertEqual(outcome, "UNKNOWN")

    async def test_network_failure_is_retried_and_backfill_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            journal = LiveStrategyJournal(data / "community-live.db")
            plan = {
                "symbol": "JNJ_otc",
                "name": "Johnson & Johnson OTC",
                "direction": "DOWN",
                "entry_at": 1800,
                "expires_at": 2160,
                "payout": 92,
                "experiment": {"method": "ema_pullback", "cycle_seconds": 120},
            }
            ident = journal.reserve(plan, 1780)
            journal.sent(ident, 1781, 1)
            journal.save_outcome(
                ident, ["UNKNOWN"], ["unconfirmed"], "UNKNOWN", 2200
            )
            now = [2300]

            class Provider:
                def __init__(self):
                    self.calls = 0

                async def usage(self):
                    return SimpleNamespace(remaining=1000)

                async def candles(self, symbol, _now):
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError()
                    archive(
                        data / "market-history.db",
                        [
                            ("JNJ_otc", 1800, 100.0),
                            ("JNJ_otc", 1920, 99.0),
                        ],
                    )
                    return [1, 2]

            provider = Provider()
            recovery = UnknownRecovery(
                root, provider, None, clock=lambda: now[0], interval=60
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 0)
            self.assertEqual(pending, 1)
            now[0] += 121  # bounded retry after a temporary upstream failure
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 1)
            self.assertEqual(pending, 0)
            self.assertEqual(provider.calls, 2)
            with closing(sqlite3.connect(journal.path)) as db, db:
                outcome = db.execute(
                    "SELECT outcome FROM signals WHERE id=?", (ident,)
                ).fetchone()[0]
            self.assertEqual(outcome, "WIN")


if __name__ == "__main__":
    unittest.main()
