import config.trading_mode as trading_mode
from services.demo_statistics_service import DemoStatisticsService


def _usdt_balance(account):
    for asset in account.get("assets", []):
        if asset.get("asset") == "USDT":
            for field in ("availableBalance", "walletBalance", "balance"):
                if asset.get(field) is not None:
                    return float(asset[field])
    for field in ("availableBalance", "totalWalletBalance"):
        if account.get(field) is not None:
            return float(account[field])
    return 0.0


def build_dashboard(paper, auto_state, controller=None, statistics=None):
    paper_status = paper.engine.status()

    auto_text = "🟢 включён" if auto_state.enabled else "🔴 выключен"
    paper_text = "🟢 включён" if paper_status["enabled"] else "🔴 выключен"

    if controller is None:
        position = paper.engine.trader.position
        position_text = (
            f"🟢 {position.symbol} {position.side}"
            if position
            else "⚪ нет"
        )
        balance = paper_status["balance"]
        trades = paper_status["trades"]
        winrate = paper_status["winrate"]
        unrealized_pnl = 0.0
    else:
        positions = controller.client.open_positions()
        if positions:
            labels = []
            for position in positions:
                amount = float(position.get("positionAmt", 0))
                side = "LONG" if amount > 0 else "SHORT"
                labels.append(f"{position.get('symbol', '?')} {side}")
            position_text = f"{len(positions)} — {', '.join(labels)}"
        else:
            position_text = "⚪ нет"
        unrealized_pnl = sum(
            float(position.get("unRealizedProfit", 0) or 0)
            for position in positions
        )

        balance = _usdt_balance(controller.client.account())
        statistics = statistics or DemoStatisticsService()
        stats = statistics.get_statistics(
            trading_mode=trading_mode.CURRENT_MODE.value,
        )
        trades = stats["total_trades"]
        winrate = stats["win_rate"]
    last_scan = auto_state.settings.get("last_scan_at") or "ещё не было"

    return (
        "🤖 Trading AI v5\n\n"
        "🟢 Статус: онлайн\n"
        f"🔁 Счёт: {trading_mode.CURRENT_MODE.value}\n"
        f"🤖 Авто: {auto_text}\n"
        f"💼 Paper: {paper_text}\n"
        f"📌 Позиции: {position_text}\n"
        f"💰 Доступно: {balance:.2f} USDT\n"
        f"📈 Текущий PnL: {unrealized_pnl:.4f} USDT\n"
        f"📊 Сделок: {trades}\n"
        f"🏆 Winrate: {winrate}%\n\n"
        f"🕒 Последний анализ: {last_scan}\n\n"
        "Выберите действие:"
    )
