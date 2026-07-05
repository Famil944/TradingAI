from paper.paper_engine import PaperEngine


class PaperController:

    def __init__(self):
        self.engine = PaperEngine()

    def on(self):
        return self.engine.turn_on()

    def off(self):
        return self.engine.turn_off()

    def status_text(self):
        status = self.engine.status()

        return (
            f"📄 Paper Trading\n\n"
            f"Статус: {status['mode']}\n"
            f"Баланс: {status['balance']} USDT\n"
            f"Открытая позиция: {'да' if status['has_position'] else 'нет'}\n"
            f"Сделок: {status['trades']}\n"
            f"Winrate: {status['winrate']}%"
        )

    def try_trade_text(self, signal_data):
        trade = self.engine.try_open_trade(signal_data)

        if trade is None:
            return None

        icon = "🟢" if trade["side"] == "LONG" else "🔴"

        return (
            f"📄 Paper Trade открыт\n\n"
            f"Монета: {trade['symbol']}\n"
            f"Сторона: {icon} {trade['side']}\n"
            f"Вход: {trade['entry_price']}\n"
            f"Take Profit: {trade['take_profit']}\n"
            f"Stop Loss: {trade['stop_loss']}\n"
            f"Размер: {trade['amount']}\n"
            f"Баланс: {trade['balance']} USDT"
        )

    def check_position_text(self, current_price):
        result = self.engine.check_position(current_price)

        if result is None:
            return None

        return (
            f"✅ Paper Trade закрыт\n\n"
            f"Монета: {result['symbol']}\n"
            f"Сторона: {result['side']}\n"
            f"Вход: {result['entry_price']}\n"
            f"Выход: {result['exit_price']}\n"
            f"Причина: {result['close_reason']}\n"
            f"Прибыль: {result['profit']} USDT\n"
            f"Баланс: {result['balance']} USDT"
        )