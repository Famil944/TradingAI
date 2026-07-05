from database.trade_repository import TradeRepository


class TradeService:

    def __init__(self):
        self.repository = TradeRepository()

    def save_trade(self, trade: dict):
        try:
            self.repository.save_trade(trade)
            return True
        except Exception as e:
            print(f"Ошибка сохранения Paper-сделки: {e}")
            return False