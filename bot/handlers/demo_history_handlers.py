from services.demo_trade_log_service import DemoTradeLogService


trade_log = DemoTradeLogService()


def format_value(value, default="-"):
    if value is None:
        return default

    return value


async def trade_history(update, context):
    try:
        trades = trade_log.get_recent_trades(limit=10)

        if not trades:
            await update.message.reply_text(
                "📭 История Demo-сделок пуста."
            )
            return

        text = "📜 Последние Demo-сделки\n\n"

        for index, trade in enumerate(trades, start=1):
            pnl = trade["pnl"]
            pnl_percent = trade["pnl_percent"]

            if pnl is None:
                pnl_text = "Сделка ещё открыта"
                result_icon = "🟡"
            elif float(pnl) > 0:
                pnl_text = (
                    f"+{float(pnl):.4f} USDT "
                    f"(+{float(pnl_percent):.2f}%)"
                )
                result_icon = "✅"
            elif float(pnl) < 0:
                pnl_text = (
                    f"{float(pnl):.4f} USDT "
                    f"({float(pnl_percent):.2f}%)"
                )
                result_icon = "❌"
            else:
                pnl_text = "0.0000 USDT (0.00%)"
                result_icon = "🤝"

            text += (
                f"{index}. {result_icon} "
                f"{trade['symbol']} {trade['side']}\n"
                f"Вход: {format_value(trade['entry_price'])}\n"
                f"Выход: {format_value(trade['exit_price'])}\n"
                f"PnL: {pnl_text}\n"
                f"Стратегия: "
                f"{format_value(trade['strategy'], 'Unknown')}\n"
                f"Quality Score: "
                f"{format_value(trade['quality_score'], 0)}\n"
                f"ATR: {format_value(trade['atr'], 0)}\n"
                f"Причина выхода: "
                f"{format_value(trade['close_reason'], 'OPEN')}\n"
                f"Статус: {trade['status']}\n\n"
            )

        await update.message.reply_text(text)

    except Exception as error:
        await update.message.reply_text(
            f"❌ Ошибка истории Demo-сделок:\n{error}"
        )