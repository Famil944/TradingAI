"""Owner-only Telegram menu and callbacks."""

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config.settings import ROOT, settings
from services.otc_cycles import format_cycles, read_stats
from services.otc_filter_report import format_report as format_filter_report
from services.otc_filter_report import report as filter_report
from services.otc_evaluation import compact_stats
from services.otc_market import TIMEFRAMES
from services.otc_signal_report import read_journal, report as signal_quality_report

router = Router()
service = None
choices = {}
STATS_EPOCH = ROOT / "data" / "signal-stats-epoch.txt"
SOURCES = (("Основной", "main_ready"), ("Groq", "groq_preview"), ("XGBoost", "ml_preview"))


def stats_epoch():
    try:
        return float(STATS_EPOCH.read_text(encoding="ascii"))
    except FileNotFoundError:
        return 0.0


class OwnerOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, types.CallbackQuery) else event
        user = event.from_user
        permitted = (
            isinstance(message, types.Message)
            and user is not None
            and message.chat.type == "private"
            and allowed(message.chat.id)
            and user.id == settings.admin_chat_id
        )
        if not permitted:
            if isinstance(event, types.CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, types.Message):
                await event.answer(
                    "Нет доступа. Укажите ID владельца в OTC_ADMIN_CHAT_ID локального .env."
                )
            return
        return await handler(event, data)


router.message.outer_middleware(OwnerOnly())
router.callback_query.outer_middleware(OwnerOnly())


def allowed(chat_id):
    return settings.admin_chat_id > 0 and chat_id == settings.admin_chat_id


def menu(chat_id, expanded=False):
    chosen = choices.get(chat_id, TIMEFRAMES)
    duration = (
        getattr(service, "session_minutes", None)
        if service and service.running()
        else None
    )
    buttons = []
    for key, label, value in [
        ("all", "Все M1/M5/M15", TIMEFRAMES),
        ("1", "M1", (60,)),
        ("5", "M5", (300,)),
        ("15", "M15", (900,)),
    ]:
        buttons.append(
            InlineKeyboardButton(
                text=("✅ " if chosen == value else "") + label,
                callback_data=f"otc:tf:{key}",
            )
        )
    rows = [buttons]
    for pair in [
        [(30, "30 минут"), (60, "1 час")],
        [(120, "2 часа"), (240, "4 часа")],
        [(480, "8 часов"), (0, "До остановки")],
    ]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if duration == value else "") + label,
                    callback_data=f"otc:start:{value}",
                )
                for value, label in pair
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Остановить", callback_data="otc:stop"),
            InlineKeyboardButton(
                text="▴ Скрыть ботов" if expanded else "▾ Боты",
                callback_data="otc:panel:hide" if expanded else "otc:panel:show",
            ),
        ]
    )
    if settings.mini_app_enabled and settings.mini_app_url:
        rows.append([
            InlineKeyboardButton(
                text="📊 Аналитика Mini App",
                web_app=WebAppInfo(url=settings.mini_app_url),
            )
        ])
    if expanded:
        rows.append([InlineKeyboardButton(text="Обновить", callback_data="otc:panel:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def strategy_status(current):
    streaming = bool(current and current.running() and current.phase == "поток активен")
    boost = getattr(current, "ml_preview", None)
    primary = getattr(current, "main_ready", None)
    groq = getattr(current, "groq_preview", None)
    groq_health = groq.client.health() if groq else (False, "не запущен")
    boost_ready = bool(boost and boost.enabled and boost.runtime.preview_ready())

    since, now = stats_epoch(), time.time()
    lines = []
    for label, attr in SOURCES:
        preview = getattr(current, attr, None)
        count = len(read_journal(preview.journal, since, now)) if preview else 0
        icon, detail = "🔴", ""
        if label == "Основной":
            if streaming:
                icon = "🟢"
                reasons = getattr(primary, "last_reasons", {}) if primary else {}
                if reasons:
                    reason, amount = max(reasons.items(), key=lambda item: item[1])
                    labels = {
                        "asset_or_quote_ineligible": "ждёт свежие данные/выплату",
                        "insufficient_indicator_history": "накапливает историю",
                        "insufficient_directional_confirmations": "мало подтверждений",
                        "pair_loss_cooldown": "пара на паузе после минуса",
                    }
                    detail = f" · {labels.get(reason, reason)} ({amount})"
            elif current and current.running():
                icon, detail = "🟠", f" · {current.phase}"
        elif label == "XGBoost":
            if streaming and boost and boost.enabled:
                if boost_ready:
                    icon = "🟢"
                else:
                    icon, detail = "🟠", " · ждёт обученную модель"
            elif current and current.running() and boost and boost.enabled:
                icon, detail = "🟠", " · ждёт свежий поток"
        elif label == "Groq":
            if streaming and groq and groq.enabled and groq_health[0]:
                icon = "🟢"
            elif groq and groq.enabled:
                icon = "🔴" if "лимит исчерпан" in groq_health[1] else "🟠"
                detail = f" · {groq_health[1]}"
        lines.append(f"{icon} {label} · {count}{detail}")
    return "\n".join(lines)


MENU_TITLE = "OTC · тестовые сигналы"


@router.message(Command("start", "help", "otc"))
async def show_menu(message: types.Message):
    await message.answer(MENU_TITLE, reply_markup=menu(message.chat.id))


@router.message(Command("otc_panel"))
async def mini_app_panel(message: types.Message):
    if not settings.mini_app_enabled or not settings.mini_app_url:
        await message.answer(
            "Mini App готов в коде, но публичный HTTPS-адрес пока не настроен. "
            "Инструкция: MINI_APP_SETUP.md. Данные и команды остаются доступны в Telegram."
        )
        return
    await message.answer("Панель TradingAI", reply_markup=menu(message.chat.id))


@router.message(Command("otc_status"))
async def status(message: types.Message):
    indicators = await asyncio.to_thread(strategy_status, service)
    await message.answer(MENU_TITLE + "\n\n" + indicators, reply_markup=menu(message.chat.id, True))


@router.message(Command("otc_stop"))
async def stop(message: types.Message):
    service.user_stopped = True
    (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "observer.stop").touch()
    stopped = await service.stop()
    await message.answer(
        ("OTC остановлен." if stopped else "OTC уже остановлен.")
        + " Будущие планы отменены; начавшиеся учитываются."
    )


@router.message(Command("otc_quality"))
async def quality(message: types.Message):
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    journals = {}
    for label, attr in (("Основной", "main_ready"), ("Groq", "groq_preview"), ("XGBoost", "ml_preview")):
        preview = getattr(service, attr, None)
        if preview:
            journals[label] = preview.journal
    if not journals:
        await message.answer("Журналы сигналов недоступны.")
        return
    lines = await asyncio.to_thread(signal_quality_report, journals, since.timestamp(), now.timestamp())
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk, parse_mode=None)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk, parse_mode=None)


@router.message(Command("otc_research"))
async def research_quality(message: types.Message):
    from services.otc_candidate_report import format_report, report

    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "7d"
    periods = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in periods:
        await message.answer("Период: 24h, 7d, 30d или all.")
        return
    now = time.time()
    since = now - periods[period] if periods[period] else 0
    data = await asyncio.to_thread(
        report, ROOT / "data/ready-candidates.db", ROOT / "data/market-history.db", since, now
    )
    text = format_report(data)
    for offset in range(0, len(text), 3500):
        await message.answer(text[offset:offset + 3500], parse_mode=None)


@router.message(Command("otc_filters"))
async def filters_report(message: types.Message):
    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "7d"
    windows = {"24h": 86400, "7d": 604800, "30d": 2592000}
    if period not in windows:
        await message.answer("Используй /otc_filters [24h|7d|30d]")
        return
    now = time.time()
    result = await asyncio.to_thread(
        filter_report, ROOT / "data", now - windows[period], now
    )
    text = format_filter_report(result)
    for offset in range(0, len(text), 3400):
        await message.answer(text[offset:offset + 3400], parse_mode=None)


@router.message(Command("otc_m1"))
async def m1_shadow_report(message: types.Message):
    from services.otc_m1_report import report as m1_report, format_report

    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "7d"
    windows = {"24h": 86400, "7d": 604800, "30d": 2592000}
    if period not in windows:
        await message.answer("Используй /otc_m1 [24h|7d|30d]")
        return
    now = time.time()
    result = await asyncio.to_thread(
        m1_report, ROOT / "data", now - windows[period], now
    )
    text = format_report(result)
    for offset in range(0, len(text), 3400):
        await message.answer(text[offset:offset + 3400], parse_mode=None)


@router.message(Command("otc_compare"))
async def compare_strategies(message: types.Message):
    from services.otc_signal_report import report as compare_report

    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "7d"
    windows = {"24h": 86400, "7d": 604800, "30d": 2592000}
    if period not in windows:
        await message.answer("Используй /otc_compare [24h|7d|30d]")
        return
    now = time.time()
    journals = {
        label: getattr(service, attr).journal for label, attr in SOURCES
        if getattr(service, attr, None) is not None
    }
    lines = await asyncio.to_thread(compare_report, journals, now - windows[period], now)
    text = "\n".join(lines)
    for offset in range(0, len(text), 3400):
        await message.answer(text[offset:offset + 3400], parse_mode=None)


@router.message(Command("otc_stats"))
async def stats(message: types.Message):
    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "all"
    durations = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in durations:
        await message.answer("Используйте /otc_stats [24h|7d|30d|all]")
        return
    await message.answer("Выбери экспирацию:", reply_markup=stats_menu(period))


async def send_confirmations(message, period="all"):
    from services.otc_confirmation_report import format_report, read_records

    durations = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in durations:
        await message.answer("Используйте /otc_confirmations [24h|7d|30d|all]")
        return
    since = time.time() - durations[period] if durations[period] else 0
    records = await asyncio.to_thread(read_records, ROOT / "data", since)
    text = await asyncio.to_thread(format_report, records)
    # Reports can grow with strategy versions; keep Telegram messages bounded.
    chunk = ""
    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@router.message(Command("otc_confirmations"))
async def confirmations(message: types.Message):
    args = (message.text or "").split()
    await send_confirmations(message, args[1] if len(args) > 1 else "all")


def stats_menu(period="all", cycles=False):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"M{m}",
                    callback_data=f"otc:{'cycles' if cycles else 'stats'}:{m}:{period}",
                )
                for m in (1, 5, 15)
            ],
            [
                InlineKeyboardButton(
                    text="Обычные"
                    if cycles
                    else "Циклы · архив условных"
                    if settings.primary_mode == "ready"
                    else "Циклы · 3 перекрытия",
                    callback_data=f"otc:{'stats' if cycles else 'cycles'}",
                )
            ],
        ]
    )


@router.message(Command("otc_diagnostics"))
async def diagnostics(message: types.Message):
    # No API call: inspecting diagnostics must not consume the account quota.
    await message.answer(
        service.status()
        + ("\n" + service.ml_runtime.status() if hasattr(service, "ml_runtime") else "")
        + "\n\nOTCharts SDK 0.7.0; Pocket Option venue otc; время UTC+2 преобразуется в UTC. "
        "OTCharts передаёт названия, текущие выплаты и статус открытия. Время изменения выплаты API не передаёт; время получения хранится отдельно. "
        "В тестовом режиме эти неизвестные данные не блокируют сигнал; он получает явную пометку. "
        "Строгий режим требует подтверждения. Без live-потока тест использует редкие снимки свечей с бюджетом запросов. "
        "Ключ и права проверяются при запуске сеанса. "
        "При отсутствии доступа: «Нет доступа к потоковым котировкам/выплатам». "
        "Независимых цен платформы нет: совпадение не подтверждено, исход UNKNOWN. "
        "Просадка и walk-forward доступны в отчёте replay. Секреты не выводятся."
    )


@router.callback_query(F.data.startswith("otc:"))
async def callback(query: types.CallbackQuery):
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        await query.answer("Неизвестное действие")
        return
    action = parts[1]
    chat = query.message.chat.id
    if action == "panel" and len(parts) == 3 and parts[2] in ("show", "hide"):
        await query.answer("Обновлено")
        expanded = parts[2] == "show"
        text = MENU_TITLE
        if expanded:
            text += "\n\n" + await asyncio.to_thread(strategy_status, service)
        keyboard = menu(chat, expanded)
        if query.message.text != text or query.message.reply_markup != keyboard:
            await query.message.edit_text(text, reply_markup=keyboard)
        return
    if action == "confirmations" and len(parts) == 2:
        await query.answer()
        await send_confirmations(query.message)
        return
    if action in ("mlstats", "groqstats"):
        preview_name = "groq_preview" if action == "groqstats" else "ml_preview"
        if (
            len(parts) != 3
            or parts[2] not in ("all", "1", "2", "3", "5", "15")
            or not hasattr(service, preview_name)
        ):
            await query.answer("Тест ещё не запущен")
            return
        await query.answer()
        seconds = None if parts[2] == "all" else int(parts[2]) * 60
        text = await asyncio.to_thread(
            getattr(service, preview_name).journal.stats, seconds, stats_epoch()
        )
        if action == "groqstats":
            text += "\n" + await asyncio.to_thread(service.groq_preview.client.status)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Все" if key == "all" else key + " мин",
                        callback_data="otc:" + action + ":" + key,
                    )
                    for key in ("all", "1", "2", "3", "5", "15")
                ]
            ]
        )
        if query.message.text != text:
            await query.message.answer(text, reply_markup=keyboard)
        return
    if action in ("stats", "cycles"):
        cycles = action == "cycles"
        if len(parts) == 2:
            await query.answer()
            await query.message.answer(
                "Выбери экспирацию:", reply_markup=stats_menu(cycles=cycles)
            )
        elif (
            len(parts) == 4
            and parts[2] in ("1", "5", "15")
            and parts[3] in ("24h", "7d", "30d", "all")
        ):
            await query.answer()
            duration = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}[
                parts[3]
            ]
            since = time.time() - duration if duration else 0
            if not cycles:
                since = max(since, stats_epoch())
            rows = await asyncio.to_thread(
                service.db.virtual_stats, since, int(parts[2]) * 60
            )
            if cycles:
                cycle_stats = await asyncio.to_thread(
                    read_stats, service.db, int(parts[2]) * 60, since
                )
                text = format_cycles(cycle_stats, int(parts[2]))
                if settings.primary_mode == "ready":
                    text = "Архив условных планов\n" + text
            else:
                if getattr(
                    settings, "primary_mode", "conditional"
                ) == "ready" and hasattr(service, "main_ready"):
                    text = await asyncio.to_thread(
                        service.main_ready.journal.stats, int(parts[2]) * 60, since
                    )
                else:
                    text = compact_stats(rows, int(parts[2]))
            if query.message.text != text:
                await query.message.edit_text(
                    text, reply_markup=stats_menu(parts[3], cycles=cycles)
                )
        else:
            await query.answer("Недопустимый выбор")
        return
    if action == "tf" and len(parts) == 3 and parts[2] in ("all", "1", "5", "15"):
        choices[chat] = TIMEFRAMES if parts[2] == "all" else (int(parts[2]) * 60,)
        await query.answer("Экспирации выбраны")
        await query.message.edit_reply_markup(
            reply_markup=menu(chat, query.message.text != MENU_TITLE)
        )
    elif (
        action == "start"
        and len(parts) == 3
        and parts[2] in ("0", "30", "60", "120", "240", "480")
    ):
        started = await service.start(
            query.bot, chat, int(parts[2]), choices.get(chat, TIMEFRAMES)
        )
        await query.answer("Проверяю доступ" if started else "Сеанс уже запущен")
        keyboard = menu(chat, query.message.text != MENU_TITLE)
        if query.message.reply_markup != keyboard:
            await query.message.edit_reply_markup(reply_markup=keyboard)

    elif action == "observer_stop" and len(parts) == 2:
        (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "observer.stop").touch()
        await query.answer("Наблюдатель останавливается")
    elif action == "stop" and len(parts) == 2:
        (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "observer.stop").touch()
        service.user_stopped = True
        stopped = await service.stop()
        await query.answer("Остановлено" if stopped else "Уже остановлен")
        keyboard = menu(chat, query.message.text != MENU_TITLE)
        if query.message.reply_markup != keyboard:
            await query.message.edit_reply_markup(reply_markup=keyboard)
    elif action == "status" and len(parts) == 2:
        await query.answer()
        indicators = await asyncio.to_thread(strategy_status, service)
        await query.message.answer(MENU_TITLE + "\n\n" + indicators, reply_markup=menu(chat, True))
    else:
        await query.answer("Недопустимый выбор")
