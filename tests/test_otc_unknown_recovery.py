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
    with sqlite3.connect(path) as db:
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
            with sqlite3.connect(journal.path) as db:
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

    async def test_bollinger_unknown_loss_does_not_invent_later_entries(self):
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
                    ("PFE_otc", 1320, 99.0),
                ],
            )
            recovery = UnknownRecovery(
                root, NoopProvider(), None, clock=lambda: 1500
            )
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 1)
            self.assertEqual(pending, 0)
            with sqlite3.connect(journal.path) as db:
                outcome, evidence = db.execute(
                    "SELECT outcome,evidence FROM predictions WHERE id=?",
                    (ident,),
                ).fetchone()
            payload = json.loads(evidence)
            self.assertEqual(outcome, "UNKNOWN")
            self.assertEqual(
                payload["attempt_outcomes"],
                ["LOSS", "PENDING", "PENDING"],
            )
            self.assertEqual(payload["stopped_after_index"], 0)
            rendered = card(payload, payload["attempt_outcomes"], outcome)
            self.assertIn("1. 03:20 · ❌", rendered)
            self.assertEqual(rendered.count("— отменена"), 2)

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
            now[0] += 61
            recovered, pending = await recovery.sweep()
            self.assertEqual(recovered, 1)
            self.assertEqual(pending, 0)
            self.assertEqual(provider.calls, 2)
            with sqlite3.connect(journal.path) as db:
                outcome = db.execute(
                    "SELECT outcome FROM signals WHERE id=?", (ident,)
                ).fetchone()[0]
            self.assertEqual(outcome, "WIN")


if __name__ == "__main__":
    unittest.main()
