from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from core.trading_core import TradingCore


core = TradingCore()


POPULAR_COINS = [
    ("BTC", "BTCUSDT"),
    ("ETH", "ETHUSDT"),
    ("SOL", "SOLUSDT"),
    ("BNB", "BNBUSDT"),
    ("XRP", "XRPUSDT"),
    ("DOGE", "DOGEUSDT"),
    ("ADA", "ADAUSDT"),
    ("LINK", "LINKUSDT"),
    ("AVAX", "AVAXUSDT"),
    ("TON", "TONUSDT"),
]


def main_menu():
    keyboard = [
        [InlineKeyboardButton("📊 Анализ рынка", callback_data="menu_analyze")],
        [InlineKeyboardButton("💰 Цена BTC", callback_data="price_btc")],
        [InlineKeyboardButton("⚙️ Статус", callback_data="status")],
    ]
    return InlineKeyboardMarkup(keyboard)


def coins_menu():
    keyboard = []

    for i in range(0, len(POPULAR_COINS), 2):
        row = []
        for name, symbol in POPULAR_COINS[i:i + 2]:
            row.append(
                InlineKeyboardButton(
                    name,
                    callback_data=f"analyze_{symbol}"
                )
            )
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Trading AI Bot работает.\n\n"
        "Выбери действие:",
        reply_markup=main_menu()
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот работает.\n"
        "Режим: Trading AI Core v3\n"
        "Автосделки: выключены\n"
        "Режим риска: безопасный"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = core.analyze_symbol("BTCUSDT", "1h")
        await update.message.reply_text(f"📊 BTC/USDT: {data['price']:,.2f} USDT")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка цены: {e}")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = core.analyze_symbol("BTCUSDT", "1h")
        await send_analysis_message(update, data)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text(
                "📊 Выбери монету для анализа:",
                reply_markup=coins_menu()
            )
            return

        symbol = context.args[0].upper()
        data = core.analyze_symbol(symbol, "1h")
        await send_analysis_message(update, data)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data == "back_main":
            await query.edit_message_text(
                "🤖 Главное меню:",
                reply_markup=main_menu()
            )

        elif data == "menu_analyze":
            await query.edit_message_text(
                "📊 Выбери монету для анализа:",
                reply_markup=coins_menu()
            )

        elif data == "price_btc":
            result = core.analyze_symbol("BTCUSDT", "1h")
            await query.edit_message_text(
                f"📊 BTC/USDT: {result['price']:,.2f} USDT",
                reply_markup=main_menu()
            )

        elif data == "status":
            await query.edit_message_text(
                "✅ Бот работает.\n"
                "Режим: Trading AI Core v3\n"
                "Автосделки: выключены\n"
                "Режим риска: безопасный",
                reply_markup=main_menu()
            )

        elif data.startswith("analyze_"):
            symbol = data.replace("analyze_", "")
            await query.edit_message_text(f"⏳ Анализирую {symbol}...")

            result = core.analyze_symbol(symbol, "1h")
            text = format_analysis(result)

            await query.edit_message_text(
                text,
                reply_markup=coins_menu()
            )

    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка: {e}")


async def send_analysis_message(update: Update, data: dict):
    await update.message.reply_text(format_analysis(data))


def format_analysis(data: dict) -> str:
    reasons_text = "\n".join([f"• {reason}" for reason in data["reasons"]])

    return (
        f"📊 {data['symbol']}\n\n"
        f"Таймфрейм: {data['interval']}\n"
        f"Цена: {data['price']:.2f} USDT\n"
        f"Тренд: {data['trend']}\n"
        f"Объём: {data['volume_status']}\n\n"
        f"EMA20: {data['ema20']}\n"
        f"EMA50: {data['ema50']}\n"
        f"EMA200: {data['ema200']}\n"
        f"RSI: {data['rsi']}\n"
        f"MACD: {data['macd']}\n"
        f"MACD Signal: {data['macd_signal']}\n"
        f"ATR: {data['atr']}\n"
        f"Bollinger High: {data['bb_high']}\n"
        f"Bollinger Low: {data['bb_low']}\n\n"
        f"Сигнал: {data['signal']}\n"
        f"Оценка: {data['score']} / 100\n"
        f"Риск: {data['risk']}\n\n"
        f"Причины:\n{reasons_text}"
    )


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
    app.add_handler(CommandHandler("analyze", analyze))

    app.add_handler(CallbackQueryHandler(handle_button))

    print("✅ Telegram bot started")
    app.run_polling()