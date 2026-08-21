import asyncio
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from database.db import Database
from config.settings import settings
import logging
from services.scanner import MarketScanner
from exchange.binance_client import BinanceClient

router = Router()
logger = logging.getLogger(__name__)
manual_scanner = MarketScanner()
scan_lock = asyncio.Lock()
scan_results = {}
scan_signal_ids = {}
scan_profiles = {}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MAX_SCAN_RESULTS = 5
SCAN_PROFILE_THRESHOLDS = {"safe": 80, "normal": 75, "more": 65}
SCAN_PROFILE_LABELS = {"safe": "Безопасный", "normal": "Обычный", "more": "Больше вариантов"}


def _price(value: float, tick_size: float = None) -> str:
    if tick_size:
        tick = Decimal(str(tick_size)).normalize()
        precision = max(0, -tick.as_tuple().exponent)
        return f"{value:.{precision}f}"
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0")


def _format_signal(signal) -> str:
    reasons = "\n".join(f"• {item.lstrip('✅ ').strip()}" for item in signal.reasons[:4])
    reasons = reasons or "• Подтверждения не указаны"
    warnings = "\n".join(signal.warnings[:2])
    created_at = signal.created_at.replace(tzinfo=timezone.utc)
    valid_until = (created_at + timedelta(
        minutes=settings.signal_validity_minutes
    )).astimezone(MOSCOW_TZ)
    in_entry_zone = signal.entry_zone_min <= signal.current_price <= signal.entry_zone_max
    entry_status = "✅ в зоне" if in_entry_zone else "⛔ вне зоны — пропустить"
    if signal.score >= 80:
        quality = "🟢 Сильный"
    elif signal.score >= 75:
        quality = "🟡 Подтверждённый"
    else:
        quality = "🟠 Только наблюдение"
    text = (
        f"{quality} · {signal.symbol} · {signal.score}/100\n\n"
        f"Вход: ${_price(signal.entry_zone_min, signal.tick_size)}–"
        f"${_price(signal.entry_zone_max, signal.tick_size)}\n"
        f"Сейчас: ${_price(signal.current_price, signal.tick_size)} · {entry_status}\n\n"
        f"TP: ${_price(signal.targets.tp1, signal.tick_size)} · "
        f"${_price(signal.targets.tp2, signal.tick_size)} · "
        f"${_price(signal.targets.tp3, signal.tick_size)} · "
        f"${_price(signal.targets.tp4, signal.tick_size)}\n"
        f"SL: ${_price(signal.stop_loss, signal.tick_size)} · −{signal.stop_loss_percent:.1f}%\n"
        f"R/R до TP2: {signal.risk_reward:.2f}\n\n"
        f"Почему:\n{reasons}\n\n"
        f"⏳ До {valid_until:%H:%M} МСК"
    )
    return text + (f"\n\n{warnings}" if warnings else "")


def _scan_keyboard(signals) -> InlineKeyboardMarkup:
    detail_buttons = [
        InlineKeyboardButton(text=str(index), callback_data=f"scan_detail:{index - 1}")
        for index in range(1, len(signals) + 1)
    ]
    rows = [detail_buttons] if detail_buttons else []
    rows.append([InlineKeyboardButton(text="🔄 Новый скан", callback_data="scan_refresh")])
    rows.append([
        InlineKeyboardButton(text="🛡 80+", callback_data="scan_profile:safe"),
        InlineKeyboardButton(text="⚖️ 75+", callback_data="scan_profile:normal"),
        InlineKeyboardButton(text="🔎 65+", callback_data="scan_profile:more"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_keyboard(index: int, symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔔 Следить", callback_data=f"scan_watch:{index}"),
            InlineKeyboardButton(text="📈 Обновить цену", callback_data=f"scan_price:{symbol}"),
        ],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="scan_back")],
    ])


def _scan_summary(signals) -> str:
    strong = sum(signal.score >= 80 for signal in signals)
    confirmed = sum(75 <= signal.score < 80 for signal in signals)
    watchlist = sum(signal.score < 75 for signal in signals)
    lines = [
        "🔎 Скан завершён",
        f"🟢 Сильных: {strong} · 🟡 Подтверждённых: {confirmed} · "
        f"🟠 Наблюдение: {watchlist}",
        "",
    ]
    for index, signal in enumerate(signals, 1):
        marker = "🟢" if signal.score >= 80 else "🟡" if signal.score >= 75 else "🟠"
        lines.append(f"{index}. {signal.symbol} — {signal.score}/100 {marker}")
    lines.extend(["", "Нажмите номер, чтобы открыть подробности."])
    return "\n".join(lines)


async def _run_scan(message: types.Message):
    if scan_lock.locked():
        await message.answer("⏳ Сканирование уже выполняется. Дождитесь результата.")
        return
    progress_message = await message.answer(
        "⏳ Этап 1/2: анализирую TOP-50 рынка. "
        "Если сигналов не будет, автоматически проверю TOP-100."
    )
    db = Database()
    db.register_user(message.chat.id)
    profile = db.get_scan_profile(message.chat.id)
    if profile not in SCAN_PROFILE_THRESHOLDS:
        profile = "more"
    threshold = SCAN_PROFILE_THRESHOLDS[profile]

    def select_signals(items):
        return [
            item for item in items
            if item["signal_object"].score >= threshold
        ][:MAX_SCAN_RESULTS]

    async def show_progress(done, total, found):
        try:
            await progress_message.edit_text(
                f"⏳ Проверено {done} из {total}\n"
                f"Предварительно найдено: {found}"
            )
        except Exception:
            logger.debug("Не удалось обновить прогресс скана", exc_info=True)

    async with scan_lock:
        try:
            results = await manual_scanner.scan_market(
                top_limit=50, respect_cooldown=False,
                progress_callback=show_progress,
            )
            selected = select_signals(results)
            expanded = False
            if not selected:
                expanded = True
                await message.answer(
                    "🔎 В TOP-50 подходящих сигналов нет. "
                    "Этап 2/2: расширяю поиск до TOP-100…"
                )
                results = await manual_scanner.scan_market(
                    top_limit=100, respect_cooldown=False,
                    progress_callback=show_progress,
                )
                selected = select_signals(results)
        except Exception:
            logger.exception("Ошибка ручного сканирования")
            await message.answer("❌ Не удалось выполнить скан. Проверьте подключение и журнал.")
            return
    if not selected:
        await message.answer(
            f"✅ Проверены TOP-100. Для профиля «{SCAN_PROFILE_LABELS[profile]}» "
            f"(Score {threshold}+) сигналов сейчас нет.",
            reply_markup=_scan_keyboard([]),
        )
        return
    signals = [item["signal_object"] for item in selected]
    scan_results[message.chat.id] = signals
    scan_signal_ids[message.chat.id] = [item["signal_id"] for item in selected]
    summary = (
        f"Режим: {SCAN_PROFILE_LABELS[profile]} · Score {threshold}+\n\n"
        f"Охват: {'TOP-100' if expanded else 'TOP-50'}\n\n"
        f"{_scan_summary(signals)}\n\n"
        f"Фильтры стратегии не прошли: "
        f"{manual_scanner.last_scan_diagnostics.get('strategy_filtered', 0)} "
        f"(просадка, разворот, Score, ликвидность или R/R)."
    )
    await message.answer(summary, reply_markup=_scan_keyboard(signals))


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Основное меню бота."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/signals"), KeyboardButton(text="/top")],
            [KeyboardButton(text="/scan"), KeyboardButton(text="/stats")],
            [KeyboardButton(text="/settings"), KeyboardButton(text="/help")]
        ],
        resize_keyboard=True
    )


@router.message(Command("start"))
async def cmd_start(message: types.Message):
    """Команда /start - описание бота."""
    Database().register_user(message.chat.id)
    text = """🤖 Добро пожаловать в Crypto Signal Bot!

Бот анализирует SPOT-рынок по вашей команде и показывает торговые сигналы.

**Главные возможности:**
- 🎯 Поиск точек входа после снижения цены
- 📊 Анализ технических индикаторов (RSI, MACD, Bollinger Bands)
- 💡 Расчёт целей (TP1-TP4) и Stop-Loss
- 🕹️ Ручной запуск сканирования командой /scan
- ⚙️ Персональные настройки

**Источник данных:**
- Binance Spot Market API (только публичные данные)

**DISCLAIMER:**
Сигналы являются результатом технического анализа и не являются гарантией роста цены или финансовой рекомендацией. Торгуйте на свой риск!

Нажмите /help для списка команд."""

    await message.answer(text, reply_markup=get_main_keyboard())


@router.message(Command("help"))
async def cmd_help(message: types.Message):
    """Команда /help - список команд."""
    text = """📋 Доступные команды:

/signals - Лучшие текущие сигналы
/top - TOP-10 монет по Score
/coin <TICKER> - Подробный анализ пары (напр. /coin BTC)
/scan - Принудительный скан рынка
/history - История сигналов
/stats - Статистика эффективности
/settings - Персональные настройки
/help - Эта справка

**Примеры использования:**
`/coin SOL` - подробный анализ пары SOL/USDT
`/top` - топ-10 лучших сигналов по Score
`/stats` - как часто сигналы достигают целей"""

    await message.answer(text)


@router.message(Command("signals"))
async def cmd_signals(message: types.Message):
    """Команда /signals - текущие сигналы."""
    db = Database()
    signals = db.get_top_signals(limit=10)
    
    if not signals:
        await message.answer("😴 Нет сигналов. Попробуйте позже или выполните /scan")
        return
    
    text = "🟢 **Лучшие текущие сигналы:**\n\n"
    
    for signal in signals[:5]:
        # signal: (id, symbol, score, entry_price, entry_zone_min, entry_zone_max, tp1, tp2, tp3, tp4, stop_loss, stop_loss_percent, support, resistance, rsi_5m, rsi_15m, rsi_1h, volume_change_percent, risk_reward, reasons, warnings, created_at, sent_to_telegram)
        id, symbol, score, entry_price, entry_zone_min, entry_zone_max, tp1, tp2, tp3, tp4, stop_loss, stop_loss_percent, support, resistance, rsi_5m, rsi_15m, rsi_1h, volume_change_percent, risk_reward, reasons, warnings, created_at, sent_to_telegram = signal
        
        text += f"💰 **{symbol}** | Score: {score}/100\n"
        text += f"Цена: ${entry_price:.2f}\n"
        text += f"Зона входа: ${entry_zone_min:.2f} - ${entry_zone_max:.2f}\n"
        text += f"TP1: ${tp1:.2f} | TP2: ${tp2:.2f} | TP3: ${tp3:.2f}\n"
        text += f"🛑 Stop: ${stop_loss:.2f} (-{stop_loss_percent:.1f}%)\n"
        text += f"📊 R/R: {risk_reward:.2f}\n\n"
    
    await message.answer(text)


@router.message(Command("top"))
async def cmd_top(message: types.Message):
    """Команда /top - TOP-10 сигналов."""
    db = Database()
    signals = db.get_top_signals(limit=10)
    
    if not signals:
        await message.answer("😴 Нет сигналов. Попробуйте выполнить /scan")
        return
    
    text = "🏆 **TOP-10 сигналов по Score:**\n\n"
    
    for idx, signal in enumerate(signals, 1):
        id, symbol, score, entry_price, *_ = signal
        text += f"{idx}. {symbol} - Score: {score}/100\n"
    
    await message.answer(text)


@router.message(Command("scan"))
async def cmd_scan(message: types.Message):
    """Команда /scan - принудительный скан."""
    await _run_scan(message)


@router.callback_query(F.data.startswith("scan_detail:"))
async def scan_detail(query: types.CallbackQuery):
    await query.answer()
    signals = scan_results.get(query.message.chat.id, [])
    try:
        index = int(query.data.split(":", 1)[1])
        signal = signals[index]
    except (ValueError, IndexError):
        await query.message.answer("Результаты устарели. Запустите новый скан.")
        return
    await query.message.answer(
        _format_signal(signal), reply_markup=_detail_keyboard(index, signal.symbol)
    )


@router.callback_query(F.data == "scan_refresh")
async def scan_refresh(query: types.CallbackQuery):
    await query.answer("Запускаю новый скан")
    await _run_scan(query.message)


@router.callback_query(F.data.startswith("scan_profile:"))
async def scan_profile(query: types.CallbackQuery):
    profile = query.data.split(":", 1)[1]
    if profile not in SCAN_PROFILE_THRESHOLDS:
        await query.answer("Неизвестный профиль", show_alert=True)
        return
    Database().set_scan_profile(query.message.chat.id, profile)
    await query.answer(f"Профиль: {SCAN_PROFILE_LABELS[profile]}")
    await _run_scan(query.message)


@router.callback_query(F.data.startswith("scan_watch:"))
async def scan_watch(query: types.CallbackQuery):
    try:
        index = int(query.data.split(":", 1)[1])
        signal_id = scan_signal_ids[query.message.chat.id][index]
    except (KeyError, ValueError, IndexError):
        await query.answer("Результаты устарели", show_alert=True)
        return
    saved = Database().watch_signal(
        query.message.chat.id, signal_id, settings.signal_validity_minutes
    )
    await query.answer(
        "Наблюдение включено" if saved else "Сигнал не найден",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("scan_price:"))
async def scan_price(query: types.CallbackQuery):
    symbol = query.data.split(":", 1)[1]
    await query.answer("Обновляю цену")
    async with BinanceClient() as client:
        ticker = await client.get_ticker(symbol)
    if not ticker:
        await query.message.answer(f"❌ Не удалось обновить цену {symbol}.")
        return
    await query.message.answer(f"📈 {symbol}: текущая цена ${ticker['price']:g}")


@router.callback_query(F.data == "scan_back")
async def scan_back(query: types.CallbackQuery):
    await query.answer()
    signals = scan_results.get(query.message.chat.id, [])
    if not signals:
        await query.message.answer("Результаты устарели. Запустите /scan.")
        return
    await query.message.answer(_scan_summary(signals), reply_markup=_scan_keyboard(signals))


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Команда /stats - статистика."""
    db = Database()
    
    stats = db.get_signal_statistics()
    text = f"""📊 Статистика сигналов:

Всего создано: {stats['signals']}
Отслежено: {stats['tracked']}
Достигли TP1 (+3%): {stats['tp1']}
Достигли TP2 (+5%): {stats['tp2']}
Достигли TP3 (+8%): {stats['tp3']}
Достигли TP4 (+15%): {stats['tp4']}
Попали на Stop: {stats['stops']}

Если «Отслежено» равно нулю, модуль контроля результатов ещё не накопил данные."""

    await message.answer(text)


@router.message(Command("coin"))
async def cmd_coin(message: types.Message):
    """Команда /coin <TICKER> - подробный анализ пары."""
    args = message.text.split()
    
    if len(args) < 2:
        await message.answer("📌 Использование: /coin <TICKER>\nПримеры: /coin BTC, /coin ETH, /coin SOL")
        return
    
    ticker = args[1].upper()
    symbol = f"{ticker}USDT"
    
    await message.answer(f"📊 Анализ {symbol}...")
    try:
        signal = await manual_scanner.analyze_symbol(symbol)
    except Exception:
        logger.exception("Ошибка анализа %s", symbol)
        await message.answer("❌ Binance не вернул данные для этой пары.")
        return
    if signal is None:
        await message.answer(
            f"🟡 {symbol}: подтверждённого входа сейчас нет. Просадка, "
            "ликвидность, разворот или R/R не прошли фильтры."
        )
        return
    await message.answer(_format_signal(signal))


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    """Команда /settings - настройки пользователя."""
    text = """⚙️ **Персональные настройки:**

Минимальный Score: 75 (по умолчанию)
Цели фиксации: 3% / 5% / 8% / 15%
Режим сканирования: ручной (/scan)

Для изменения настроек напишите:
`/set_score 70` - изменить минимальный Score"""

    await message.answer(text)


@router.message(Command("history"))
async def cmd_history(message: types.Message):
    """Команда /history - история сигналов."""
    db = Database()
    signals = db.get_signals(limit=20)
    
    text = "📜 **История последних 20 сигналов:**\n\n"
    
    if not signals:
        await message.answer("История пуста")
        return
    
    for signal in signals:
        id, symbol, score, entry_price, *_, created_at = signal
        text += f"• {symbol} | Score: {score} | Цена: ${entry_price:.2f} | {created_at[:10]}\n"
    
    await message.answer(text)
