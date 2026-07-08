from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def app_main_menu():
    keyboard = [
        [
            InlineKeyboardButton("🔄 Обновить", callback_data="refresh_dashboard"),
            InlineKeyboardButton("🔔 Уведомления", callback_data="menu_notifications"),
        ],
        [
            InlineKeyboardButton("📈 Рынок", callback_data="menu_market"),
            InlineKeyboardButton("🤖 Автоторговля", callback_data="menu_auto"),
        ],
        [
            InlineKeyboardButton("💼 Paper", callback_data="menu_paper"),
            InlineKeyboardButton("📊 Статистика", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu_settings"),
            InlineKeyboardButton("ℹ️ Помощь", callback_data="menu_help"),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


def auto_menu():
    keyboard = [
        [InlineKeyboardButton("▶️ Анализ один раз", callback_data="auto_once_btn")],
        [
            InlineKeyboardButton("🟢 Включить", callback_data="auto_on_btn"),
            InlineKeyboardButton("🔴 Выключить", callback_data="auto_off_btn"),
        ],
        [
            InlineKeyboardButton("📌 Позиция", callback_data="position_btn"),
            InlineKeyboardButton("📋 Последний анализ", callback_data="last_analysis_btn"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)


def paper_menu():
    keyboard = [
        [
            InlineKeyboardButton("📄 Статус", callback_data="paper_status_btn"),
            InlineKeyboardButton("💰 Баланс", callback_data="paper_balance_btn"),
        ],
        [
            InlineKeyboardButton("📊 Статистика", callback_data="paper_stats_btn"),
            InlineKeyboardButton("📜 История", callback_data="paper_history_btn"),
        ],
        [InlineKeyboardButton("🏁 Последняя сделка", callback_data="last_trade_btn")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)


def market_menu():
    keyboard = [
        [InlineKeyboardButton("🔥 Лучшие сигналы", callback_data="market_scan")],
        [
            InlineKeyboardButton("💰 BTC", callback_data="analyze_BTCUSDT"),
            InlineKeyboardButton("📈 ETH", callback_data="analyze_ETHUSDT"),
        ],
        [InlineKeyboardButton("📊 Анализ монеты", callback_data="menu_analyze")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)


def stats_menu():
    keyboard = [
        [
            InlineKeyboardButton("📈 Доходность", callback_data="stats_profit_btn"),
            InlineKeyboardButton("🏆 Winrate", callback_data="stats_winrate_btn"),
        ],
        [
            InlineKeyboardButton("📅 Сегодня", callback_data="stats_today_btn"),
            InlineKeyboardButton("📅 Неделя", callback_data="stats_week_btn"),
        ],
        [InlineKeyboardButton("📅 Месяц", callback_data="stats_month_btn")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)


def settings_menu():
    keyboard = [
        [
            InlineKeyboardButton("💵 Размер сделки", callback_data="settings_size_btn"),
            InlineKeyboardButton("⚠️ Риск", callback_data="settings_risk_btn"),
        ],
        [
            InlineKeyboardButton("⭐ Quality Score", callback_data="settings_quality_btn"),
            InlineKeyboardButton("🕒 Таймфрейм", callback_data="settings_timeframe_btn"),
        ],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="menu_notifications")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)