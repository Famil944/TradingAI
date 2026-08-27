import traceback

from services.demo_statistics_service import DemoStatisticsService


stats = DemoStatisticsService()


async def demo_stats(update, context):
    try:
        statistics = stats.get_statistics()

        await update.message.reply_text(
            f"📊 Demo Trading Stats\n\n"
            f"Всего сделок: {statistics['total_trades']}\n"
            f"Открытых: {statistics['open_trades']}\n"
            f"Закрытых: {statistics['closed_trades']}\n\n"
            f"✅ Прибыльных: {statistics['winning_trades']}\n"
            f"❌ Убыточных: {statistics['losing_trades']}\n"
            f"🤝 Безубыточных: {statistics['break_even_trades']}\n\n"
            f"🏆 Win Rate: {statistics['win_rate']}%\n"
            f"💰 Общий PnL: {statistics['total_pnl']:.4f} USDT\n"
            f"📊 Средний PnL: {statistics['average_pnl']:.4f} USDT\n"
            f"📉 Максимальная просадка: "
            f"{statistics['maximum_drawdown']:.4f} USDT"
        )

    except Exception as error:
        print(f"DEMO STATS ERROR: {error}")
        traceback.print_exc()

        try:
            await update.message.reply_text(
                f"❌ Ошибка demo_stats:\n{error}"
            )
        except Exception:
            pass