from bot.inline_menus import settings_menu


async def show_settings(query):
    await query.edit_message_text(
        "⚙️ Настройки\n\n"
        "Выберите параметр:",
        reply_markup=settings_menu(),
    )


async def show_setting(query, title, value):
    await query.edit_message_text(
        f"⚙️ {title}\n\n"
        f"Текущее значение:\n{value}\n\n"
        "Изменение через интерфейс будет добавлено позже.",
        reply_markup=settings_menu(),
    )


async def show_risk(query):
    await show_setting(query, "Риск", "Безопасный")


async def show_trade_size(query):
    await show_setting(query, "Размер сделки", "Используется значение из config")


async def show_quality(query):
    await show_setting(query, "Quality Score", "70")


async def show_timeframe(query):
    await show_setting(query, "Таймфрейм", "1H")