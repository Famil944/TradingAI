from datetime import datetime, timedelta, timezone

from database.demo_trade_repository import DemoTradeRepository


class DemoStatisticsService:

    def __init__(self):
        self.repository = DemoTradeRepository()

    def get_all_trades(self):
        with self.repository.db.connect() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    id,
                    symbol,
                    side,
                    entry_price,
                    quantity,
                    take_profit,
                    stop_loss,
                    status,
                    exit_price,
                    pnl,
                    pnl_percent,
                    close_reason,
                    created_at,
                    closed_at,
                    trading_mode
                FROM demo_trades
                ORDER BY id ASC
            """)

            rows = cursor.fetchall()

            trades = []

            for row in rows:
                trades.append({
                    "id": row[0],
                    "symbol": row[1],
                    "side": row[2],
                    "entry_price": row[3],
                    "quantity": row[4],
                    "take_profit": row[5],
                    "stop_loss": row[6],
                    "status": row[7],
                    "exit_price": row[8],
                    "pnl": row[9],
                    "pnl_percent": row[10],
                    "close_reason": row[11],
                    "created_at": row[12],
                    "closed_at": row[13],
                    "trading_mode": row[14] or "DEMO",
                })

            return trades

    def get_statistics(self, days=None, trading_mode=None):
        trades = self.get_all_trades()
        if trading_mode:
            trades = [
                trade for trade in trades
                if trade["trading_mode"] == trading_mode
            ]
        if days is not None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            trades = [
                trade for trade in trades
                if self._parse_time(
                    trade["closed_at"] or trade["created_at"]
                ) >= since
            ]

        total_trades = len(trades)

        open_trades = [
            trade for trade in trades
            if trade["status"] == "OPEN"
        ]

        closed_trades = [
            trade for trade in trades
            if trade["status"] == "CLOSED"
        ]

        winning_trades = [
            trade for trade in closed_trades
            if trade["pnl"] is not None
            and float(trade["pnl"]) > 0
        ]

        losing_trades = [
            trade for trade in closed_trades
            if trade["pnl"] is not None
            and float(trade["pnl"]) < 0
        ]

        break_even_trades = [
            trade for trade in closed_trades
            if trade["pnl"] is not None
            and float(trade["pnl"]) == 0
        ]

        closed_count = len(closed_trades)
        winning_count = len(winning_trades)
        losing_count = len(losing_trades)
        break_even_count = len(break_even_trades)

        if closed_count > 0:
            win_rate = (
                winning_count / closed_count
            ) * 100
        else:
            win_rate = 0

        pnl_values = [
            float(trade["pnl"])
            for trade in closed_trades
            if trade["pnl"] is not None
        ]

        total_pnl = sum(pnl_values)

        if pnl_values:
            average_pnl = total_pnl / len(pnl_values)
        else:
            average_pnl = 0

        maximum_drawdown = self.calculate_max_drawdown(
            closed_trades
        )

        return {
            "total_trades": total_trades,
            "open_trades": len(open_trades),
            "closed_trades": closed_count,
            "winning_trades": winning_count,
            "losing_trades": losing_count,
            "break_even_trades": break_even_count,
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 4),
            "average_pnl": round(average_pnl, 4),
            "maximum_drawdown": round(maximum_drawdown, 4),
            "best_trade": round(max(pnl_values, default=0), 4),
            "worst_trade": round(min(pnl_values, default=0), 4),
        }

    @staticmethod
    def _parse_time(value):
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def calculate_max_drawdown(self, closed_trades):
        balance_curve = 0
        highest_balance = 0
        maximum_drawdown = 0

        for trade in closed_trades:
            if trade["pnl"] is None:
                continue

            balance_curve += float(trade["pnl"])

            if balance_curve > highest_balance:
                highest_balance = balance_curve

            drawdown = highest_balance - balance_curve
            if drawdown > maximum_drawdown:
                maximum_drawdown = drawdown

        return maximum_drawdown

    def get_total_trades(self):
        statistics = self.get_statistics()
        return statistics["total_trades"]
