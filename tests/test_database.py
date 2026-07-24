import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.db import Database
from database.demo_trade_repository import DemoTradeRepository


class DatabaseTests(unittest.TestCase):
    def test_configured_database_path_and_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                database = Database()
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

    def test_orphaned_trade_is_removed_from_open_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trades.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                repository = DemoTradeRepository()
                repository.save_open_trade(
                    {
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "entry_price": 100,
                        "quantity": 1,
                        "take_profit": 110,
                        "stop_loss": 95,
                    }
                )
                repository.mark_last_open_trade(
                    "BTCUSDT",
                    "ORPHANED",
                    "missing on exchange",
                )
                self.assertEqual(repository.get_open_trades(), [])

    def test_close_persists_costs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "close.db"
            with patch.dict(os.environ, {"TRADING_AI_DB_PATH": str(path)}):
                repository = DemoTradeRepository()
                repository.save_open_trade(
                    {
                        "symbol": "ETHUSDT",
                        "side": "SHORT",
                        "entry_price": 100,
                        "quantity": 1,
                        "take_profit": 90,
                        "stop_loss": 105,
                    }
                )
                repository.close_last_open_trade(
                    symbol="ETHUSDT",
                    exit_price=90,
                    pnl=9.8,
                    pnl_percent=10,
                    close_reason="TAKE_PROFIT",
                    commission=0.2,
                    funding=0,
                )
                trade = repository.get_recent_trades(1)[0]
                self.assertEqual(trade["status"], "CLOSED")
