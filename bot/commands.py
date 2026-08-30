import asyncio
import json
import tempfile
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton,
    InlineKeyboardMarkup, BufferedInputFile,
)
from database.db import Database
from config.settings import settings
import logging
from services.scanner import MarketScanner
from exchange.binance_client import BinanceClient
from services.trade_export_service import build_trades_xlsx
from services.diagnostic_export_service import build_diagnostic_json
from services.screenshot_ocr_service import (
    find_tesseract, recognize_binance_screenshot,
)

router = Router()
logger = logging.getLogger(__name__)
manual_scanner = MarketScanner()
pump_service = None
scan_lock = asyncio.Lock()
scan_results = {}
scan_signal_ids = {}
scan_profiles = {}
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MAX_SCAN_RESULTS = 5
SCAN_PROFILE_THRESHOLDS = {"safe": 80, "normal": 75, "more": 65}
SCAN_PROFILE_LABELS = {"safe": "Безопасный", "normal": "Обычный", "more": "Больше вариантов"}


class TradeSetup(StatesGroup):
    choosing_price = State()
    waiting_price = State()
    choosing_amount = State()
    waiting_amount = State()
    waiting_screenshot = State()
    confirming = State()


def configure_pump_service(service):
    global pump_service
    pump_service = service


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
    risk_note = (
        "⚠️ Ранний вход: повышенный риск, используйте меньший объём.\n"
        if signal.score < 75 else ""
    )
    trade_levels = (
        f"{risk_note}"
        f"Цель: ${_price(signal.targets.tp1, signal.tick_size)} (+3%)\n"
        "Закрытие позиции подтверждается вручную."
    )
    text = (
        f"{quality} · {signal.symbol} · {signal.score}/100\n\n"
        f"Вход: ${_price(signal.entry_zone_min, signal.tick_size)}–"
        f"${_price(signal.entry_zone_max, signal.tick_size)}\n"
        f"Сейчас: ${_price(signal.current_price, signal.tick_size)} · {entry_status}\n\n"
        f"{trade_levels}\n\n"
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


def _detail_keyboard(index: int, symbol: str, score: int) -> InlineKeyboardMarkup:
    first_row = [
        InlineKeyboardButton(text="✅ Я вошёл", callback_data=f"scan_take:{index}"),
        InlineKeyboardButton(text="📈 Обновить цену", callback_data=f"scan_price:{symbol}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        first_row,
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
        f"(ликвидность, спред, объём, тренд, памп, сопротивление или Score)."
    )
    await message.answer(summary, reply_markup=_scan_keyboard(signals))


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Основное меню бота."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/scan"), KeyboardButton(text="/trades")],
            [KeyboardButton(text="/export"), KeyboardButton(text="/diagnostics")],
            [KeyboardButton(text="/stats")],
            [KeyboardButton(text="/pump")],
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
- 💡 Расчёт единственной цели +3%
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
/trades - Мои открытые и закрытые сделки
/export - Скачать журнал сделок Excel
/diagnostics - Скачать отчёт для улучшения стратегии
/pump - Экспериментальный поиск возможных импульсов
/history - История ваших сделок
/edit_trade ID ЦЕНА СУММА - Уточнить вход и сумму USDT
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
        text += f"Цель +3%: ${tp1:.2f}\n"
        text += "Закрытие подтверждается вручную.\n\n"
    
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
        _format_signal(signal),
        reply_markup=_detail_keyboard(index, signal.symbol, signal.score),
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
        signal = scan_results[query.message.chat.id][index]
        signal_id = scan_signal_ids[query.message.chat.id][index]
    except (KeyError, ValueError, IndexError):
        await query.answer("Результаты устарели", show_alert=True)
        return
    saved = Database().watch_signal(
        query.message.chat.id, signal_id, settings.signal_validity_minutes
    )


async def _begin_trade_setup(query: types.CallbackQuery, state: FSMContext,
                             signal_id: int, symbol: str):
    async with BinanceClient() as client:
        ticker = await client.get_ticker(symbol)
    if not ticker:
        await query.answer("Не удалось получить цену Binance", show_alert=True)
        return
    suggested_price = float(ticker["price"])
    await state.clear()
    await state.update_data(
        signal_id=signal_id, symbol=symbol, entry_price=suggested_price
    )
    await state.set_state(TradeSetup.choosing_price)
    await query.answer()
    await query.message.answer(
        f"✅ Вы выбрали {symbol}\n\nКакая цена входа?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📈 Взять текущую ${_price(suggested_price)}",
                callback_data="setup_price_market",
            )],
            [InlineKeyboardButton(
                text="✍️ Ввести цену вручную",
                callback_data="setup_price_manual",
            )],
            [InlineKeyboardButton(
                text="📷 Заполнить по скриншоту",
                callback_data="setup_screenshot",
            )],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="setup_cancel")],
        ]),
    )


async def _show_amount_keyboard(target):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="$10", callback_data="setup_amount:10"),
         InlineKeyboardButton(text="$20", callback_data="setup_amount:20"),
         InlineKeyboardButton(text="$30", callback_data="setup_amount:30")],
        [InlineKeyboardButton(text="$40", callback_data="setup_amount:40"),
         InlineKeyboardButton(text="$50", callback_data="setup_amount:50")],
        [InlineKeyboardButton(text="✍️ Другая сумма", callback_data="setup_amount_manual")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="setup_cancel")],
    ])
    await target.answer("Выберите сумму сделки в USDT:", reply_markup=keyboard)


async def _show_trade_confirmation(target, state: FSMContext):
    data = await state.get_data()
    price = float(data["entry_price"])
    amount = float(data["position_usdt"])
    quantity = float(data.get("quantity") or amount / price)
    opened = data.get("opened_at") or "время подтверждения"
    await state.set_state(TradeSetup.confirming)
    await target.answer(
        "Проверьте сделку:\n\n"
        f"Монета: {data['symbol']}\n"
        f"Цена входа: ${_price(price)}\n"
        f"Сумма: ${amount:g}\n"
        f"Количество: {quantity:g}\n"
        f"Открыта: {opened}\n"
        f"Цель +3%: ${_price(price * 1.03)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Всё правильно", callback_data="setup_confirm")],
            [InlineKeyboardButton(text="⬅️ Изменить", callback_data="setup_restart")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="setup_cancel")],
        ]),
    )


@router.callback_query(F.data.startswith("scan_take:"))
async def scan_take(query: types.CallbackQuery, state: FSMContext):
    try:
        index = int(query.data.split(":", 1)[1])
        signal = scan_results[query.message.chat.id][index]
        signal_id = scan_signal_ids[query.message.chat.id][index]
    except (KeyError, ValueError, IndexError):
        await query.answer("Результаты устарели. Запустите /scan", show_alert=True)
        return
    await _begin_trade_setup(query, state, signal_id, signal.symbol)


@router.callback_query(F.data.startswith("auto_watch:"))
async def auto_watch(query: types.CallbackQuery):
    try:
        signal_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный сигнал", show_alert=True)
        return
    saved = Database().watch_signal(
        query.message.chat.id, signal_id, settings.signal_validity_minutes
    )
    await query.answer(
        "Слежение включено: сообщу о входе и цели +3%"
        if saved else "Сигнал уже недоступен",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("auto_take:"))
async def auto_take(query: types.CallbackQuery, state: FSMContext):
    try:
        signal_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректный сигнал", show_alert=True)
        return
    with Database().connect() as conn:
        row = conn.execute(
            "SELECT symbol FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
    if not row:
        await query.answer("Сигнал не найден", show_alert=True)
        return
    await _begin_trade_setup(query, state, signal_id, row[0])


@router.callback_query(TradeSetup.choosing_price, F.data == "setup_price_market")
async def setup_price_market(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(TradeSetup.choosing_amount)
    await _show_amount_keyboard(query.message)


@router.callback_query(TradeSetup.choosing_price, F.data == "setup_price_manual")
async def setup_price_manual(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(TradeSetup.waiting_price)
    await query.message.answer("Введите фактическую цену покупки, например: 128.72")


@router.callback_query(TradeSetup.choosing_price, F.data == "setup_screenshot")
async def setup_screenshot(query: types.CallbackQuery, state: FSMContext):
    if not find_tesseract():
        await query.answer(
            "OCR не установлен на устройстве, где запущен бот",
            show_alert=True,
        )
        return
    await query.answer()
    await state.set_state(TradeSetup.waiting_screenshot)
    await query.message.answer(
        "Отправьте скриншот позиции или истории покупки Binance как фотографию."
    )


@router.message(TradeSetup.waiting_screenshot, F.photo)
async def setup_receive_screenshot(message: types.Message, state: FSMContext):
    temporary = Path(tempfile.gettempdir()) / f"tradingai_{message.chat.id}.jpg"
    try:
        await message.bot.download(message.photo[-1], destination=temporary)
        parsed = await recognize_binance_screenshot(temporary)
    except Exception:
        logger.exception("Не удалось распознать скриншот Binance")
        await message.answer(
            "Не удалось распознать данные. Выберите ручной ввод цены.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="✍️ Ввести вручную", callback_data="setup_price_manual"
                )
            ]]),
        )
        await state.set_state(TradeSetup.choosing_price)
        return
    finally:
        temporary.unlink(missing_ok=True)
    if not parsed.entry_price or not parsed.quantity:
        await message.answer(
            "На скриншоте не удалось уверенно найти цену и количество. "
            "Используйте ручной ввод."
        )
        await state.set_state(TradeSetup.choosing_price)
        return
    amount = parsed.entry_price * parsed.quantity
    await state.update_data(
        entry_price=parsed.entry_price, quantity=parsed.quantity,
        position_usdt=amount, opened_at=parsed.opened_at,
    )
    await _show_trade_confirmation(message, state)


@router.message(TradeSetup.waiting_screenshot)
async def setup_screenshot_requires_photo(message: types.Message):
    await message.answer("Нужно отправить изображение или скриншот как фотографию.")


@router.message(TradeSetup.waiting_price)
async def setup_receive_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").strip())
        if price <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введите положительное число, например: 128.72")
        return
    await state.update_data(entry_price=price)
    await state.set_state(TradeSetup.choosing_amount)
    await _show_amount_keyboard(message)


@router.callback_query(TradeSetup.choosing_amount, F.data.startswith("setup_amount:"))
async def setup_amount_fixed(query: types.CallbackQuery, state: FSMContext):
    amount = float(query.data.rsplit(":", 1)[1])
    await state.update_data(position_usdt=amount)
    await query.answer()
    await _show_trade_confirmation(query.message, state)


@router.callback_query(TradeSetup.choosing_amount, F.data == "setup_amount_manual")
async def setup_amount_manual(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    await state.set_state(TradeSetup.waiting_amount)
    await query.message.answer("Введите сумму сделки в USDT, например: 15")


@router.message(TradeSetup.waiting_amount)
async def setup_receive_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").strip())
        if amount <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введите положительную сумму, например: 15")
        return
    await state.update_data(position_usdt=amount)
    await _show_trade_confirmation(message, state)


@router.callback_query(TradeSetup.confirming, F.data == "setup_confirm")
async def setup_confirm(query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    trade_id = Database().open_manual_trade(
        query.message.chat.id, data["signal_id"], data["entry_price"]
    )
    if trade_id is None:
        await query.answer("Эта монета уже есть в открытых сделках", show_alert=True)
        await state.clear()
        return
    if trade_id is False:
        await query.answer("Сигнал больше недоступен", show_alert=True)
        await state.clear()
        return
    Database().edit_manual_trade(
        trade_id, query.message.chat.id, data["entry_price"], data["position_usdt"]
    )
    if data.get("opened_at"):
        Database().update_manual_trade(trade_id, opened_at=data["opened_at"])
    await query.answer("Сделка сохранена", show_alert=True)
    await state.clear()
    await query.message.answer(
        f"✅ Сделка #{trade_id} сохранена. Контроль цели +3% включён."
    )


@router.callback_query(F.data == "setup_restart")
async def setup_restart(query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await _begin_trade_setup(
        query, state, data["signal_id"], data["symbol"]
    )


@router.callback_query(F.data == "setup_cancel")
async def setup_cancel(query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await query.answer("Добавление сделки отменено")


def _trade_keyboard(trade_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔴 Я закрыл сделку", callback_data=f"trade_close:{trade_id}"
        )
    ]])


@router.message(Command("trades"))
async def cmd_trades(message: types.Message):
    trades = Database().get_manual_trades(message.chat.id)
    if not trades:
        await message.answer("Сделок пока нет. Запустите /scan и нажмите «Я вошёл».")
        return
    open_trades = [trade for trade in trades if trade["status"] == "open"]
    pending = [trade for trade in trades if trade["status"] == "pending_close"]
    closed = [trade for trade in trades if trade["status"] == "closed"]
    statistics = Database().get_manual_trade_statistics(message.chat.id)
    critical = []
    profitable = []
    for trade in open_trades:
        current = trade.get("current_price") or trade["entry_price"]
        pnl = (current / trade["entry_price"] - 1) * 100
        if pnl <= -2:
            critical.append(trade)
        elif pnl > 0:
            profitable.append(trade)
    await message.answer(
        "📊 СОСТОЯНИЕ СДЕЛОК\n\n"
        f"🟢 В процессе: {len(open_trades)}\n"
        f"📈 Сейчас в плюсе: {len(profitable)}\n"
        f"🚨 Критическая зона: {len(critical)}\n"
        f"⏳ Ждут подтверждения: {len(pending)}\n"
        f"✅ Закрыто: {len(closed)}\n"
        f"🏆 Закрыто в плюс: {statistics['wins']}"
    )
    for trade in open_trades:
        current = trade.get("current_price") or trade["entry_price"]
        change = (current / trade["entry_price"] - 1) * 100
        marker = "🚨 КРИТИЧНО" if trade in critical else "🟢 В процессе"
        await message.answer(
            f"{marker} · #{trade['id']} {trade['symbol']}\n"
            f"Вход: ${_price(trade['entry_price'])}\n"
            f"Сейчас: ${_price(current)} ({change:+.2f}%)\n"
            f"Максимум: ${_price(trade['max_price'])}\n"
            f"Цель +3%: ${_price(trade['tp1'])}\n"
            f"Открыта: {trade['opened_at']} UTC",
            reply_markup=_trade_keyboard(trade["id"]),
        )
    for trade in pending:
        await message.answer(
            f"⏳ #{trade['id']} {trade['symbol']} · требуется подтверждение\n"
            f"Обнаружено: {trade.get('pending_reason')}\n"
            f"Уровень: ${_price(trade.get('pending_price') or 0)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Подтверждаю закрытие",
                    callback_data=f"trade_confirm_close:{trade['id']}",
                )],
                [InlineKeyboardButton(
                    text="⏳ Сделка ещё открыта",
                    callback_data=f"trade_keep_open:{trade['id']}",
                )],
            ]),
        )
    if closed:
        lines = ["Последние закрытые:"]
        for trade in closed[:10]:
            result = (
                (trade["close_price"] / trade["entry_price"] - 1) * 100
                if trade["close_price"] else 0
            )
            lines.append(
                f"#{trade['id']} {trade['symbol']} · {result:+.2f}% · "
                f"{trade.get('close_reason') or 'закрыта'}"
            )
        await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("trade_close:"))
async def trade_close(query: types.CallbackQuery):
    try:
        trade_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная сделка", show_alert=True)
        return
    trades = Database().get_manual_trades(query.message.chat.id, status="open")
    trade = next((item for item in trades if item["id"] == trade_id), None)
    if not trade:
        await query.answer("Сделка уже закрыта", show_alert=True)
        return
    await query.answer()
    await query.message.answer(
        f"Вы действительно закрыли {trade['symbol']} в Binance?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Да, закрыта", callback_data=f"trade_manual_confirm:{trade_id}"
            ),
            InlineKeyboardButton(text="Отмена", callback_data="trade_close_cancel"),
        ]]),
    )


@router.callback_query(F.data.startswith("trade_manual_confirm:"))
async def trade_manual_confirm(query: types.CallbackQuery):
    trade_id = int(query.data.rsplit(":", 1)[1])
    trades = Database().get_manual_trades(query.message.chat.id, status="open")
    trade = next((item for item in trades if item["id"] == trade_id), None)
    if not trade:
        await query.answer("Сделка уже закрыта или ожидает подтверждения", show_alert=True)
        return
    async with BinanceClient() as client:
        ticker = await client.get_ticker(trade["symbol"])
    if not ticker:
        await query.answer("Не удалось получить цену Binance", show_alert=True)
        return
    price = float(ticker["price"])
    Database().close_manual_trade(trade_id, query.message.chat.id, price)
    result = (price / trade["entry_price"] - 1) * 100
    await query.answer("Закрытие сохранено", show_alert=True)
    await query.message.answer(
        f"🔴 {trade['symbol']} закрыта\nЦена: ${_price(price)}\n"
        f"Результат: {result:+.2f}%"
    )


@router.callback_query(F.data == "trade_close_cancel")
async def trade_close_cancel(query: types.CallbackQuery):
    await query.answer("Закрытие отменено")


@router.callback_query(F.data.startswith("trade_confirm_close:"))
async def trade_confirm_close(query: types.CallbackQuery):
    trade_id = int(query.data.rsplit(":", 1)[1])
    saved = Database().confirm_pending_trade(trade_id, query.message.chat.id)
    await query.answer(
        "Закрытие подтверждено" if saved else "Подтверждение уже обработано",
        show_alert=True,
    )
    if saved:
        await query.message.answer(f"✅ Сделка #{trade_id} перенесена в историю.")


@router.callback_query(F.data.startswith("trade_keep_open:"))
async def trade_keep_open(query: types.CallbackQuery):
    trade_id = int(query.data.rsplit(":", 1)[1])
    saved = Database().keep_pending_trade_open(
        trade_id, query.message.chat.id
    )
    await query.answer(
        "Продолжаю наблюдение" if saved else "Подтверждение уже обработано",
        show_alert=True,
    )


@router.message(Command("edit_trade"))
async def cmd_edit_trade(message: types.Message):
    parts = message.text.replace(",", ".").split()
    if len(parts) not in {3, 4}:
        await message.answer(
            "Использование: /edit_trade ID ЦЕНА [СУММА_USDT]\n"
            "Пример: /edit_trade 7 0.001501 10"
        )
        return
    try:
        trade_id = int(parts[1])
        entry_price = float(parts[2])
        position_usdt = float(parts[3]) if len(parts) == 4 else None
    except ValueError:
        await message.answer("ID, цена и сумма должны быть числами.")
        return
    saved = Database().edit_manual_trade(
        trade_id, message.chat.id, entry_price, position_usdt
    )
    if not saved:
        await message.answer("Открытая сделка не найдена или значения некорректны.")
        return
    amount = f" · сумма ${position_usdt:g}" if position_usdt else ""
    await message.answer(
        f"✅ Сделка #{trade_id} обновлена\nВход: ${_price(entry_price)}{amount}\n"
        f"Цель +3%: ${_price(entry_price * 1.03)}"
    )


@router.message(Command("export"))
async def cmd_export(message: types.Message):
    trades = Database().get_manual_trades(message.chat.id)
    if not trades:
        await message.answer("Журнал сделок пока пуст.")
        return
    content = build_trades_xlsx(trades)
    filename = f"TradingAI_trades_{datetime.now(MOSCOW_TZ):%Y-%m-%d}.xlsx"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption="📊 Ваш журнал сделок TradingAI",
    )


@router.message(Command("diagnostics"))
async def cmd_diagnostics(message: types.Message):
    trades = Database().get_manual_trades(message.chat.id)
    content = build_diagnostic_json(manual_scanner.last_scan_diagnostics, trades)
    filename = f"TradingAI_diagnostics_{datetime.now(MOSCOW_TZ):%Y-%m-%d_%H-%M}.json"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption=(
            "🧪 Диагностический отчёт готов. Пришлите этот файл мне — "
            "по нему можно улучшать фильтры и Score. Секретные ключи в файл не входят."
        ),
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
    stats = Database().get_manual_trade_statistics(message.chat.id)
    text = f"""📊 Статистика ваших сделок:

Всего выбрано: {stats['total']}
🟢 В процессе: {stats['open']}
🚨 Критическая зона: {stats['critical']}
⏳ Ждут подтверждения: {stats['pending']}
✅ Закрыто: {stats['closed']}
🏆 В плюс: {stats['wins']}
📉 В минус: {stats['losses']}
🎯 Закрыто по цели +3%: {stats['targets']}
✋ Закрыто вручную: {stats['manual']}

Средний результат закрытых: {stats['average_result']:+.2f}%"""

    await message.answer(text)


@router.message(Command("coin"))
async def cmd_coin(message: types.Message):
    """Команда /coin <TICKER> - подробный анализ пары."""
    args = message.text.split()
    
    if len(args) < 2:
        await message.answer("📌 Использование: /coin <TICKER>\nПримеры: /coin BTC, /coin ETH, /coin SOL")
        return
    
    ticker = args[1].upper().replace("/", "")
    symbol = ticker if ticker.endswith("USDT") else f"{ticker}USDT"
    
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

Профили сканирования:
🛡 Безопасный: Score 80+
⚖️ Обычный: Score 75+
🔎 Больше вариантов: Score 65+

Единственная цель: +3%
Критическая зона: текущий результат сделки −2% или ниже.

Профиль выбирается кнопками после команды /scan."""

    await message.answer(text)


@router.message(Command("history"))
async def cmd_history(message: types.Message):
    """Команда /history - история реальных выбранных сделок."""
    trades = Database().get_manual_trades(message.chat.id)
    closed = [trade for trade in trades if trade["status"] == "closed"][:20]
    if not closed:
        await message.answer("История закрытых сделок пока пуста.")
        return
    lines = ["📜 История последних сделок:", ""]
    for trade in closed:
        result = (trade["close_price"] / trade["entry_price"] - 1) * 100
        pnl = (
            f" · {trade['position_usdt'] * result / 100:+.2f} USDT"
            if trade.get("position_usdt") else ""
        )
        lines.append(
            f"#{trade['id']} {trade['symbol']} · {result:+.2f}%{pnl} · "
            f"{trade.get('close_reason') or 'вручную'}"
        )
    text = "\n".join(lines)
    await message.answer(text)


def _pump_keyboard(user_id: int):
    enabled = Database().get_pump_background(user_id)
    toggle = (
        InlineKeyboardButton(text="⏹ Выключить фон", callback_data="pump_bg_off")
        if enabled else
        InlineKeyboardButton(text="▶️ Включить фон", callback_data="pump_bg_on")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Один полный скан", callback_data="pump_scan")],
        [toggle],
        [InlineKeyboardButton(text="📊 Результаты", callback_data="pump_results"),
         InlineKeyboardButton(text="📈 Статистика", callback_data="pump_stats")],
        [InlineKeyboardButton(text="📥 Скачать данные", callback_data="pump_export")],
    ])


@router.message(Command("pump"))
async def cmd_pump(message: types.Message):
    Database().register_user(message.chat.id)
    enabled = Database().get_pump_background(message.chat.id)
    await message.answer(
        "🧪 Экспериментальный Pump-анализ\n\n"
        "Проверяет все доступные Binance Spot USDT-пары без TOP-ограничения. "
        "Ничего не покупает и не добавляет в сделки.\n"
        f"Фоновый режим: {'включён' if enabled else 'выключен'}",
        reply_markup=_pump_keyboard(message.chat.id),
    )


@router.callback_query(F.data == "pump_scan")
async def pump_scan_once(query: types.CallbackQuery):
    await query.answer()
    if pump_service is None:
        await query.message.answer("Pump-сервис ещё не запущен.")
        return
    if pump_service.lock.locked():
        await query.message.answer("⏳ Pump-скан уже выполняется. Дождитесь результата.")
        return
    progress_message = await query.message.answer("⏳ Получаю полный список USDT-пар…")

    async def progress(done, total, found):
        try:
            await progress_message.edit_text(
                f"⏳ Pump-анализ: {done}/{total}\nКандидатов: {found}"
            )
        except Exception:
            pass

    try:
        candidates, saved = await pump_service.scan_for_user(
            query.message.chat.id, progress
        )
    except Exception:
        logger.exception("Manual pump scan failed")
        await query.message.answer("❌ Pump-скан не завершён. Попробуйте позже.")
        return
    diagnostics = pump_service.scanner.last_diagnostics
    await query.message.answer(
        f"✅ Проверено пар: {diagnostics.get('checked', 0)}\n"
        f"Найдено кандидатов: {len(candidates)}\n"
        f"Добавлено в наблюдение: {len(saved)}"
    )
    if not candidates:
        return
    for prediction_id, candidate in saved[:5]:
        await query.message.answer(
            pump_service.format_candidate(candidate, prediction_id)
        )


@router.callback_query(F.data.in_({"pump_bg_on", "pump_bg_off"}))
async def pump_background_toggle(query: types.CallbackQuery):
    enabled = query.data == "pump_bg_on"
    Database().set_pump_background(query.message.chat.id, enabled)
    await query.answer("Фоновый Pump-поиск включён" if enabled else "Фоновый Pump-поиск выключен")
    await query.message.answer(
        f"{'▶️' if enabled else '⏹'} Фоновый Pump-поиск "
        f"{'включён' if enabled else 'выключен'}.",
        reply_markup=_pump_keyboard(query.message.chat.id),
    )


def _pump_results_text(user_id: int):
    rows = Database().get_pump_predictions(user_id=user_id, limit=20)
    if not rows:
        return "Pump-прогнозов пока нет."
    lines = ["🧪 Последние Pump-прогнозы:", ""]
    for row in rows:
        gain = (row["max_price"] / row["start_price"] - 1) * 100
        drawdown = (row["min_price"] / row["start_price"] - 1) * 100
        state = "⏳" if row["status"] == "observing" else "✅" if row["outcome"] == "pump" else "❌"
        lines.append(
            f"{state} #{row['id']} {row['symbol']} · Score {row['score']} · "
            f"макс {gain:+.2f}% · мин {drawdown:+.2f}%"
        )
    return "\n".join(lines)


@router.callback_query(F.data == "pump_results")
async def pump_results(query: types.CallbackQuery):
    await query.answer()
    await query.message.answer(_pump_results_text(query.message.chat.id))


@router.callback_query(F.data == "pump_stats")
async def pump_stats(query: types.CallbackQuery):
    await query.answer()
    stats = Database().get_pump_statistics(query.message.chat.id)
    await query.message.answer(
        "📈 Pump-статистика\n\n"
        f"Всего прогнозов: {stats['total']}\nНаблюдаются: {stats['observing']}\n"
        f"Завершены: {stats['completed']}\nПамп состоялся: {stats['successful']}\n"
        f"Точность: {stats['accuracy']:.1f}%"
    )


@router.callback_query(F.data == "pump_export")
async def pump_export(query: types.CallbackQuery):
    await query.answer()
    rows = Database().get_pump_predictions(user_id=query.message.chat.id, limit=10000)
    safe = []
    for row in rows:
        item = {key: value for key, value in row.items() if key != "user_id"}
        for key in ("technical_json", "checkpoints_json"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                pass
        safe.append(item)
    payload = json.dumps(
        {"version": 1, "predictions": safe}, ensure_ascii=False, indent=2,
        default=str,
    ).encode("utf-8")
    await query.message.answer_document(
        BufferedInputFile(
            payload,
            filename=f"TradingAI_pump_{datetime.now(MOSCOW_TZ):%Y-%m-%d_%H-%M}.json",
        ),
        caption="Экспериментальные Pump-прогнозы без персональных данных.",
    )
