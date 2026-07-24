from bot.inline_menus import (
    live_confirmation_menu,
    notifications_menu,
    quality_menu,
    risk_menu,
    settings_menu,
    timeframe_menu,
    trade_size_menu,
    trading_mode_menu,
)
from services.app_settings import AppSettings


settings = AppSettings()


async def show_settings(query):
    await query.edit_message_text(
        "⚙️ Настройки\n\n"
        "Выберите параметр:",
        reply_markup=settings_menu(),
    )


async def show_setting(query, title, value, markup):
    await query.edit_message_text(
        f"⚙️ {title}\n\n"
        f"Текущее значение: {value}\n\n"
        "Выберите новое значение:",
        reply_markup=markup,
    )


async def show_risk(query):
    await show_setting(
        query,
        "Риск на сделку",
        f"{settings.get('risk_percent')}%",
        risk_menu(),
    )


async def show_trade_size(query):
    await show_setting(
        query,
        "Максимальный размер позиции",
        settings.get("max_quantity"),
        trade_size_menu(),
    )


async def show_quality(query):
    await show_setting(
        query,
        "Минимальный Quality Score",
        settings.get("quality_score"),
        quality_menu(),
    )


async def show_timeframe(query):
    await show_setting(
        query,
        "Таймфрейм автосканирования",
        settings.get("timeframe"),
        timeframe_menu(),
    )


async def show_notifications(query):
    labels = {
        "all": "Все сообщения",
        "trades": "Только сделки и ошибки",
        "off": "Выключены",
    }
    current = settings.get("notifications")
    await show_setting(
        query,
        "Уведомления",
        labels[current],
        notifications_menu(),
    )


async def apply_setting(query, callback_data):
    prefixes = {
        "set_risk_": ("risk_percent", "Риск"),
        "set_size_": ("max_quantity", "Размер позиции"),
        "set_quality_": ("quality_score", "Quality Score"),
        "set_timeframe_": ("timeframe", "Таймфрейм"),
        "set_notifications_": ("notifications", "Уведомления"),
    }
    for prefix, (key, title) in prefixes.items():
        if callback_data.startswith(prefix):
            value = callback_data[len(prefix):]
            saved = settings.set(key, value)
            await query.edit_message_text(
                f"✅ {title}: {saved}\n\n"
                "Настройка сохранена и применяется без перезапуска.",
                reply_markup=settings_menu(),
            )
            return
    raise ValueError("Неизвестная настройка")


async def show_trading_mode(query, current_mode):
    await query.edit_message_text(
        "🔁 Торговый счёт\n\n"
        f"Сейчас запущен: {current_mode.value}\n\n"
        "Выбранный режим будет проверен и применится "
        "после перезапуска бота.",
        reply_markup=trading_mode_menu(current_mode.value),
    )


async def confirm_live_mode(query):
    await query.edit_message_text(
        "⚠️ Переключение на LIVE\n\n"
        "После перезапуска команды /trade_* и автоторговля "
        "будут работать с реальными средствами.\n\n"
        "Продолжить?",
        reply_markup=live_confirmation_menu(),
    )
