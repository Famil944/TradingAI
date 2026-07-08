from bot.inline_menus import stats_menu


async def show_stats(query, paper):
    status = paper.engine.status()

    text = (
        f"📊 Статистика\n\n"
        f"💰 Баланс: {status['balance']} USDT\n"
        f"📈 Сделок: {status['trades']}\n"
        f"🏆 Winrate: {status['winrate']}%\n"
        f"📌 Открытая позиция: {'Да' if status['has_position'] else 'Нет'}"
    )

    await query.edit_message_text(
        text,
        reply_markup=stats_menu(),
    )


async def show_profit(query, paper):
    await show_stats(query, paper)


async def show_winrate(query, paper):
    await show_stats(query, paper)


async def show_today(query, paper):
    await show_stats(query, paper)


async def show_week(query, paper):
    await show_stats(query, paper)


async def show_month(query, paper):
    await show_stats(query, paper)