import math
from datetime import datetime, timedelta, timezone


class TradingRiskGuard:
    def __init__(
        self,
        max_open_positions=3,
        min_reward_risk=1.2,
        max_daily_loss_percent=3,
        max_drawdown_percent=10,
        max_consecutive_losses=3,
        cooldown_minutes=15,
    ):
        self.max_open_positions = max_open_positions
        self.min_reward_risk = min_reward_risk
        self.max_daily_loss_percent = max_daily_loss_percent
        self.max_drawdown_percent = max_drawdown_percent
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown = timedelta(minutes=cooldown_minutes)

    def validate_market_state(self, open_positions, balance):
        if len(open_positions) >= self.max_open_positions:
            raise PermissionError(
                "Достигнут лимит одновременно открытых позиций"
            )
        if not math.isfinite(float(balance)) or float(balance) <= 0:
            raise ValueError("Доступный баланс должен быть больше нуля")

    def validate_trade(
        self,
        side,
        entry_price,
        stop_loss,
        take_profit,
        quantity,
    ):
        values = [entry_price, stop_loss, take_profit, quantity]
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("Параметры сделки должны быть конечными числами")
        if float(entry_price) <= 0 or float(quantity) <= 0:
            raise ValueError("Цена и количество должны быть больше нуля")

        if side == "LONG":
            risk = entry_price - stop_loss
            reward = take_profit - entry_price
        elif side == "SHORT":
            risk = stop_loss - entry_price
            reward = entry_price - take_profit
        else:
            raise ValueError(f"Неизвестная сторона: {side}")

        if risk <= 0 or reward <= 0:
            raise ValueError("Некорректное расположение Stop Loss или Take Profit")
        if reward / risk < self.min_reward_risk:
            raise PermissionError("Risk/reward ниже разрешённого минимума")

        return True

    def validate_history(self, trades, balance, symbol=None, now=None):
        now = now or datetime.now(timezone.utc)
        closed = [
            trade for trade in trades
            if trade.get("status") == "CLOSED"
            and trade.get("pnl") is not None
        ]

        today_pnl = 0.0
        for trade in closed:
            closed_at = self._parse_time(trade.get("closed_at"))
            if closed_at and closed_at.date() == now.date():
                today_pnl += float(trade["pnl"])
        max_daily_loss = float(balance) * (
            self.max_daily_loss_percent / 100
        )
        if today_pnl <= -max_daily_loss:
            raise PermissionError("Достигнут дневной лимит убытка")

        equity = peak = 0.0
        maximum_drawdown = 0.0
        for trade in reversed(closed):
            equity += float(trade["pnl"])
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, peak - equity)
        if maximum_drawdown >= float(balance) * (
            self.max_drawdown_percent / 100
        ):
            raise PermissionError("Достигнут лимит просадки")

        recent = closed[:self.max_consecutive_losses]
        if (
            len(recent) == self.max_consecutive_losses
            and all(float(trade["pnl"]) < 0 for trade in recent)
        ):
            raise PermissionError("Торговля остановлена после серии убытков")

        if symbol:
            last_symbol_trade = next(
                (trade for trade in closed if trade.get("symbol") == symbol),
                None,
            )
            if last_symbol_trade:
                closed_at = self._parse_time(
                    last_symbol_trade.get("closed_at")
                )
                if closed_at and now - closed_at < self.cooldown:
                    raise PermissionError(
                        f"Для {symbol} действует cooldown после сделки"
                    )

    @staticmethod
    def _parse_time(value):
        if not value:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
