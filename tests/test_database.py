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
            self.assertAlmostEqual(trades[0]["tp1"], 100.5 * 1.03)

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
            stats = database.get_manual_trade_statistics(123)
            self.assertEqual(stats["closed"], 1)
            self.assertEqual(stats["wins"], 1)

    def test_pending_trade_requires_user_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "pending.db"))
            database.init_db()
            signal_id = database.save_signal({
                "symbol": "SOLUSDT", "score": 80, "entry_price": 100,
                "entry_zone_min": 99, "entry_zone_max": 101,
                "tp1": 103, "tp2": 103, "tp3": 103, "tp4": 103,
                "stop_loss": 97, "stop_loss_percent": 3,
                "support": 98, "resistance": 110, "risk_reward": 1,
            })
            trade_id = database.open_manual_trade(123, signal_id, 100)
            database.set_trade_pending(
                trade_id, "TP +3%", 103, "2026-08-27 10:00:00"
            )
            self.assertEqual(
                database.get_manual_trades(123)[0]["status"], "pending_close"
            )
            self.assertTrue(database.confirm_pending_trade(trade_id, 123))
            trade = database.get_manual_trades(123)[0]
            self.assertEqual(trade["status"], "closed")
            self.assertEqual(trade["close_reason"], "TP +3%")

    def test_trade_entry_and_position_can_be_corrected(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(str(Path(directory) / "edit.db"))
            database.init_db()
            signal_id = database.save_signal({
                "symbol": "XRPUSDT", "score": 80, "entry_price": 1,
                "entry_zone_min": 0.99, "entry_zone_max": 1.01,
                "tp1": 1.03, "tp2": 1.03, "tp3": 1.03, "tp4": 1.03,
                "stop_loss": 0.97, "stop_loss_percent": 3,
                "support": 0.98, "resistance": 1.1, "risk_reward": 1,
            })
            trade_id = database.open_manual_trade(123, signal_id, 1)
            self.assertTrue(database.edit_manual_trade(trade_id, 123, 1.1, 11))
            trade = database.get_manual_trades(123)[0]
            self.assertAlmostEqual(trade["tp1"], 1.133)
            self.assertAlmostEqual(trade["quantity"], 10)

    def test_pump_prediction_and_background_setting_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pump.db"
            database = Database(str(path))
            database.init_db()
            database.set_pump_background(123, True)
            prediction_id = database.save_pump_prediction(123, {
                "symbol": "TESTUSDT", "score": 75, "stage": "impulse",
                "price": 1.0, "metrics": {"volume_ratio_1m": 2.0},
                "news_score": 0, "news_items": 1, "news_critical": False,
            })

            reopened = Database(str(path))
            reopened.init_db()
            rows = reopened.get_pump_predictions(user_id=123)
            self.assertTrue(reopened.get_pump_background(123))
            self.assertIsInstance(prediction_id, int)
            self.assertEqual(rows[0]["symbol"], "TESTUSDT")
            self.assertEqual(reopened.get_pump_statistics(123)["observing"], 1)
            self.assertEqual(database.get_database_id(), reopened.get_database_id())

            reopened.update_pump_prediction(prediction_id, max_price=1.06)
            stats = reopened.get_pump_statistics(123)
            self.assertEqual(stats["successful"], 1)
            self.assertEqual(stats["observing_confirmed"], 1)
            self.assertEqual(stats["waiting"], 0)


if __name__ == "__main__":
    unittest.main()
