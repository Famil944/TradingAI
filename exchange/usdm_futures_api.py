from binance.error import ClientError


class UsdmFuturesApi:
    """Compatibility adapter for regular and conditional USD-M orders."""

    CONDITIONAL_TYPES = {
        "STOP",
        "STOP_MARKET",
        "TAKE_PROFIT",
        "TAKE_PROFIT_MARKET",
        "TRAILING_STOP_MARKET",
    }

    def __init__(self, client):
        self.client = client
        self._algo_ids = set()

    def __getattr__(self, name):
        return getattr(self.client, name)

    @staticmethod
    def _normalize_algo_order(data):
        normalized = dict(data)
        normalized["orderId"] = data.get("algoId")
        normalized["status"] = data.get("algoStatus")
        normalized["type"] = data.get("orderType") or data.get("type")
        normalized["stopPrice"] = data.get("triggerPrice")
        normalized["_conditional"] = True
        return normalized

    def _algo_request(self, method, path, **params):
        return self.client.sign_request(method, path, params)

    def new_order(self, symbol, side, type, **kwargs):
        if type not in self.CONDITIONAL_TYPES:
            return self.client.new_order(
                symbol=symbol,
                side=side,
                type=type,
                **kwargs,
            )

        params = dict(kwargs)
        params.pop("newOrderRespType", None)
        params["triggerPrice"] = params.pop("stopPrice", None)
        result = self._algo_request(
            "POST",
            "/fapi/v1/algoOrder",
            algoType="CONDITIONAL",
            symbol=symbol,
            side=side,
            type=type,
            **params,
        )
        normalized = self._normalize_algo_order(result)
        self._algo_ids.add(str(normalized["orderId"]))
        return normalized

    def query_order(self, symbol, orderId):
        order_id = str(orderId)
        if order_id in self._algo_ids:
            return self._query_algo(orderId)
        try:
            return self.client.query_order(symbol=symbol, orderId=orderId)
        except ClientError:
            return self._query_algo(orderId)

    def _query_algo(self, algo_id):
        result = self._algo_request(
            "GET",
            "/fapi/v1/algoOrder",
            algoId=algo_id,
        )
        self._algo_ids.add(str(algo_id))
        return self._normalize_algo_order(result)

    def get_open_orders(self, symbol=None):
        normal = self.client.get_open_orders(symbol=symbol)
        algo = self._algo_request(
            "GET",
            "/fapi/v1/openAlgoOrders",
            symbol=symbol,
        )
        normalized = [
            self._normalize_algo_order(item)
            for item in algo
        ]
        self._algo_ids.update(
            str(item["orderId"]) for item in normalized
        )
        return normal + normalized

    def cancel_order(self, symbol, orderId):
        order_id = str(orderId)
        if order_id in self._algo_ids:
            return self._cancel_algo(orderId)
        try:
            return self.client.cancel_order(
                symbol=symbol,
                orderId=orderId,
            )
        except ClientError:
            return self._cancel_algo(orderId)

    def _cancel_algo(self, algo_id):
        return self._algo_request(
            "DELETE",
            "/fapi/v1/algoOrder",
            algoId=algo_id,
        )

    def cancel_open_orders(self, symbol):
        normal = self.client.cancel_open_orders(symbol=symbol)
        algo = self._algo_request(
            "DELETE",
            "/fapi/v1/algoOpenOrders",
            symbol=symbol,
        )
        return {"normal": normal, "conditional": algo}
