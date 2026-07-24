import threading
import config.trading_mode as trading_mode
from indicators.market_analyzer import MarketAnalyzer
from exchange.trading_client_factory import create_trading_client
from services.demo_trade_manager import DemoTradeManager
from services.demo_trade_monitor import DemoTradeMonitor
from services.demo_position_sizer import DemoPositionSizer
from services.demo_trade_log_service import DemoTradeLogService
from services.trading_risk_guard import TradingRiskGuard
from services.app_settings import AppSettings



class DemoTradingController:

    def __init__(
        self,
        client=None,
        trade_log=None,
        analyzer=None,
        monitor_factory=DemoTradeMonitor,
    ):
        self.client = client or create_trading_client()
        self.trade_log = trade_log or DemoTradeLogService()
        self.manager = DemoTradeManager(self.client, self.trade_log)
        self.active_monitors = {}
        self.settings = AppSettings()
        configured_max_quantity = float(
            self.settings.get("max_quantity")
        )
        if self.settings.get("auto_risk") == "true":
            configured_max_quantity = None
        self.sizer = DemoPositionSizer(
            risk_percent=float(self.settings.get("risk_percent")),
            max_quantity=configured_max_quantity,
            max_balance_percent=float(
                self.settings.get("max_balance_percent")
            ),
        )
        self.analyzer = analyzer or MarketAnalyzer()
        self.monitor_factory = monitor_factory
        self.risk_guard = TradingRiskGuard()

    def set_client(self, client):
        """Replace the exchange client after confirming there are no positions."""
        for monitor in self.active_monitors.values():
            monitor.stop()
        self.active_monitors.clear()
        self.client = client
        self.manager = DemoTradeManager(self.client, self.trade_log)

    def _remove_monitor(self, symbol, monitor):
        if self.active_monitors.get(symbol) is monitor:
            self.active_monitors.pop(symbol, None)

    def has_open_position(self, symbol=None):
        positions = self.client.open_positions(symbol)
        return len(positions) > 0

    def open_demo_trade(self, signal_data):
        configured_max_quantity = float(
            self.settings.get("max_quantity")
        )
        if self.settings.get("auto_risk") == "true":
            configured_max_quantity = None
        self.sizer = DemoPositionSizer(
            risk_percent=float(self.settings.get("risk_percent")),
            max_quantity=configured_max_quantity,
            max_balance_percent=float(
                self.settings.get("max_balance_percent")
            ),
        )
        symbol = signal_data["symbol"]
        signal = signal_data["signal"]

        if self.has_open_position(symbol):
            return "⚠️ Уже есть открытая Demo-позиция."

        all_positions = self.client.open_positions()
        price = float(self.client.price(symbol)["price"])
        klines = self.client.klines(symbol, "1h", 250)
        analysis = self.analyzer.analyze(klines)
        atr = analysis["atr"]
        account = self.client.account()
        balance = 0
        for asset in account["assets"]:
            if asset["asset"] == "USDT":
                balance = float(asset["availableBalance"])
                break

        self.risk_guard.validate_market_state(all_positions, balance)
        self.risk_guard.validate_history(
            self.trade_log.get_recent_trades(limit=100),
            balance=balance,
            symbol=symbol,
        )

        if signal == "🟢 LONG":
            take_profit = round(price + atr * 2.5, 1)
            stop_loss = round(price - atr * 1.5, 1)
            quantity = self.sizer.calculate_long_quantity(
                balance=balance,
                entry_price=price,
                stop_loss=stop_loss,
                quality_score=signal_data.get("quality_score", 0),
            )

            if quantity <= 0:
                return "❌ Не удалось рассчитать размер позиции."
            side = "LONG"

        elif signal == "🔴 SHORT":

            take_profit = round(price - atr * 2.5, 1)
            stop_loss = round(price + atr * 1.5, 1)

            quantity = self.sizer.calculate_short_quantity(
                balance=balance,
                entry_price=price,
                stop_loss=stop_loss,
                quality_score=signal_data.get("quality_score", 0),
            )

            if quantity <= 0:
                return "❌ Не удалось рассчитать размер позиции."
            side = "SHORT"

        else:
            return "⚠️ Неизвестный торговый сигнал."

        self.risk_guard.validate_trade(
            side=side,
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=quantity,
        )

        order = (
            self.manager.open_long(symbol, quantity)
            if side == "LONG"
            else self.manager.open_short(symbol, quantity)
        )
        if not order or order.get("blocked"):
            raise RuntimeError(f"Биржа не подтвердила открытие ордера: {order}")

        executed_quantity = float(
            order.get("executedQty") or order.get("origQty") or quantity
        )
        actual_entry_price = float(order.get("avgPrice") or 0) or price

        try:
            protection = self.client.place_protective_orders(
                symbol=symbol,
                position_side=side,
                quantity=executed_quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            stop_order_id = protection["stop_order"]["orderId"]
            take_profit_order_id = protection[
                "take_profit_order"
            ]["orderId"]

            self.trade_log.save_open_trade(
                symbol=symbol,
                side=side,
                entry_price=actual_entry_price,
                quantity=executed_quantity,
                take_profit=take_profit,
                stop_loss=stop_loss,
                strategy=signal_data.get("strategy", "AI Strategy"),
                quality_score=signal_data.get("quality_score", 0),
                atr=analysis["atr"],
                trading_mode=trading_mode.CURRENT_MODE.value,
                entry_order_id=order.get("orderId"),
                stop_order_id=stop_order_id,
                take_profit_order_id=take_profit_order_id,
            )
        except Exception:
            try:
                self.client.cancel_all_orders(symbol)
            except Exception:
                pass
            close_side = "SELL" if side == "LONG" else "BUY"
            self.client.close_position_market(
                symbol=symbol,
                side=close_side,
                quantity=executed_quantity,
            )
            raise

        monitor = self.monitor_factory(
            symbol=symbol,
            entry_price=actual_entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            side=side,
            interval_seconds=5,
            manager=self.manager,
            on_stop=self._remove_monitor,
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
        )

        self.active_monitors[symbol] = monitor

        threading.Thread(
            target=monitor.start,
            daemon=True,
        ).start()

        return (
            f"🚀 {trading_mode.CURRENT_MODE.value}-сделка открыта\n\n"
            f"Монета: {symbol}\n"
            f"Сторона: {side}\n"
            f"Вход: {actual_entry_price}\n"
            f"TP: {take_profit}\n"
            f"SL: {stop_loss}\n"
            f"Количество: {executed_quantity}\n"
            f"Авто-риск: "
            f"{self.sizer.adaptive_risk_percent(signal_data.get('quality_score', 0)):.2f}%"
        )

    def restore_open_trades(self):
        restored = []
        skipped = []

        open_trades = self.trade_log.get_open_trades()

        for trade in open_trades:
            symbol = trade["symbol"]
            side = trade["side"]

            positions = self.client.open_positions(symbol)

            if not positions:
                finalized = (
                    self.manager.finalize_exchange_closed_position(symbol)
                )
                if finalized:
                    skipped.append(
                        f"{symbol}: закрытие синхронизировано с Binance"
                    )
                    continue
                reason = "Позиция отсутствует на Binance при восстановлении"
                self.trade_log.mark_orphaned(symbol, reason)
                skipped.append(
                    f"{symbol}: {reason}; запись помечена ORPHANED"
                )
                continue

            position_amount = float(
                positions[0]["positionAmt"]
            )

            side_matches = (
                side == "LONG" and position_amount > 0
            ) or (
                side == "SHORT" and position_amount < 0
            )

            if not side_matches:
                reason = "Сторона позиции не совпадает с Binance"
                self.trade_log.mark_orphaned(symbol, reason)
                skipped.append(
                    f"{symbol}: {reason}; запись помечена ORPHANED"
                )
                continue

            if symbol in self.active_monitors:
                skipped.append(
                    f"{symbol}: монитор уже запущен"
                )
                continue

            self._ensure_protection(trade, abs(position_amount))

            monitor = self.monitor_factory(
                symbol=symbol,
                entry_price=float(trade["entry_price"]),
                take_profit=float(trade["take_profit"]),
                stop_loss=float(trade["stop_loss"]),
                side=side,
                interval_seconds=5,
                manager=self.manager,
                on_stop=self._remove_monitor,
                stop_order_id=trade.get("stop_order_id"),
                take_profit_order_id=trade.get("take_profit_order_id"),
            )

            self.active_monitors[symbol] = monitor

            threading.Thread(
                target=monitor.start,
                daemon=True,
            ).start()

            restored.append(symbol)

        if restored:
            print(
                "✅ Восстановлен мониторинг Demo-позиций: "
                + ", ".join(restored)
            )
        else:
            print("📭 Demo-позиции для восстановления не найдены.")

        for message in skipped:
            print(f"⚠️ {message}")

        return {
            "restored": restored,
            "skipped": skipped,
        }

    def _ensure_protection(self, trade, quantity):
        symbol = trade["symbol"]
        orders = self.client.open_orders(symbol)
        open_ids = {str(order["orderId"]) for order in orders}
        stop_order_id = trade.get("stop_order_id")
        take_profit_order_id = trade.get("take_profit_order_id")

        if not stop_order_id or str(stop_order_id) not in open_ids:
            stop_order = self.client.stop_market_order(
                symbol=symbol,
                side="SELL" if trade["side"] == "LONG" else "BUY",
                quantity=quantity,
                stop_price=trade["stop_loss"],
            )
            stop_order_id = stop_order["orderId"]

        if (
            not take_profit_order_id
            or str(take_profit_order_id) not in open_ids
        ):
            take_profit_order = self.client.take_profit_market_order(
                symbol=symbol,
                side="SELL" if trade["side"] == "LONG" else "BUY",
                quantity=quantity,
                take_profit_price=trade["take_profit"],
            )
            take_profit_order_id = take_profit_order["orderId"]

        self.trade_log.update_protection(
            symbol,
            stop_order_id=stop_order_id,
            take_profit_order_id=take_profit_order_id,
            )
