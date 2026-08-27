from datetime import datetime, timezone

from exchange.trading_client_factory import create_trading_client
from services.demo_trade_log_service import DemoTradeLogService


class DemoTradeManager:

    def __init__(self, client=None, trade_log=None):
        self.client = client or create_trading_client()
        self.trade_log = trade_log or DemoTradeLogService()

    def open_long(self, symbol, quantity):
        print("🚀 Открываем LONG...")

        order = self.client.market_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
        )

        print("✅ LONG открыт")

        return order

    def open_short(self, symbol, quantity):
        print("📉 Открываем SHORT...")

        order = self.client.market_order(
            symbol=symbol,
            side="SELL",
            quantity=quantity,
        )

        print("✅ SHORT открыт")

        return order

    def position_quantity(self, symbol):
        positions = self.client.open_positions(symbol)
        if not positions:
            raise RuntimeError(f"Позиция {symbol} отсутствует")
        return abs(float(positions[0]["positionAmt"]))

    def replace_stop_loss(
        self,
        symbol,
        side,
        quantity,
        stop_price,
        previous_order_id=None,
    ):
        order = self.client.replace_stop_loss(
            symbol=symbol,
            position_side=side,
            quantity=quantity,
            stop_price=stop_price,
            previous_order_id=previous_order_id,
        )
        self.trade_log.update_protection(
            symbol,
            stop_loss=stop_price,
            stop_order_id=order["orderId"],
        )
        return order

    def finalize_exchange_closed_position(self, symbol):
        trade = self.trade_log.get_last_open_trade(symbol)
        if trade is None:
            return None

        filled = None
        close_reason = "EXCHANGE_CLOSE"
        for order_id, reason in (
            (trade.get("take_profit_order_id"), "TAKE_PROFIT"),
            (trade.get("stop_order_id"), "STOP_LOSS"),
        ):
            if not order_id:
                continue
            try:
                order = self.client.order_status(symbol, order_id)
            except Exception:
                continue
            if order.get("status") == "FILLED":
                filled = order
                close_reason = reason
                break

        exit_price = 0
        if filled:
            exit_price = float(
                filled.get("avgPrice")
                or filled.get("stopPrice")
                or filled.get("price")
                or 0
            )
        if exit_price <= 0:
            exit_price = float(self.client.price(symbol)["price"])

        entry_price = float(trade["entry_price"])
        quantity = float(trade["quantity"])
        if trade["side"] == "LONG":
            pnl = (exit_price - entry_price) * quantity
            pnl_percent = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - exit_price) * quantity
            pnl_percent = (entry_price - exit_price) / entry_price * 100

        commission, funding, realized_pnl = self._exchange_costs(
            symbol=symbol,
            trade=trade,
            close_order_id=filled.get("orderId") if filled else None,
        )
        if realized_pnl is not None:
            pnl = realized_pnl
        pnl = pnl + funding - commission

        self.client.cancel_all_orders(symbol)
        self.trade_log.close_trade(
            symbol=symbol,
            exit_price=exit_price,
            pnl=round(pnl, 4),
            pnl_percent=round(pnl_percent, 2),
            close_reason=close_reason,
            commission=round(commission, 8),
            funding=round(funding, 8),
        )
        return {
            "exit_price": exit_price,
            "pnl": round(pnl, 4),
            "pnl_percent": round(pnl_percent, 2),
            "close_reason": close_reason,
            "commission": round(commission, 8),
            "funding": round(funding, 8),
        }

    def _exchange_costs(self, symbol, trade, close_order_id=None):
        order_ids = {
            str(value)
            for value in (
                trade.get("entry_order_id"),
                close_order_id,
            )
            if value is not None
        }
        commission = 0.0
        realized_pnl = 0.0
        has_realized = False
        try:
            account_trades = self.client.account_trades(symbol, limit=1000)
            for item in account_trades:
                if str(item.get("orderId")) not in order_ids:
                    continue
                commission += abs(float(item.get("commission", 0)))
                if close_order_id is not None and (
                    str(item.get("orderId")) == str(close_order_id)
                ):
                    realized_pnl += float(item.get("realizedPnl", 0))
                    has_realized = True
        except Exception:
            pass

        funding = 0.0
        try:
            created_at = trade.get("created_at")
            start_time = None
            if created_at:
                parsed = datetime.fromisoformat(str(created_at))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                start_time = int(parsed.timestamp() * 1000)
            kwargs = {
                "symbol": symbol,
                "incomeType": "FUNDING_FEE",
                "limit": 1000,
            }
            if start_time is not None:
                kwargs["startTime"] = start_time
            funding = sum(
                float(item.get("income", 0))
                for item in self.client.income_history(**kwargs)
            )
        except Exception:
            pass

        return (
            commission,
            funding,
            realized_pnl if has_realized else None,
        )

    def check_break_even(
        self,
        symbol,
        entry_price,
        side,
        trigger_percent=0.5,
    ):
        positions = self.client.open_positions(symbol)

        if not positions:
            return {
                "move_stop": False,
                "reason": "Позиции нет.",
            }

        price = float(self.client.price(symbol)["price"])
        position = positions[0]
        amount = float(position["positionAmt"])

        if side == "LONG" and amount > 0:
            profit_percent = (
                (price - entry_price) / entry_price
            ) * 100

        elif side == "SHORT" and amount < 0:
            profit_percent = (
                (entry_price - price) / entry_price
            ) * 100

        else:
            return {
                "move_stop": False,
                "reason": (
                    f"Несоответствие стороны позиции: "
                    f"side={side}, amount={amount}"
                ),
            }

        if profit_percent >= trigger_percent:
            return {
                "move_stop": True,
                "new_stop_loss": entry_price,
                "profit_percent": round(profit_percent, 2),
            }

        return {
            "move_stop": False,
            "profit_percent": round(profit_percent, 2),
        }

    def calculate_trailing_stop(
        self,
        symbol,
        entry_price,
        current_stop_loss,
        side,
        trigger_percent=0.5,
        distance_percent=0.3,
    ):
        positions = self.client.open_positions(symbol)

        if not positions:
            return {
                "updated": False,
                "reason": "Позиции нет.",
            }

        price = float(self.client.price(symbol)["price"])
        position = positions[0]
        amount = float(position["positionAmt"])

        if side == "LONG" and amount > 0:
            profit_percent = (
                (price - entry_price) / entry_price
            ) * 100

            if profit_percent < trigger_percent:
                return {
                    "updated": False,
                    "reason": "Прибыль ещё мала для trailing stop.",
                    "profit_percent": round(profit_percent, 2),
                }

            new_stop_loss = round(
                price * (1 - distance_percent / 100),
                1,
            )

            if new_stop_loss > current_stop_loss:
                return {
                    "updated": True,
                    "new_stop_loss": new_stop_loss,
                    "profit_percent": round(profit_percent, 2),
                }

        elif side == "SHORT" and amount < 0:
            profit_percent = (
                (entry_price - price) / entry_price
            ) * 100

            if profit_percent < trigger_percent:
                return {
                    "updated": False,
                    "reason": "Прибыль ещё мала для trailing stop.",
                    "profit_percent": round(profit_percent, 2),
                }

            new_stop_loss = round(
                price * (1 + distance_percent / 100),
                1,
            )

            if new_stop_loss < current_stop_loss:
                return {
                    "updated": True,
                    "new_stop_loss": new_stop_loss,
                    "profit_percent": round(profit_percent, 2),
                }

        else:
            return {
                "updated": False,
                "reason": (
                    f"Несоответствие стороны позиции: "
                    f"side={side}, amount={amount}"
                ),
            }

        return {
            "updated": False,
            "reason": "Trailing stop не обновлён.",
        }

    def close_position(
        self,
        symbol,
        exit_price,
        close_reason,
    ):
        positions = self.client.open_positions(symbol)

        if not positions:
            print("Открытых позиций нет.")
            return None

        trade = self.trade_log.get_last_open_trade(symbol)

        if trade is None:
            print("❌ Открытая сделка не найдена в базе.")
            return None

        position = positions[0]
        amount = float(position["positionAmt"])
        quantity = abs(amount)

        if amount > 0:
            close_side = "SELL"
        else:
            close_side = "BUY"

        result = self.client.close_position_market(
            symbol=symbol,
            side=close_side,
            quantity=quantity,
        )

        self.client.cancel_all_orders(symbol)

        entry_price = float(trade["entry_price"])
        logged_quantity = float(trade["quantity"])
        side = trade["side"]

        if side == "LONG":
            pnl = (
                float(exit_price) - entry_price
            ) * logged_quantity
            pnl_percent = (
                (float(exit_price) - entry_price)
                / entry_price
            ) * 100

        elif side == "SHORT":
            pnl = (
                entry_price - float(exit_price)
            ) * logged_quantity

            pnl_percent = (
                (entry_price - float(exit_price))
                / entry_price
            ) * 100

        else:
            pnl = 0
            pnl_percent = 0

        pnl = round(pnl, 4)
        pnl_percent = round(pnl_percent, 2)

        self.trade_log.close_trade(
            symbol=symbol,
            exit_price=float(exit_price),
            pnl=pnl,
            pnl_percent=pnl_percent,
            close_reason=close_reason,
        )

        print("✅ Позиция закрыта")
        print(f"Цена входа: {entry_price}")
        print(f"Цена выхода: {exit_price}")
        print(f"PnL: {pnl} USDT")
        print(f"PnL: {pnl_percent}%")
        print(f"Причина: {close_reason}")

        return {
            "order": result,
            "entry_price": entry_price,
            "exit_price": float(exit_price),
            "pnl": pnl,
            "pnl_percent": pnl_percent,
            "close_reason": close_reason,
        }

    def check_tp_sl(
        self,
        symbol,
        side,
        take_profit,
        stop_loss,
    ):
        positions = self.client.open_positions(symbol)

        if not positions:
            return "Позиции нет."

        price = float(self.client.price(symbol)["price"])

        position = positions[0]
        amount = float(position["positionAmt"])

        print(f"Цена: {price}")
        print(f"Сторона: {side}")
        print(f"TP: {take_profit}")
        print(f"SL: {stop_loss}")

        if side == "LONG" and amount > 0:
            if price >= take_profit:
                close_result = self.close_position(
                    symbol=symbol,
                    exit_price=price,
                    close_reason="TAKE_PROFIT",
                )

                if close_result:
                    return (
                        f"🎯 Take Profit сработал\n"
                        f"PnL: {close_result['pnl']} USDT\n"
                        f"PnL: {close_result['pnl_percent']}%"
                    )

            if price <= stop_loss:
                close_result = self.close_position(
                    symbol=symbol,
                    exit_price=price,
                    close_reason="STOP_LOSS",
            )

                if close_result:
                    return (
                        f"🛑 Stop Loss сработал\n"
                        f"PnL: {close_result['pnl']} USDT\n"
                        f"PnL: {close_result['pnl_percent']}%"
                    )

        elif side == "SHORT" and amount < 0:
            if price <= take_profit:
                close_result = self.close_position(
                    symbol=symbol,
                    exit_price=price,
                    close_reason="TAKE_PROFIT",
                )

                if close_result:
                    return (
                        f"🎯 Take Profit сработал\n"
                        f"PnL: {close_result['pnl']} USDT\n"
                        f"PnL: {close_result['pnl_percent']}%"
                    )

            if price >= stop_loss:
                close_result = self.close_position(
                    symbol=symbol,
                    exit_price=price,
                    close_reason="STOP_LOSS",
                )

                if close_result:
                    return (
                        f"🛑 Stop Loss сработал\n"
                        f"PnL: {close_result['pnl']} USDT\n"
                        f"PnL: {close_result['pnl_percent']}%"
                    )

        else:
            return (
                f"⚠️ Несоответствие позиции: "
                f"ожидалась сторона {side}, amount={amount}"
            )

        return "⏳ Позиция удерживается"
