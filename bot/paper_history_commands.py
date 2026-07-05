from telegram import Update
from telegram.ext import ContextTypes

from bot.paper_commands import paper


async def paper_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = paper.engine.history.all()

    if not trades:
        await update.message.reply_text("📄 История Paper-сделок пока пустая.")
        return

    text = "📄 История Paper Trading\n\n"

    for index, trade in enumerate(trades[-10:], start=1):
        symbol = trade.get("symbol", "Unknown")
        side = trade.get("side", "N/A")
        entry = trade.get("entry_price", "N/A")
        profit = trade.get("profit", None)

        text += f"{index}. {symbol} {side}\n"
        text += f"Вход: {entry}\n"

        if profit is not None:
            text += f"Прибыль: {profit} USDT\n"

        text += "\n"

    await update.message.reply_text(text)