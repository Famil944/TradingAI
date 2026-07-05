from telegram import Update
from telegram.ext import ContextTypes

from services.trade_history_service import TradeHistoryService


history_service = TradeHistoryService()


async def paper_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = history_service.get_last_trades(10)

    if not trades:
        await update.message.reply_text("📄 История Paper-сделок пока пустая.")
        return

    text = "📄 История Paper Trading\n\n"

    for index, trade in enumerate(trades, start=1):
        symbol, side, entry, exit_price, profit, reason, created_at = trade

        text += (
            f"{index}. {symbol} {side}\n"
            f"Вход: {entry}\n"
            f"Выход: {exit_price}\n"
            f"Прибыль: {profit} USDT\n"
            f"Причина: {reason}\n"
            f"Время: {created_at}\n\n"
        )

    await update.message.reply_text(text)