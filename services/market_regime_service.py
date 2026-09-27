import asyncio
from dataclasses import dataclass

from analysis.signal_scorer import SignalScorer


@dataclass(frozen=True)
class MarketRegime:
    name: str
    btc_24h_percent: float = 0.0
    btc_7d_percent: float = 0.0
    reason: str = ""


class MarketRegimeService:
    async def assess(self, client) -> MarketRegime:
        one_hour, daily = await asyncio.gather(
            client.get_klines("BTCUSDT", "1h", 50),
            client.get_klines("BTCUSDT", "1d", 30),
        )
        one_hour = one_hour[:-1] if one_hour else []
        daily = daily[:-1] if daily else []
        if len(one_hour) < 25 or len(daily) < 8:
            return MarketRegime("CAUTION", reason="BTC data incomplete")
        move_24h = (one_hour[-1].close / one_hour[-25].close - 1) * 100
        move_7d = (daily[-1].close / daily[-8].close - 1) * 100
        slope_1h = SignalScorer.ema_slope_percent(one_hour)
        if move_24h <= -6 or (move_7d <= -10 and (slope_1h or 0) < 0):
            return MarketRegime(
                "HIGH_RISK", move_24h, move_7d, "BTC падает ускоренно"
            )
        if move_24h <= -3 or move_7d <= -6 or (slope_1h or 0) < -0.5:
            return MarketRegime(
                "CAUTION", move_24h, move_7d, "рынок BTC под давлением"
            )
        return MarketRegime("NORMAL", move_24h, move_7d, "BTC стабилен")
