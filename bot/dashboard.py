import config.trading_mode as trading_mode


def build_dashboard(paper, auto_state):
    status = paper.engine.status()
    position = paper.engine.trader.position

    auto_text = "🟢 включён" if auto_state.enabled else "🔴 выключен"
    paper_text = "🟢 включён" if status["enabled"] else "🔴 выключен"

    if position:
        position_text = f"🟢 {position.symbol} {position.side}"
    else:
        position_text = "⚪ нет"

    return (
        "🤖 Trading AI v5\n\n"
        "🟢 Статус: онлайн\n"
        f"🔁 Счёт: {trading_mode.CURRENT_MODE.value}\n"
        f"🤖 Авто: {auto_text}\n"
        f"💼 Paper: {paper_text}\n"
        f"📌 Позиция: {position_text}\n"
        f"💰 Баланс: {status['balance']} USDT\n"
        f"📊 Сделок: {status['trades']}\n"
        f"🏆 Winrate: {status['winrate']}%\n\n"
        "Выберите действие:"
    )
