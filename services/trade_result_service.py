from database.trade_result_repository import TradeResultRepository


class TradeResultService:

    def __init__(self):
        self.repository = TradeResultRepository()

    def save_trade(
        self,
        symbol,
        side,
        entry_price,
        exit_price,
        profit,
        profit_percent,
        close_reason,
        hold_minutes,
    ):
        self.repository.save({
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "profit": profit,
            "profit_percent": profit_percent,
            "close_reason": close_reason,
            "hold_minutes": hold_minutes,
        })