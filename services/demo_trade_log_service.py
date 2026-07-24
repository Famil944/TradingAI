from database.demo_trade_repository import DemoTradeRepository


class DemoTradeLogService:

    def __init__(self):
        self.repository = DemoTradeRepository()

    def save_open_trade(
        self,
        symbol,
        side,
        entry_price,
        quantity,
        take_profit,
        stop_loss,
        strategy="Unknown",
        quality_score=0,
        atr=0,
        trading_mode="DEMO",
        entry_order_id=None,
        stop_order_id=None,
        take_profit_order_id=None,
        commission=0,
        funding=0,
    ):
        self.repository.save_open_trade({
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "quantity": quantity,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "strategy": strategy,
            "quality_score": quality_score,
            "atr": atr,
            "trading_mode": trading_mode,
            "entry_order_id": entry_order_id,
            "stop_order_id": stop_order_id,
            "take_profit_order_id": take_profit_order_id,
            "commission": commission,
            "funding": funding,
            "status": "OPEN",
        })

    def get_last_open_trade(self, symbol):
        return self.repository.get_last_open_trade(symbol)

    def close_trade(
        self,
        symbol,
        exit_price,
        pnl,
        pnl_percent,
        close_reason,
        commission=0,
        funding=0,
    ):
        self.repository.close_last_open_trade(
            symbol=symbol,
            exit_price=exit_price,
            pnl=pnl,
            pnl_percent=pnl_percent,
            close_reason=close_reason,
            status="CLOSED",
            commission=commission,
            funding=funding,
        )

    def delete_all_trades(self):
        self.repository.delete_all_trades()

    def get_recent_trades(self, limit=10):
        return self.repository.get_recent_trades(limit)

    def get_open_trades(self):
        return self.repository.get_open_trades()

    def mark_orphaned(self, symbol, reason):
        self.repository.mark_last_open_trade(
            symbol=symbol,
            status="ORPHANED",
            close_reason=reason,
        )

    def update_protection(self, symbol, **kwargs):
        self.repository.update_protection(symbol, **kwargs)
