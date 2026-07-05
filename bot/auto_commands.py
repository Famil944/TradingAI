import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from auto.auto_loop import AutoLoop
from auto.auto_state import AutoState
from auto.auto_trader import AutoTrader
from bot.paper_commands import paper
from core.trading_core import TradingCore
from scanner.market_scanner import MarketScanner
from services.telegram_notifier import TelegramNotifier
from auto.position_watch_loop import PositionWatchLoop
from paper.event_setup import setup_trade_events


core = TradingCore()
scanner = MarketScanner(core)
auto_state = AutoState()
notifier = TelegramNotifier()
auto_trader = AutoTrader(scanner, paper, core)
auto_loop = AutoLoop(auto_state, auto_trader, notifier)
setup_trade_events(paper, notifier)
position_watch_loop = PositionWatchLoop(core, paper, notifier)


async def auto_once(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notifier.setup(context.bot, update.effective_chat.id)
    result = auto_trader.run_once()
    await update.message.reply_text(result)


async def auto_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notifier.setup(context.bot, update.effective_chat.id)

    auto_state.turn_on()
    await update.message.reply_text("🤖 Авто-режим включён")

    if not auto_loop.is_running:
        asyncio.create_task(auto_loop.start())
        
    if not position_watch_loop.is_running:
        asyncio.create_task(position_watch_loop.start())    


async def auto_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(auto_state.turn_off())


async def auto_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Авто-режим: {auto_state.status()}"
    )