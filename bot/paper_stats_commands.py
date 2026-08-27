from telegram import Update
from telegram.ext import ContextTypes

from bot.paper_commands import paper


async def paper_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = paper.engine.status()
    trades = paper.engine.history.all()

    total_profit = 0

    for trade in trades:
        profit = trade.get("profit")
        if profit is not None:
            total_profit += profit

    text = (
        f"📊 Paper Trading Stats\n\n"
        f"Баланс: {status['balance']} USDT\n"
        f"Сделок: {status['trades']}\n"
        f"Winrate: {status['winrate']}%\n"
        f"Общая прибыль: {round(total_profit, 2)} USDT\n"
        f"Открытая позиция: {'да' if status['has_position'] else 'нет'}"
    )

    await update.message.reply_text(text)