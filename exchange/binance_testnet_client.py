import os
from pathlib import Path

from dotenv import load_dotenv
from binance.um_futures import UMFutures

from config.trading_mode import CURRENT_MODE, TradingMode


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class BinanceTestnetClient:

    def __init__(self):
        self.api_key = os.getenv("BINANCE_TESTNET_API_KEY")
        self.api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")

        self.client = UMFutures(
            key=self.api_key,
            secret=self.api_secret,
            base_url="https://demo-fapi.binance.com",
        )

    def ping(self):
        return self.client.ping()

    def account(self):
        return self.client.account()

    def price(self, symbol="BTCUSDT"):
        return self.client.ticker_price(symbol=symbol)

    def market_order(self, symbol, side, quantity):
        if CURRENT_MODE != TradingMode.DEMO:
            return {
                "blocked": True,
                "reason": f"Текущий режим: {CURRENT_MODE.value}. Ордер запрещён.",
            }

        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
        )

    def stop_market_order(self, symbol, side, quantity, stop_price):
        if CURRENT_MODE != TradingMode.DEMO:
            return {"blocked": True, "reason": "Stop Loss запрещён"}

        return self.client.new_order(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            quantity=quantity,
            reduceOnly=True,
            workingType="MARK_PRICE",
        )

    def take_profit_market_order(self, symbol, side, quantity, take_profit_price):
       if CURRENT_MODE != TradingMode.DEMO:
          return {"blocked": True, "reason": "Take Profit запрещён"}

       return self.client.new_order(
            symbol=symbol,
            side=side,
            type="LIMIT",
            price=take_profit_price,
            quantity=quantity,
            timeInForce="GTC",
            reduceOnly=True,
        )

    def order_status(self, symbol, order_id):
        return self.client.query_order(
            symbol=symbol,
            orderId=order_id,
        )

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
        close_side = "SELL" if side == "BUY" else "BUY"

        return self.client.new_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=quantity,
            reduceOnly=True,
        )
        
    def close_partial_position_market(self, symbol, side, quantity):
        close_side = "SELL" if side == "BUY" else "BUY"

        return self.client.new_order(
            symbol=symbol,
            side=close_side,
            type="MARKET",
            quantity=quantity,
            reduceOnly=True,
        )

    def cancel_all_orders(self, symbol):
        return self.client.cancel_open_orders(symbol=symbol)

    def set_leverage(self, symbol, leverage):
        return self.client.change_leverage(
            symbol=symbol,
            leverage=leverage,
        )