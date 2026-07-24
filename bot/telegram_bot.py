import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from core.trading_core import TradingCore
from scanner.market_scanner import MarketScanner
from bot.paper_commands import paper_on, paper_off, paper_status, paper

# from bot.monitor_commands import paper_check
from bot.auto_commands import (
    auto_once,
    auto_on,
    auto_off,
    auto_status,
    auto_trader,
    auto_state,
    auto_loop,
    position_watch_loop,
    notifier,
)
from bot.multi_tf_commands import multi_tf_analyze
from bot.paper_history_commands import paper_history
from bot.paper_stats_commands import paper_stats
from bot.position_commands import position_status
from bot.persistent_keyboard import persistent_keyboard
from telegram.ext import MessageHandler, filters
from bot.menu_command import open_menu
from bot.inline_menus import (
    app_main_menu,
    auto_menu,
    paper_menu,
    market_menu,
    stats_menu,
    settings_menu,
)
from bot.handlers.menu_handlers import (
    show_main_menu,
    show_market_menu,
    show_auto_menu,
    show_paper_menu,
    show_help,
    show_help_page,
)
from bot.dashboard import build_dashboard
from bot.handlers.paper_handlers import (
    show_paper_status,
    show_paper_balance,
    show_paper_stats,
    show_paper_history,
    show_last_trade,
)
from bot.handlers.auto_handlers import (
    run_auto_once,
    turn_auto_on,
    turn_auto_off,
    show_position,
)

from bot.handlers.market_handlers import (
    show_market_scan,
    show_btc_price,
)
from bot.handlers.stats_handlers import (
    show_profit,
    show_winrate,
    show_today,
    show_week,
    show_month,
)

from services.demo_trading_controller import DemoTradingController

from bot.handlers.settings_handlers import (
    show_settings,
    show_risk,
    show_trade_size,
    show_quality,
    show_timeframe,
    show_notifications,
    apply_setting,
    show_trading_mode,
    confirm_live_mode,
)
from bot.handlers.analytics_handlers import show_trade_analytics
from bot.handlers.demo_stats_handlers import demo_stats
from bot.handlers.demo_history_handlers import trade_history
from services.telegram_notifier import notifier as demo_notifier
from services.demo_trade_log_service import DemoTradeLogService
import config.trading_mode as trading_mode
from config.trading_mode import LIVE_TRADING_ENABLED, TradingMode
from services.trading_mode_switcher import TradingModeSwitcher
from services.error_formatter import user_error_message

core = TradingCore()
scanner = MarketScanner(core)
demo_controller = DemoTradingController()
demo_trade_log = DemoTradeLogService()
mode_switcher = TradingModeSwitcher()


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

def coins_menu():
    keyboard = []

    for i in range(0, len(POPULAR_COINS), 2):
        row = []
        for name, symbol in POPULAR_COINS[i : i + 2]:
            row.append(InlineKeyboardButton(name, callback_data=f"analyze_{symbol}"))
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


async def start(update, context):
    await update.message.reply_text(
        "🤖 Добро пожаловать в Trading AI Bot!\n\n"
        "Нажмите кнопку 🏠 Меню, чтобы открыть главное меню.",
        reply_markup=persistent_keyboard(),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Бот работает.\n"
        "Режим: Trading AI Core v5\n"
        "Автосделки: выключены\n"
        "Режим риска: безопасный"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await asyncio.to_thread(
            core.analyze_symbol, "BTCUSDT", "1h"
        )
        await update.message.reply_text(f"📊 BTC/USDT: {data['price']:,.2f} USDT")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка цены: {e}")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data = await asyncio.to_thread(
            core.analyze_symbol, "BTCUSDT", "1h"
        )
        await send_analysis_message(update, data)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            await update.message.reply_text(
                "📊 Выбери монету для анализа:",
                reply_markup=coins_menu(),
            )
            return

        symbol = context.args[0].upper()
        data = await asyncio.to_thread(
            core.analyze_symbol, symbol, "1h"
        )
        await send_analysis_message(update, data)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка анализа: {e}")


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("⏳ Сканирую рынок...")
        results = await asyncio.to_thread(
            scanner.scan_market, "1h", 10
        )
        text = format_scan_results(results)
        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка сканера: {e}")

async def demo_long(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if trading_mode.CURRENT_MODE != TradingMode.DEMO:
        await update.message.reply_text(
            "⛔ /demo_long доступна только при TRADING_MODE=DEMO."
        )
        return
    await _open_current_trade(update, context, "LONG")


async def trade_long(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _open_current_trade(update, context, "LONG")


async def _open_current_trade(update, context, direction):
    try:
        demo_notifier.setup(
            context.bot,
            update.effective_chat.id,
        )

        symbol = "BTCUSDT"

        if context.args:
            symbol = context.args[0].upper()

        signal = {
            "symbol": symbol,
            "signal": "🟢 LONG" if direction == "LONG" else "🔴 SHORT",
            "strategy": f"MANUAL_{trading_mode.CURRENT_MODE.value}",
            "quality_score": 0,
        }

        result = await asyncio.to_thread(
            demo_controller.open_demo_trade, signal
        )

        await update.message.reply_text(result)

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка открытия "
            f"{trading_mode.CURRENT_MODE.value}-сделки:\n{error}"
        )

async def demo_short(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if trading_mode.CURRENT_MODE != TradingMode.DEMO:
        await update.message.reply_text(
            "⛔ /demo_short доступна только при TRADING_MODE=DEMO."
        )
        return
    await _open_current_trade(update, context, "SHORT")


async def trade_short(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _open_current_trade(update, context, "SHORT")


async def trading_mode_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    live_status = (
        "разблокирован"
        if LIVE_TRADING_ENABLED
        else "заблокирован"
    )
    await update.message.reply_text(
        f"Режим торговли: {trading_mode.CURRENT_MODE.value}\n"
        f"Live-доступ: {live_status}"
    )

async def demo_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = await asyncio.to_thread(
        demo_controller.client.open_positions
    )

    await update.message.reply_text(str(positions))

async def demo_close(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        demo_notifier.setup(
            context.bot,
            update.effective_chat.id,
        )

        symbol = "BTCUSDT"

        if context.args:
            symbol = context.args[0].upper()

        positions = await asyncio.to_thread(
            demo_controller.client.open_positions, symbol
        )

        if not positions:
            await update.message.reply_text(
                f"📭 Открытой Demo-позиции по {symbol} нет."
            )
            return

        exit_price = float(
            demo_controller.client.price(symbol)["price"]
        )

        result = await asyncio.to_thread(
            demo_controller.manager.close_position,
            symbol,
            exit_price,
            "MANUAL_CLOSE",
        )

        if result is None:
            await update.message.reply_text(
                "❌ Не удалось закрыть сделку.\n"
                "Открытая запись сделки не найдена в базе."
            )
            return

        pnl = result["pnl"]
        pnl_percent = result["pnl_percent"]

        await update.message.reply_text(
            f"✅ Demo-позиция закрыта вручную\n\n"
            f"Монета: {symbol}\n"
            f"Цена входа: {result['entry_price']}\n"
            f"Цена выхода: {result['exit_price']}\n"
            f"PnL: {pnl} USDT\n"
            f"PnL: {pnl_percent}%"
        )

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка закрытия Demo-позиции:\n{error}"
        )

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    try:
        if data in ["back_main", "back_app_main"]:
            await show_main_menu(query)

        elif data == "refresh_dashboard":
            await query.edit_message_text(
                build_dashboard(paper, auto_state),
                reply_markup=app_main_menu(),
            )

        elif data == "menu_market":
            await show_market_menu(query)

        elif data == "menu_auto":
            await show_auto_menu(query)

        elif data == "menu_paper":
            await show_paper_menu(query)

        elif data == "menu_stats":
            await query.edit_message_text(
                "📊 Статистика",
                reply_markup=stats_menu(),
            )

        elif data == "menu_settings":
            await show_settings(query)

        elif data == "menu_notifications":
            await show_notifications(query)

        elif data == "stats_profit_btn":
            await show_trade_analytics(query)

        elif data == "stats_winrate_btn":
            await show_winrate(query, paper)

        elif data == "stats_today_btn":
            await show_today(query, paper)

        elif data == "stats_week_btn":
            await show_week(query, paper)

        elif data == "stats_month_btn":
            await show_month(query, paper)

        elif data == "settings_size_btn":
            await show_trade_size(query)

        elif data == "settings_risk_btn":
            await show_risk(query)

        elif data == "settings_quality_btn":
            await show_quality(query)

        elif data == "settings_timeframe_btn":
            await show_timeframe(query)

        elif data == "settings_mode_btn":
            await show_trading_mode(query, trading_mode.CURRENT_MODE)

        elif data.startswith("set_"):
            await apply_setting(query, data)

        elif data == "mode_select_demo":
            await apply_trading_mode(query, TradingMode.DEMO)

        elif data == "mode_select_live":
            await confirm_live_mode(query)

        elif data == "mode_confirm_live":
            await apply_trading_mode(query, TradingMode.LIVE)

        elif data == "menu_help":
            await show_help(query)

        elif data.startswith("help_"):
            await show_help_page(query, data)

        elif data == "menu_analyze":
            await query.edit_message_text(
                "📊 Выбери монету для анализа:",
                reply_markup=coins_menu(),
            )

        elif data == "market_scan":
            await show_market_scan(query, scanner, format_scan_results)

        elif data == "price_btc":
            await show_btc_price(query, core)

        elif data == "auto_once_btn":
            await run_auto_once(query, auto_trader)

        elif data == "auto_on_btn":
            await turn_auto_on(
                query,
                context,
                auto_state,
                auto_loop,
                position_watch_loop,
                notifier,
            )

        elif data == "auto_off_btn":
            await turn_auto_off(
                query,
                auto_state,
                auto_loop,
                position_watch_loop,
            )

        elif data == "position_btn":
            await show_position(query, demo_controller)

        elif data == "last_analysis_btn":
            await query.edit_message_text(
                auto_trader.last_analysis
                or "📭 Автоанализ ещё не запускался.",
                reply_markup=auto_menu(),
            )

        elif data == "paper_status_btn":
            await show_paper_status(query, paper)

        elif data == "paper_stats_btn":
            await show_paper_stats(query, paper)

        elif data == "paper_balance_btn":
            await show_paper_balance(query, paper)

        elif data == "last_trade_btn":
            await show_last_trade(query, paper)

        elif data == "paper_history_btn":
            await show_paper_history(query, paper)

        elif data == "status":
            await query.edit_message_text(
                "✅ Бот работает.\n"
                "Режим: Trading AI Core v5\n"
                "Режим риска: безопасный",
                reply_markup=app_main_menu(),
            )

        elif data.startswith("analyze_"):
            symbol = data.replace("analyze_", "")
            await query.edit_message_text(f"⏳ Анализирую {symbol}...")

            result = await asyncio.to_thread(
                core.analyze_symbol, symbol, "1h"
            )
            text = format_analysis(result)

            paper_text = paper.try_trade_text(result)

            if paper_text:
                text += "\n\n" + paper_text

            await query.edit_message_text(
                text,
                reply_markup=coins_menu(),
            )

    except Exception as error:
        message = user_error_message(error)
        if message is None:
            return
        await query.edit_message_text(f"❌ Ошибка: {message}")


async def apply_trading_mode(query, target_mode):
    auto_state.turn_off()
    auto_loop.stop()
    position_watch_loop.stop()

    await query.edit_message_text(
        f"⏳ Проверяю доступ к {target_mode.value}..."
    )
    result = await asyncio.to_thread(
        mode_switcher.switch,
        target_mode,
        demo_controller.client,
    )
    trading_mode.set_current_mode(target_mode)
    demo_controller.set_client(result["client"])
    auto_trader.demo_controller.set_client(result["client"])
    await query.edit_message_text(
        f"✅ Режим {result['mode']} применён.\n\n"
        "Ключи и соединение проверены. Открытых позиций нет.\n"
        "Перезапуск бота не требуется.",
        reply_markup=settings_menu(),
    )


async def send_analysis_message(update: Update, data: dict):
    await update.message.reply_text(format_analysis(data))

    paper_text = paper.try_trade_text(data)

    if paper_text:
        await update.message.reply_text(paper_text)

async def demo_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if await asyncio.to_thread(
            demo_controller.client.open_positions
        ):
            await update.message.reply_text(
                "⛔ Сначала закройте все Demo-позиции."
            )
            return
        demo_trade_log.delete_all_trades()

        await update.message.reply_text(
            "🗑 Demo Trading полностью очищен."
        )

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка:\n{error}"
        )


def format_analysis(data: dict) -> str:
    reasons_text = "\n".join([f"• {reason}" for reason in data["reasons"]])

    fear_greed = data.get("fear_greed", {})
    fg_value = fear_greed.get("value", "Нет данных")
    fg_status = fear_greed.get("status", "Нет данных")
    fg_comment = fear_greed.get("comment", "")

    funding = data.get("funding_rate", {})
    funding_value = funding.get("funding_rate", "Нет данных")
    funding_status = funding.get("status", "Нет данных")
    funding_comment = funding.get("comment", "")

    open_interest = data.get("open_interest", {})
    oi_value = open_interest.get("open_interest", "Нет данных")
    oi_status = open_interest.get("status", "Нет данных")

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
        f"😨 Fear & Greed: {fg_value}\n"
        f"Состояние: {fg_status}\n"
        f"Комментарий: {fg_comment}\n\n"
        f"💰 Funding Rate: {funding_value}%\n"
        f"Состояние: {funding_status}\n"
        f"Комментарий: {funding_comment}\n\n"
        f"📊 Open Interest: {oi_value}\n"
        f"Состояние: {oi_status}\n\n"
        f"Сигнал: {data['signal']}\n"
        f"Оценка: {data['score']} / 100\n"
        f"Риск: {data['risk']}\n\n"
        f"Причины:\n{reasons_text}"
    )


def format_scan_results(results: list) -> str:
    if not results:
        return "❌ Сканер не нашёл данные."

    text = "🔥 Лучшие сигналы рынка\n\n"

    for index, item in enumerate(results, start=1):
        fear_greed = item.get("fear_greed", {})
        fg_value = fear_greed.get("value", "Нет данных")
        fg_status = fear_greed.get("status", "Нет данных")

        text += (
            f"{index}. {item['symbol']}\n"
            f"{item['signal']}\n"
            f"Оценка: {item['score']} / 100\n"
            f"Цена: {item['price']:.2f} USDT\n"
            f"Тренд: {item['trend']}\n"
            f"Fear & Greed: {fg_value} | {fg_status}\n\n"
        )

    return text


async def _resume_automatic_trading(application):
    if not auto_state.enabled:
        return
    chat_id = auto_state.settings.get("telegram_chat_id")
    if not chat_id:
        auto_state.turn_off()
        print("⚠️ Автоторговля не возобновлена: chat_id отсутствует")
        return
    notifier.setup(application.bot, int(chat_id))
    if not auto_loop.is_running:
        application.create_task(auto_loop.start())
    print("✅ Автоторговля возобновлена после перезапуска")


def run_telegram_bot(token: str):
    # Network reconciliation belongs to application startup, not module import.
    health = demo_controller.client.health_check()
    print(
        f"✅ Binance {trading_mode.CURRENT_MODE.value}: "
        f"{health['position_mode']}, drift={health['clock_drift_ms']}ms"
    )
    demo_controller.restore_open_trades()

    app = (
        Application.builder()
        .token(token)
        .post_init(_resume_automatic_trading)
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
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("paper_on", paper_on))
    app.add_handler(CommandHandler("paper_off", paper_off))
    app.add_handler(CommandHandler("paper_status", paper_status))
    app.add_handler(CommandHandler("auto_once", auto_once))
    app.add_handler(CommandHandler("auto_on", auto_on))
    app.add_handler(CommandHandler("auto_off", auto_off))
    app.add_handler(CommandHandler("auto_status", auto_status))
    # app.add_handler(CommandHandler("paper_check", paper_check))
    app.add_handler(CommandHandler("multi", multi_tf_analyze))
    app.add_handler(CommandHandler("paper_history", paper_history))
    app.add_handler(CommandHandler("paper_stats", paper_stats))
    app.add_handler(CommandHandler("position", position_status))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Меню$"), open_menu))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(CommandHandler("demo_stats", demo_stats))
    app.add_handler(CommandHandler("trade_history", trade_history))
    app.add_handler(CommandHandler("demo_long", demo_long))
    app.add_handler(CommandHandler("demo_short", demo_short))
    app.add_handler(CommandHandler("trade_long", trade_long))
    app.add_handler(CommandHandler("trade_short", trade_short))
    app.add_handler(CommandHandler("trading_mode", trading_mode_status))
    app.add_handler(CommandHandler("demo_positions", demo_positions))
    app.add_handler(CommandHandler("demo_close", demo_close))
    app.add_handler(CommandHandler("demo_reset", demo_reset))

    print("✅ Telegram bot started")
    app.run_polling()
