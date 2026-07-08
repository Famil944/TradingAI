from bot.inline_menus import app_main_menu, auto_menu, paper_menu, market_menu


async def show_main_menu(query):
    await query.edit_message_text(
        "🏠 Главное меню",
        reply_markup=app_main_menu()
    )


async def show_market_menu(query):
    await query.edit_message_text(
        "📈 Рынок",
        reply_markup=market_menu()
    )


async def show_auto_menu(query):
    await query.edit_message_text(
        "🤖 Автоторговля",
        reply_markup=auto_menu()
    )


async def show_paper_menu(query):
    await query.edit_message_text(
        "💼 Paper Trading",
        reply_markup=paper_menu()
    )


async def show_help(query):
    await query.edit_message_text(
        "ℹ️ Помощь\n\n"
        "🏠 Меню - открыть главное меню\n"
        "🤖 Автоторговля - автоанализ\n"
        "💼 Paper Trading - виртуальная торговля\n"
        "📈 Рынок - анализ монет",
        reply_markup=app_main_menu()
    )