from telegram import Update
from telegram.ext import ContextTypes

from bot.paper_commands import paper
from core.trading_core import TradingCore
from paper.position_calculator import PositionCalculator


core = TradingCore()
calculator = PositionCalculator()


async def position_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    position = paper.engine.trader.position

    if not position:
        await update.message.reply_text("📭 Открытой позиции нет.")
        return

    current_price = core.market.get_price(position.symbol)
    data = calculator.calculate(position, current_price)

    text = (
        f"📌 Текущая Paper-позиция\n\n"
        f"Монета: {position.symbol}\n"
        f"Сторона: {position.side}\n\n"
        f"Вход: {position.entry_price}\n"
        f"Текущая цена: {current_price}\n"
        f"Объём: {position.amount}\n\n"
        f"P/L: {data['profit']} USDT "
        f"({data['profit_percent']}%)\n"
        f"До TP: {data['distance_to_tp']}%\n"
        f"До SL: {data['distance_to_sl']}%\n\n"
        f"Take Profit: {position.take_profit}\n"
        f"Stop Loss: {position.stop_loss}\n"
        f"Partial TP: {'да' if position.partial_closed else 'нет'}"
    )

    await update.message.reply_text(text)