from telegram import Update
from telegram.ext import ContextTypes

from auto.auto_trader import AutoTrader
from bot.paper_commands import paper
from core.trading_core import TradingCore
from scanner.market_scanner import MarketScanner


core = TradingCore()
scanner = MarketScanner(core)
auto_trader = AutoTrader(scanner, paper)


async def auto_once(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = auto_trader.run_once()
    await update.message.reply_text(result)