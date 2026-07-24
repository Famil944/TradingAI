from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config.trading_mode as trading_mode


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
        [
            InlineKeyboardButton(
                f"🔁 Счёт: {trading_mode.CURRENT_MODE.value}",
                callback_data="settings_mode_btn",
            )
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


def help_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "🚀 Быстрый старт",
                callback_data="help_quick_start",
            ),
            InlineKeyboardButton(
                "🤖 Автоторговля",
                callback_data="help_auto",
            ),
        ],
        [
            InlineKeyboardButton(
                "📌 Позиции",
                callback_data="help_positions",
            ),
            InlineKeyboardButton(
                "📊 Статистика",
                callback_data="help_statistics",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔁 Demo / Live",
                callback_data="help_modes",
            ),
            InlineKeyboardButton(
                "🛡 Безопасность",
                callback_data="help_safety",
            ),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


def help_back_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ К разделам", callback_data="menu_help")]]
    )


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
        [
            InlineKeyboardButton(
                "🔁 Demo / Live",
                callback_data="settings_mode_btn",
            )
        ],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="menu_notifications")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_app_main")],
    ]

    return InlineKeyboardMarkup(keyboard)


def trading_mode_menu(current_mode):
    keyboard = [
        [
            InlineKeyboardButton(
                "🧪 DEMO",
                callback_data="mode_select_demo",
            ),
            InlineKeyboardButton(
                "💰 LIVE",
                callback_data="mode_select_live",
            ),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def live_confirmation_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "⚠️ Да, выбрать LIVE",
                callback_data="mode_confirm_live",
            )
        ],
        [
            InlineKeyboardButton(
                "Отмена",
                callback_data="settings_mode_btn",
            )
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def setting_values_menu(prefix, values):
    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=f"set_{prefix}_{value}")
            for label, value in row
        ]
        for row in values
    ]
    keyboard.append(
        [InlineKeyboardButton("⬅️ Назад", callback_data="menu_settings")]
    )
    return InlineKeyboardMarkup(keyboard)


def risk_menu():
    return setting_values_menu(
        "risk",
        [[("0.5%", "0.5"), ("1%", "1.0"), ("2%", "2.0")]],
    )


def trade_size_menu():
    return setting_values_menu(
        "size",
        [[("0.001", "0.001"), ("0.005", "0.005"), ("0.01", "0.01")]],
    )


def quality_menu():
    return setting_values_menu(
        "quality",
        [
            [("65", "65"), ("70", "70")],
            [("75", "75"), ("80", "80")],
        ],
    )


def timeframe_menu():
    return setting_values_menu(
        "timeframe",
        [[("15m", "15m"), ("1H", "1h"), ("4H", "4h")]],
    )


def notifications_menu():
    return setting_values_menu(
        "notifications",
        [
            [("🔔 Все", "all"), ("💼 Только сделки", "trades")],
            [("🔕 Выключить", "off")],
        ],
    )
