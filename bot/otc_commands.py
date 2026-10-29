"""Telegram controls for a session-based Pocket Option OTC signal scanner."""
import os
from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from services.otc_session import OTCSession, TIMEFRAMES
from services.otc_data import usage


router = Router()
service = OTCSession()
choices = {}


def allowed(chat_id: int) -> bool:
    value = os.getenv("OTC_ADMIN_CHAT_ID", "")
    return value.isdigit() and int(value) == chat_id


def menu(chat_id):
    chosen = choices.get(chat_id, TIMEFRAMES)
    timeframes = [
        InlineKeyboardButton(
            text=("✅ " if selected == choice else "") + label,
            callback_data=f"otc:tf:{choice}",
        )
        for choice, label, selected in (
            ("all", "Все M1/M5/M15", chosen == TIMEFRAMES),
            ("1", "M1", chosen == (60,)),
            ("5", "M5", chosen == (300,)),
            ("15", "M15", chosen == (900,)),
        )
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [timeframes[0]],
        timeframes[1:],
        [
            InlineKeyboardButton(text="30 мин", callback_data="otc:start:30"),
            InlineKeyboardButton(text="1 час", callback_data="otc:start:60"),
        ],
        [
            InlineKeyboardButton(text="2 часа", callback_data="otc:start:120"),
            InlineKeyboardButton(text="4 часа", callback_data="otc:start:240"),
        ],
        [
            InlineKeyboardButton(text="8 часов", callback_data="otc:start:480"),
            InlineKeyboardButton(text="До остановки", callback_data="otc:start:0"),
        ],
        [InlineKeyboardButton(text="⏹ Остановить", callback_data="otc:stop")],
        [InlineKeyboardButton(text="🔎 Статус", callback_data="otc:status")],
    ])


@router.message(Command("otc_id"))
async def otc_id(message: types.Message):
    await message.answer(f"ID этого Telegram-чата: {message.chat.id}")


@router.message(Command("otc"))
async def otc_menu(message: types.Message):
    if not allowed(message.chat.id):
        await message.answer(
            "OTC-сканер доступен владельцу. Узнай ID через /otc_id "
            "и укажи OTC_ADMIN_CHAT_ID в локальном .env."
        )
        return
    await message.answer(
        "📊 Pocket Option OTC · только 90%+ и свежие данные.\n"
        "Выбери экспирацию, затем время работы сканера.\n"
        "Не открывает сделки. Бесплатный OTCharts не поддерживает "
        "сигналы в реальном времени.",
        reply_markup=menu(message.chat.id),
    )


@router.message(Command("otc_status"))
async def otc_status(message: types.Message):
    if not allowed(message.chat.id):
        await message.answer("Нет доступа. Проверь OTC_ADMIN_CHAT_ID.")
        return
    lines = [service.status()]
    try:
        current = await usage()
        lines.append(
            f"\nТариф OTCharts: {current.plan_name}\n"
            f"Потоки: {current.streams_limit}\n"
            f"Активов на поток: {current.instruments_per_stream or 'без лимита'}\n"
            f"API-запросов осталось: {current.remaining}"
        )
    except Exception:
        lines.append("\nНе удалось проверить OTCharts. Проверь OTCHARTS_API_KEY.")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("otc:"))
async def otc_callback(query: types.CallbackQuery):
    if not allowed(query.message.chat.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = query.data.split(":")
    action = parts[1]
    if action == "tf":
        tf = parts[2]
        choices[query.message.chat.id] = (
            TIMEFRAMES if tf == "all" else (int(tf) * 60,)
        )
        await query.answer("Экспирации выбраны")
        await query.message.edit_reply_markup(reply_markup=menu(query.message.chat.id))
    elif action == "start":
        duration = int(parts[2])
        if service.start(
            query.bot, query.message.chat.id,
            duration, choices.get(query.message.chat.id, TIMEFRAMES),
        ):
            await query.answer("Сканирование запускается")
            await query.message.answer(
                "⏳ Проверяю тариф и подключаю поток. "
                "Если поток недоступен, сигналы не отправляются."
            )
        else:
            await query.answer("Сканирование уже идёт", show_alert=True)
    elif action == "stop":
        stopped = service.stop()
        await query.answer("Остановлено" if stopped else "Сканер уже остановлен")
        await query.message.answer("⏹ Остановка OTC-сканера запрошена.")
    elif action == "status":
        await query.answer()
        await query.message.answer(service.status())
