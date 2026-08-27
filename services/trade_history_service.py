from database.trade_history_repository import TradeHistoryRepository


class TradeHistoryService:

    def __init__(self):
        self.repository = TradeHistoryRepository()

    def get_last_trades(self, limit: int = 10):
        return self.repository.get_last_trades(limit)