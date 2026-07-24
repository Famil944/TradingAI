import hashlib
import hmac
import time
from urllib.parse import urlencode

import requests
from binance.error import ClientError


class PortfolioMarginApi:
    """Minimal HMAC adapter for Binance Portfolio Margin UM endpoints."""

    BASE_URL = "https://papi.binance.com"

    def __init__(
        self,
        api_key,
        api_secret,
        public_client,
        session=None,
        timeout=15,
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.public = public_client
        self.session = session or requests.Session()
        self.timeout = timeout
        self._conditional_ids = set()

    def _signed_request(self, method, path, **params):
        payload = {
            key: self._serialize(value)
            for key, value in params.items()
            if value is not None
        }
        payload.setdefault("recvWindow", 5000)
        payload["timestamp"] = int(time.time() * 1000)
        query = urlencode(payload)
        payload["signature"] = hmac.new(
            self.api_secret,
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        response = self.session.request(
            method,
            self.BASE_URL + path,
            params=payload if method in {"GET", "DELETE"} else None,
            data=payload if method not in {"GET", "DELETE"} else None,
            headers={"X-MBX-APIKEY": self.api_key},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            try:
                error = response.json()
            except ValueError:
                error = {"code": response.status_code, "msg": response.text}
            raise ClientError(
                response.status_code,
                error.get("code"),
                error.get("msg", "Binance Portfolio Margin error"),
                dict(response.headers),
            )
        return response.json()

    @staticmethod
    def _serialize(value):
        if isinstance(value, bool):
            return str(value).lower()
        return value

    @staticmethod
    def _conditional_order(data):
        normalized = dict(data)
        strategy_id = data.get("strategyId")
        normalized["triggeredOrderId"] = data.get("orderId")
        normalized["orderId"] = strategy_id
        normalized["status"] = (
            data.get("status")
            or data.get("strategyStatus")
        )
        normalized["type"] = data.get("strategyType")
        normalized["_conditional"] = True
        return normalized

    # Public USD-M market data remains on fapi.
    def ping(self):
        return self.public.ping()

    def time(self):
        return self.public.time()

    def ticker_price(self, symbol):
        return self.public.ticker_price(symbol=symbol)

    def klines(self, **kwargs):
        return self.public.klines(**kwargs)

    def exchange_info(self):
        return self.public.exchange_info()

    # Signed Portfolio Margin account endpoints.
    def account(self):
        account = self._signed_request(
            "GET", "/papi/v1/um/account"
        )
        portfolio = self._signed_request(
            "GET", "/papi/v1/account"
        )
        available = (
            portfolio.get("totalAvailableBalance")
            or portfolio.get("actualEquity")
            or "0"
        )
        for asset in account.get("assets", []):
            if asset.get("asset") == "USDT":
                if float(available) <= 0:
                    available = asset.get("crossWalletBalance", "0")
                asset["availableBalance"] = available
        account["canTrade"] = portfolio.get("accountStatus") == "NORMAL"
        account["portfolioAccountStatus"] = portfolio.get("accountStatus")
        return account

    def get_position_risk(self, symbol=None):
        return self._signed_request(
            "GET",
            "/papi/v1/um/positionRisk",
            symbol=symbol,
        )

    def get_position_mode(self):
        return self._signed_request(
            "GET", "/papi/v1/um/positionSide/dual"
        )

    def new_order(self, symbol, side, type, **kwargs):
        params = {"symbol": symbol, "side": side, **kwargs}
        if type in {
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
            "TRAILING_STOP_MARKET",
        }:
            params.pop("newOrderRespType", None)
            result = self._signed_request(
                "POST",
                "/papi/v1/um/conditional/order",
                strategyType=type,
                **params,
            )
            normalized = self._conditional_order(result)
            self._conditional_ids.add(str(normalized["orderId"]))
            return normalized

        return self._signed_request(
            "POST",
            "/papi/v1/um/order",
            type=type,
            **params,
        )

    def query_order(self, symbol, orderId):
        order_id = str(orderId)
        if order_id in self._conditional_ids:
            return self._query_conditional(symbol, orderId)
        try:
            return self._signed_request(
                "GET",
                "/papi/v1/um/order",
                symbol=symbol,
                orderId=orderId,
            )
        except ClientError:
            return self._query_conditional(symbol, orderId)

    def _query_conditional(self, symbol, strategy_id):
        result = self._signed_request(
            "GET",
            "/papi/v1/um/conditional/orderHistory",
            symbol=symbol,
            strategyId=strategy_id,
        )
        self._conditional_ids.add(str(strategy_id))
        return self._conditional_order(result)

    def get_open_orders(self, symbol=None):
        normal = self._signed_request(
            "GET",
            "/papi/v1/um/openOrders",
            symbol=symbol,
        )
        conditional = self._signed_request(
            "GET",
            "/papi/v1/um/conditional/openOrders",
            symbol=symbol,
        )
        normalized = [
            self._conditional_order(item)
            for item in conditional
        ]
        self._conditional_ids.update(
            str(item["orderId"]) for item in normalized
        )
        return normal + normalized

    def cancel_order(self, symbol, orderId):
        order_id = str(orderId)
        if order_id in self._conditional_ids:
            return self._cancel_conditional(symbol, orderId)
        try:
            return self._signed_request(
                "DELETE",
                "/papi/v1/um/order",
                symbol=symbol,
                orderId=orderId,
            )
        except ClientError:
            return self._cancel_conditional(symbol, orderId)

    def _cancel_conditional(self, symbol, strategy_id):
        return self._signed_request(
            "DELETE",
            "/papi/v1/um/conditional/order",
            symbol=symbol,
            strategyId=strategy_id,
        )

    def cancel_open_orders(self, symbol):
        normal = self._signed_request(
            "DELETE",
            "/papi/v1/um/allOpenOrders",
            symbol=symbol,
        )
        conditional = self._signed_request(
            "DELETE",
            "/papi/v1/um/conditional/allOpenOrders",
            symbol=symbol,
        )
        return {"normal": normal, "conditional": conditional}

    def change_leverage(self, symbol, leverage):
        return self._signed_request(
            "POST",
            "/papi/v1/um/leverage",
            symbol=symbol,
            leverage=leverage,
        )

    def get_account_trades(self, symbol, **kwargs):
        return self._signed_request(
            "GET",
            "/papi/v1/um/userTrades",
            symbol=symbol,
            **kwargs,
        )

    def get_income_history(self, **kwargs):
        return self._signed_request(
            "GET",
            "/papi/v1/um/income",
            **kwargs,
        )
