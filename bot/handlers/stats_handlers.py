from bot.inline_menus import stats_menu
import config.trading_mode as trading_mode
from services.demo_statistics_service import DemoStatisticsService


statistics_service = DemoStatisticsService()

async def show_stats(query, days=None, title="За всё время"):
    status = statistics_service.get_statistics(
        days=days,
        trading_mode=trading_mode.CURRENT_MODE.value,
    )
    text = (
        f"📊 {trading_mode.CURRENT_MODE.value}: {title}\n\n"
        f"Сделок: {status['total_trades']}\n"
        f"Открытых: {status['open_trades']}\n"
        f"Закрытых: {status['closed_trades']}\n"
        f"🏆 Winrate: {status['win_rate']}%\n\n"
        f"💰 Общий PnL: {status['total_pnl']:.4f} USDT\n"
        f"Средний PnL: {status['average_pnl']:.4f} USDT\n"
        f"Лучшая: {status['best_trade']:.4f} USDT\n"
        f"Худшая: {status['worst_trade']:.4f} USDT\n"
        f"📉 Макс. просадка: "
        f"{status['maximum_drawdown']:.4f} USDT"
    )

    await query.edit_message_text(
        text,
        reply_markup=stats_menu(),
    )


async def show_profit(query, paper=None):
    await show_stats(query)


async def show_winrate(query, paper=None):
    await show_stats(query)


async def show_today(query, paper=None):
    await show_stats(query, days=1, title="Сегодня")


async def show_week(query, paper=None):
    await show_stats(query, days=7, title="7 дней")


async def show_month(query, paper=None):
    await show_stats(query, days=30, title="30 дней")
