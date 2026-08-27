import asyncio

from bot.inline_menus import auto_menu


async def run_auto_once(query, auto_trader):
    await query.edit_message_text("⏳ Запускаю автоанализ...")

    result = await asyncio.to_thread(auto_trader.run_once)

    await query.edit_message_text(
        result,
        reply_markup=auto_menu(),
    )


async def turn_auto_on(query, context, auto_state, auto_loop, position_watch_loop, notifier):
    notifier.setup(context.bot, query.message.chat_id)
    auto_state.settings.set(
        "telegram_chat_id",
        str(query.message.chat_id),
    )

    auto_state.turn_on()

    if not auto_loop.is_running:
        asyncio.create_task(auto_loop.start())

    if not position_watch_loop.is_running:
        asyncio.create_task(position_watch_loop.start())

    await query.edit_message_text(
        "🤖 Авто-режим включён",
        reply_markup=auto_menu(),
    )


async def turn_auto_off(
    query,
    auto_state,
    auto_loop=None,
    position_watch_loop=None,
):
    if auto_loop is not None:
        auto_loop.stop()
    if position_watch_loop is not None:
        position_watch_loop.stop()

    await query.edit_message_text(
        auto_state.turn_off(),
        reply_markup=auto_menu(),
    )


async def show_position(query, controller):
    positions = await asyncio.to_thread(
        controller.client.open_positions
    )
    if not positions:
        await query.edit_message_text(
            "📭 Открытых биржевых позиций нет.",
            reply_markup=auto_menu(),
        )
        return

    rows = ["📌 Открытые биржевые позиции\n"]
    for position in positions:
        amount = float(position.get("positionAmt", 0))
        side = "LONG" if amount > 0 else "SHORT"
        rows.append(
            f"{position.get('symbol', '?')} | {side}\n"
            f"Количество: {abs(amount)}\n"
            f"Вход: {position.get('entryPrice', '?')}\n"
            f"PnL: {position.get('unRealizedProfit', '?')} USDT"
        )
    text = "\n\n".join(rows)

    await query.edit_message_text(
        text,
        reply_markup=auto_menu(),
    )
