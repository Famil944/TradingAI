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
        print(">>> try_trade_text вызван")
        try:
            trade = self.engine.try_open_trade(signal_data)
            print(">>> trade =", trade)
        except Exception as e:
            print(">>> ERROR:", e)
            raise

        if trade is None:
            return None

        return (
            f"📄 Paper Trade открыт\n\n"
            f"Монета: {trade['symbol']}\n"
            f"Сторона: {trade['side']}\n"
            f"Вход: {trade['entry_price']}\n"
            f"Take Profit: {trade['take_profit']}\n"
            f"Stop Loss: {trade['stop_loss']}\n"
            f"Баланс: {trade['balance']} USDT"
        )

    def check_position_text(self, current_price):
        result = self.engine.check_position(current_price)

        if result is None:
            return None

        return (
            f"✅ Paper Trade закрыт\n\n"
            f"Монета: {result['symbol']}\n"
            f"Причина: {result['close_reason']}\n"
            f"Прибыль: {result['profit']} USDT\n"
            f"Баланс: {result['balance']} USDT"
        )