from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from exchange.binance_client import BinanceMarketClient
from indicators.market_analyzer import MarketAnalyzer


market = BinanceMarketClient()
analyzer = MarketAnalyzer()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Trading AI Bot работает.\n\n"
        "Команды:\n"
        "/status - статус\n"
        "/price - цена BTC\n"
        "/signal - анализ BTC"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает. Режим: анализ рынка.")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        btc_price = market.get_price("BTCUSDT")
        await update.message.reply_text(f"📊 BTC/USDT: {btc_price:,.2f} USDT")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка Binance: {e}")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        klines = market.get_klines("BTCUSDT", "1h", 200)
        data = analyzer.analyze(klines)

        text = (
            f"📊 BTC/USDT\n\n"
            f"Цена: {data['price']:.2f} USDT\n"
            f"Тренд: {data['trend']}\n"
            f"EMA20: {data['ema20']}\n"
            f"EMA50: {data['ema50']}\n"
            f"RSI: {data['rsi']}"
        )

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


def run_telegram_bot(token: str):
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("signal", signal))

    print("✅ Telegram bot started")
    app.run_polling()