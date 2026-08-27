import tempfile
import unittest
from pathlib import Path

from database.db import Database


class DatabaseTests(unittest.TestCase):
    def test_configured_database_path_and_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            database = Database(str(path))
            database.init_db()
            with database.connect() as connection:
                foreign_keys = connection.execute(
                    "PRAGMA foreign_keys"
                ).fetchone()[0]
                busy_timeout = connection.execute(
                    "PRAGMA busy_timeout"
                ).fetchone()[0]

            self.assertEqual(database.db_path, path.resolve())
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(busy_timeout, 10000)

    def test_manual_trade_survives_new_database_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.db"
            database = Database(str(path))
            database.init_db()
            signal_id = database.save_signal({
                "symbol": "BTCUSDT", "score": 80, "entry_price": 100,
                "entry_zone_min": 99, "entry_zone_max": 101,
                "tp1": 103, "tp2": 105, "tp3": 108, "tp4": 115,
                "stop_loss": 97, "stop_loss_percent": 3,
                "support": 98, "resistance": 110, "risk_reward": 2,
            })
            trade_id = database.open_manual_trade(123, signal_id, 100.5)

            reopened = Database(str(path))
            reopened.init_db()
            trades = reopened.get_manual_trades(123, status="open")

            self.assertIsInstance(trade_id, int)
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["symbol"], "BTCUSDT")
            self.assertEqual(trades[0]["entry_price"], 100.5)

    def test_manual_trade_can_be_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "trades.db"))
            database.init_db()
            signal_id = database.save_signal({
                "symbol": "ETHUSDT", "score": 75, "entry_price": 100,
                "entry_zone_min": 99, "entry_zone_max": 101,
                "tp1": 103, "tp2": 105, "tp3": 108, "tp4": 115,
                "stop_loss": 97, "stop_loss_percent": 3,
                "support": 98, "resistance": 110, "risk_reward": 2,
            })
            trade_id = database.open_manual_trade(123, signal_id, 100)
            self.assertTrue(database.close_manual_trade(trade_id, 123, 104))
            trade = database.get_manual_trades(123)[0]
            self.assertEqual(trade["status"], "closed")
            self.assertEqual(trade["close_price"], 104)


if __name__ == "__main__":
    unittest.main()
