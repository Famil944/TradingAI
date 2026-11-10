"""Owner-only Telegram menu and callbacks."""

import asyncio
import time

from aiogram import BaseMiddleware, F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import settings
from services.otc_market import TIMEFRAMES
from services.otc_statistics import format_stats

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


def menu(chat_id):
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
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start", "help", "otc"))
async def show_menu(message: types.Message):
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
    await message.answer(service.status())


@router.message(Command("otc_stop"))
async def stop(message: types.Message):
    stopped = await service.stop()
    await message.answer("OTC остановлен." if stopped else "OTC уже остановлен.")


@router.message(Command("otc_stats"))
async def stats(message: types.Message):
    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "all"
    durations = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in durations:
        await message.answer("Используйте /otc_stats [24h|7d|30d|all]")
        return
    since = time.time() - durations[period] if durations[period] else 0
    rows = await asyncio.to_thread(service.db.stats, since)
    await message.answer(format_stats(rows, period))


@router.message(Command("otc_diagnostics"))
async def diagnostics(message: types.Message):
    # No API call: inspecting diagnostics must not consume the account quota.
    await message.answer(
        service.status()
        + "\n\nOTCharts SDK 0.7.0; Pocket Option venue otc; время UTC+2 преобразуется в UTC. "
        "SDK не передаёт timestamp выплаты; статус открытия Pocket Option может быть неизвестен. "
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
    elif action == "stop" and len(parts) == 2:
        stopped = await service.stop()
        await query.answer("Остановлено" if stopped else "Уже остановлен")
    elif action == "status" and len(parts) == 2:
        await query.answer()
        await query.message.answer(service.status())
    else:
        await query.answer("Недопустимый выбор")
