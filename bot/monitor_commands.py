from telegram import Update
from telegram.ext import ContextTypes

from paper.position_monitor import PositionMonitor
from bot.paper_commands import paper
from core.trading_core import TradingCore


core = TradingCore()
monitor = PositionMonitor(core, paper)


async def paper_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = monitor.check_current_position()

    if result:
        await update.message.reply_text(result)
        return

    await update.message.reply_text("📄 Открытая Paper-сделка пока не достигла TP/SL.")