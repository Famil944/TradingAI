from telegram import Update
from telegram.ext import ContextTypes

from core.trading_core import TradingCore
from core.multi_timeframe_analyzer import MultiTimeframeAnalyzer


core = TradingCore()
multi_tf = MultiTimeframeAnalyzer(core)


async def multi_tf_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напиши так: /multi BTCUSDT")
        return

    symbol = context.args[0].upper()
    data = multi_tf.analyze(symbol)

    text = (
        f"📊 Multi-Timeframe Analysis\n\n"
        f"Монета: {data['symbol']}\n"
        f"Итоговый сигнал: {data['final_signal']}\n"
        f"Средняя оценка: {data['avg_score']} / 100\n\n"
    )

    for item in data["results"]:
        text += (
            f"{item['interval']}: {item['signal']} | "
            f"Score: {item['score']}\n"
        )

    await update.message.reply_text(text)