import threading
import time

from exchange.order_validation import validate_order
from exchange.symbol_rules import SymbolRules, decimal_to_api


class BinanceFuturesClient:
    """Shared order behavior for Binance Futures Demo and Live."""

    def __init__(self, client, order_access_check):
        self.client = client
        self._order_access_check = order_access_check
        self._rules = {}
        self._rules_lock = threading.Lock()
        self._rules_loaded_at = 0.0

    def _check_order_access(self):
        self._order_access_check()

    def _check_new_position_access(self):
        self._check_order_access()
        from config.trading_mode import NEW_POSITIONS_ENABLED

        if not NEW_POSITIONS_ENABLED:
            raise PermissionError(
                "Включён CLOSE_ONLY: новые позиции запрещены"
            )

    def ping(self):
        return self.client.ping()

    def server_time(self):
        return self.client.time()

    def account(self):
        return self.client.account()

    def price(self, symbol="BTCUSDT"):
        return self.client.ticker_price(symbol=symbol)

    def klines(self, symbol="BTCUSDT", interval="1h", limit=250):
        return self.client.klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )

    def refresh_symbol_rules(self):
        data = self.client.exchange_info()
        rules = {
            item["symbol"]: SymbolRules.from_exchange_symbol(item)
            for item in data["symbols"]
            if item.get("status") == "TRADING"
        }
        with self._rules_lock:
            self._rules = rules
            self._rules_loaded_at = time.monotonic()
        return rules

    def symbol_rules(self, symbol):
        symbol = str(symbol).upper()
        with self._rules_lock:
            stale = time.monotonic() - self._rules_loaded_at > 3600
            rules = self._rules.get(symbol)
        if rules is None or stale:
            rules = self.refresh_symbol_rules().get(symbol)
        if rules is None:
            raise ValueError(f"Binance не поддерживает символ {symbol}")
        return rules

    def normalize_order(self, symbol, side, quantity, reference_price=None):
        symbol, side, quantity = validate_order(symbol, side, quantity)
        rules = self.symbol_rules(symbol)
        normalized = rules.normalize_quantity(quantity)
        if reference_price is not None:
            rules.validate_notional(normalized, reference_price)
        return symbol, side, decimal_to_api(normalized)

    def market_order(self, symbol, side, quantity):
        self._check_new_position_access()
        reference_price = self.price(symbol)["price"]
        symbol, side, quantity = self.normalize_order(
            symbol, side, quantity, reference_price
        )
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            newOrderRespType="RESULT",
        )

    def stop_market_order(self, symbol, side, quantity, stop_price):
        self._check_order_access()
        symbol, side, quantity = self.normalize_order(
            symbol, side, quantity
        )
        rules = self.symbol_rules(symbol)
        stop_price = decimal_to_api(
            rules.normalize_stop_price(stop_price, side)
        )
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            quantity=quantity,
            reduceOnly="true",
            workingType="MARK_PRICE",
            priceProtect="true",
        )

    def take_profit_market_order(
        self,
        symbol,
        side,
        quantity,
        take_profit_price,
    ):
        self._check_order_access()
        symbol, side, quantity = self.normalize_order(
            symbol, side, quantity
        )
        rules = self.symbol_rules(symbol)
        take_profit_price = decimal_to_api(
            rules.normalize_stop_price(take_profit_price, side)
        )
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="TAKE_PROFIT_MARKET",
            stopPrice=take_profit_price,
            quantity=quantity,
            reduceOnly="true",
            workingType="MARK_PRICE",
            priceProtect="true",
        )

    def place_protective_orders(
        self,
        symbol,
        position_side,
        quantity,
        stop_loss,
        take_profit,
    ):
        close_side = "SELL" if position_side == "LONG" else "BUY"
        stop_order = self.stop_market_order(
            symbol, close_side, quantity, stop_loss
        )
        try:
            take_profit_order = self.take_profit_market_order(
                symbol, close_side, quantity, take_profit
            )
        except Exception:
            self.cancel_order(symbol, stop_order["orderId"])
            raise
        return {
            "stop_order": stop_order,
            "take_profit_order": take_profit_order,
        }

    def replace_stop_loss(
        self,
        symbol,
        position_side,
        quantity,
        stop_price,
        previous_order_id=None,
    ):
        close_side = "SELL" if position_side == "LONG" else "BUY"
        new_order = self.stop_market_order(
            symbol, close_side, quantity, stop_price
        )
        if previous_order_id:
            try:
                self.cancel_order(symbol, previous_order_id)
            except Exception:
                # The new stop already protects the position. Reconciliation
                # can remove a duplicate stop later.
                pass
        return new_order

    def order_status(self, symbol, order_id):
        return self.client.query_order(symbol=symbol, orderId=order_id)

    def open_orders(self, symbol=None):
        return (
            self.client.get_open_orders(symbol=symbol)
            if symbol
            else self.client.get_open_orders()
        )

    def cancel_order(self, symbol, order_id):
        self._check_order_access()
        return self.client.cancel_order(symbol=symbol, orderId=order_id)

    def cancel_all_orders(self, symbol):
        self._check_order_access()
        return self.client.cancel_open_orders(symbol=symbol)

    def open_positions(self, symbol=None):
        data = (
            self.client.get_position_risk(symbol=symbol)
            if symbol
            else self.client.get_position_risk()
        )
        return [
            item for item in data
            if float(item["positionAmt"]) != 0
        ]

    def close_position_market(self, symbol, side, quantity):
        self._check_order_access()
        symbol, side, quantity = self.normalize_order(
            symbol, side, quantity
        )
        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
            reduceOnly="true",
            newOrderRespType="RESULT",
        )

    def close_partial_position_market(self, symbol, side, quantity):
        close_side = "SELL" if side == "BUY" else "BUY"
        return self.close_position_market(symbol, close_side, quantity)

    def set_leverage(self, symbol, leverage):
        self._check_order_access()
        leverage = int(leverage)
        if not 1 <= leverage <= 20:
            raise ValueError("Разрешено плечо только от 1 до 20")
        return self.client.change_leverage(
            symbol=symbol,
            leverage=leverage,
        )

    def account_trades(self, symbol, **kwargs):
        return self.client.get_account_trades(symbol=symbol, **kwargs)

    def income_history(self, **kwargs):
        return self.client.get_income_history(**kwargs)

    def health_check(self):
        self.ping()
        server_time = self.server_time()
        account = self.account()
        position_mode = self.client.get_position_mode()
        dual_side = position_mode.get("dualSidePosition")
        if dual_side is True or str(dual_side).lower() == "true":
            raise RuntimeError(
                "Hedge Mode не поддерживается. Включите Binance One-way Mode."
            )
        if account.get("canTrade") is False:
            raise PermissionError("Binance сообщает canTrade=false")
        server_timestamp = int(server_time.get("serverTime", 0))
        drift_ms = abs(int(time.time() * 1000) - server_timestamp)
        if server_timestamp and drift_ms > 5000:
            raise RuntimeError(
                f"Системное время отличается от Binance на {drift_ms} мс"
            )
        return {
            "server_time": server_timestamp,
            "clock_drift_ms": drift_ms,
            "can_trade": account.get("canTrade"),
            "position_mode": "ONE_WAY",
        }
