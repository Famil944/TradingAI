from bot.inline_menus import stats_menu
from services.trade_analytics_service import TradeAnalyticsService


analytics = TradeAnalyticsService()


async def show_trade_analytics(query):
    data = analytics.summary()

    text = (
        f"📊 Аналитика сделок\n\n"
        f"Сделок: {data['count']}\n"
        f"Общая прибыль: {data['total_profit']} USDT\n"
        f"Средняя прибыль: {data['average_profit']} USDT\n"
        f"Лучшая сделка: {data['best_trade']} USDT\n"
        f"Худшая сделка: {data['worst_trade']} USDT"
    )

    await query.edit_message_text(
        text,
        reply_markup=stats_menu(),
    )