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
            f"Открытая позиция: {'да' if status['has_position'] else 'нет'}"
        )

    def try_trade(self, signal_data):
        return self.engine.try_open_trade(signal_data)