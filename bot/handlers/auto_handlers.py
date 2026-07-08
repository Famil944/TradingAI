import asyncio

from bot.inline_menus import auto_menu


async def run_auto_once(query, auto_trader):
    await query.edit_message_text("⏳ Запускаю автоанализ...")

    result = auto_trader.run_once()

    await query.edit_message_text(
        result,
        reply_markup=auto_menu(),
    )


async def turn_auto_on(query, context, auto_state, auto_loop, position_watch_loop, notifier):
    notifier.setup(context.bot, query.message.chat_id)

    auto_state.turn_on()

    if not auto_loop.is_running:
        asyncio.create_task(auto_loop.start())

    if not position_watch_loop.is_running:
        asyncio.create_task(position_watch_loop.start())

    await query.edit_message_text(
        "🤖 Авто-режим включён",
        reply_markup=auto_menu(),
    )


async def turn_auto_off(query, auto_state):
    await query.edit_message_text(
        auto_state.turn_off(),
        reply_markup=auto_menu(),
    )


async def show_position(query, paper):
    position = paper.engine.trader.position

    if not position:
        await query.edit_message_text(
            "📭 Открытой позиции нет.",
            reply_markup=auto_menu(),
        )
        return

    text = (
        f"📌 Текущая Paper-позиция\n\n"
        f"Монета: {position.symbol}\n"
        f"Сторона: {position.side}\n"
        f"Вход: {position.entry_price}\n"
        f"Объём: {position.amount}\n"
        f"Take Profit: {position.take_profit}\n"
        f"Stop Loss: {position.stop_loss}\n"
        f"Partial TP: {'да' if position.partial_closed else 'нет'}"
    )

    await query.edit_message_text(
        text,
        reply_markup=auto_menu(),
    )