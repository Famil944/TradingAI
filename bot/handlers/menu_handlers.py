from bot.inline_menus import (
    app_main_menu,
    auto_menu,
    help_back_menu,
    help_menu,
    market_menu,
    paper_menu,
)


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
        "ℹ️ Помощь и обучение\n\n"
        "Выберите раздел — бот пошагово объяснит, что нажимать "
        "и где смотреть результат.",
        reply_markup=help_menu(),
    )


HELP_PAGES = {
    "help_quick_start": (
        "🚀 Быстрый старт\n\n"
        "1. Откройте ⚙️ Настройки → 🔁 Demo / Live.\n"
        "2. Для первого запуска выберите DEMO.\n"
        "3. Откройте 🤖 Автоторговля.\n"
        "4. Нажмите 🟢 Включить.\n"
        "5. Проверяйте сделки через 📌 Позиция.\n"
        "6. Результаты смотрите в 📊 Статистика.\n\n"
        "После включения бот самостоятельно сканирует рынок, "
        "выбирает сигнал, рассчитывает размер и выставляет защиту."
    ),
    "help_auto": (
        "🤖 Автоторговля\n\n"
        "Путь: 🏠 Меню → 🤖 Автоторговля.\n\n"
        "🟢 Включить — запускает постоянное сканирование.\n"
        "🔴 Выключить — запрещает новые автоматические входы.\n"
        "▶️ Анализ один раз — выполняет одну проверку рынка.\n"
        "📋 Последний анализ — показывает последнее решение.\n\n"
        "После рестарта Render включённая автоторговля "
        "возобновляется автоматически."
    ),
    "help_positions": (
        "📌 Открытые позиции\n\n"
        "Путь: 🏠 Меню → 🤖 Автоторговля → 📌 Позиция.\n\n"
        "Там отображаются позиции активного счёта DEMO или LIVE: "
        "монета, LONG/SHORT, количество, цена входа и текущий PnL.\n\n"
        "Если список пуст, биржевых позиций сейчас нет."
    ),
    "help_statistics": (
        "📊 Статистика\n\n"
        "Путь: 🏠 Меню → 📊 Статистика.\n\n"
        "Можно посмотреть результаты за сегодня, 7 дней, "
        "30 дней или всё время: количество сделок, Winrate, "
        "общий и средний PnL, лучшую/худшую сделку и просадку.\n\n"
        "Статистика DEMO и LIVE разделена."
    ),
    "help_modes": (
        "🔁 Режимы DEMO и LIVE\n\n"
        "Путь: 🏠 Меню → ⚙️ Настройки → 🔁 Demo / Live.\n\n"
        "DEMO использует тестовые средства Binance.\n"
        "LIVE использует реальные средства и требует отдельного "
        "подтверждения риска.\n\n"
        "Переключение возможно только без открытых позиций. "
        "После проверки режим применяется сразу."
    ),
    "help_safety": (
        "🛡 Безопасность\n\n"
        "• Сначала проверяйте изменения в DEMO.\n"
        "• Не запускайте две копии бота с одним Telegram-токеном.\n"
        "• Не отправляйте API-ключи в Telegram или Git.\n"
        "• Выключение автоторговли запрещает новые автосделки, "
        "но не закрывает уже открытую позицию.\n"
        "• Защитные Stop Loss и Take Profit размещаются на Binance.\n"
        "• При подозрительной работе сначала нажмите 🔴 Выключить "
        "и проверьте позиции."
    ),
}


async def show_help_page(query, page):
    await query.edit_message_text(
        HELP_PAGES[page],
        reply_markup=help_back_menu(),
    )
