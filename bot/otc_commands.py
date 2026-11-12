"""Owner-only Telegram menu and callbacks."""

import asyncio
import time

from aiogram import BaseMiddleware, F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import ROOT, settings
from services.otc_cycles import format_cycles, read_stats
from services.otc_evaluation import compact_stats
from services.otc_market import TIMEFRAMES

router = Router()
service = None
choices = {}


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


def menu(chat_id, conditional=None):
    if conditional is None:
        conditional = getattr(settings, "signal_mode", "immediate") == "conditional"
    chosen = choices.get(chat_id, TIMEFRAMES)
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
    rows = [[buttons[0]], buttons[1:]]
    for pair in [
        [(30, "30 минут"), (60, "1 час")],
        [(120, "2 часа"), (240, "4 часа")],
        [(480, "8 часов"), (0, "До остановки")],
    ]:
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"otc:start:{value}")
                for value, label in pair
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Остановить", callback_data="otc:stop"),
            InlineKeyboardButton(text="Статус", callback_data="otc:status"),
        ]
    )
    rows.append([InlineKeyboardButton(text="Статистика", callback_data="otc:stats")])
    rows.append([InlineKeyboardButton(text="Подтверждения", callback_data="otc:confirmations")])
    rows.append(
        [
            InlineKeyboardButton(text="🟠 XGBoost", callback_data="otc:mlstats:all"),
            InlineKeyboardButton(text="🟣 Groq", callback_data="otc:groqstats:all"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start", "help", "otc"))
async def show_menu(message: types.Message):
    if getattr(settings, "signal_mode", "immediate") == "conditional":
        await message.answer(
            "📊 OTC · M1 / M5 / M15\n"
            + (
                "🔵 Основной v5: готовые сигналы · тест.\n"
                if settings.primary_mode == "ready"
                else "🔵 Основной: вход по условию свечи.\n"
            )
            + "🟣 Groq / 🟠 XGBoost: отдельные тесты.\n"
            "Опоздал — пропуск.\n"
            "Выбери экспирацию и длительность.",
            reply_markup=menu(message.chat.id),
        )
        return
    await message.answer(
        "Pocket Option OTC · исследовательские сигналы M1/M5/M15.\n"
        "ЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ. Автоматических сделок нет.\n"
        "Тестовый режим допускает неизвестные выплату и открытие; строгий требует подтверждения и ≥90%.\n"
        "/otc_status — статус; /otc_stop — остановить; /otc_stats [24h|7d|30d|all] — статистика; "
        "/otc_diagnostics — ограничения источника.\nВыберите экспирацию и длительность.",
        reply_markup=menu(message.chat.id),
    )


@router.message(Command("otc_status"))
async def status(message: types.Message):
    await message.answer(service.brief_status())


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
            getattr(service, preview_name).journal.stats, seconds
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
        await query.message.edit_reply_markup(reply_markup=menu(chat))
    elif (
        action == "start"
        and len(parts) == 3
        and parts[2] in ("0", "30", "60", "120", "240", "480")
    ):
        started = await service.start(
            query.bot, chat, int(parts[2]), choices.get(chat, TIMEFRAMES)
        )
        await query.answer("Проверяю доступ" if started else "Сеанс уже запущен")
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
    elif action == "status" and len(parts) == 2:
        await query.answer()
        await query.message.answer(service.brief_status())
    else:
        await query.answer("Недопустимый выбор")
