from bot.inline_menus import paper_menu


async def show_paper_status(query, paper):
    await query.edit_message_text(
        paper.status_text(),
        reply_markup=paper_menu(),
    )


async def show_paper_balance(query, paper):
    status = paper.engine.status()

    await query.edit_message_text(
        f"💰 Баланс Paper Trading\n\n"
        f"{status['balance']} USDT",
        reply_markup=paper_menu(),
    )


async def show_paper_stats(query, paper):
    status = paper.engine.status()

    text = (
        f"📊 Paper Trading Stats\n\n"
        f"Баланс: {status['balance']} USDT\n"
        f"Сделок: {status['trades']}\n"
        f"Winrate: {status['winrate']}%\n"
        f"Открытая позиция: {'да' if status['has_position'] else 'нет'}"
    )

    await query.edit_message_text(
        text,
        reply_markup=paper_menu(),
    )


async def show_paper_history(query, paper):
    trades = paper.engine.history.all()

    if not trades:
        text = "📄 История Paper-сделок пока пустая."
    else:
        text = "📄 История Paper Trading\n\n"

        for index, trade in enumerate(trades[-10:], start=1):
            text += (
                f"{index}. {trade.get('symbol')} {trade.get('side')}\n"
                f"Вход: {trade.get('entry_price')}\n"
                f"Прибыль: {trade.get('profit', 'N/A')} USDT\n\n"
            )

    await query.edit_message_text(
        text,
        reply_markup=paper_menu(),
    )


async def show_last_trade(query, paper):
    trades = paper.engine.history.all()

    if not trades:
        text = "🏁 Последней сделки пока нет."
    else:
        trade = trades[-1]
        text = (
            f"🏁 Последняя сделка\n\n"
            f"Монета: {trade.get('symbol')}\n"
            f"Сторона: {trade.get('side')}\n"
            f"Вход: {trade.get('entry_price')}\n"
            f"Выход: {trade.get('exit_price', 'N/A')}\n"
            f"Прибыль: {trade.get('profit', 'N/A')} USDT"
        )

    await query.edit_message_text(
        text,
        reply_markup=paper_menu(),
    )