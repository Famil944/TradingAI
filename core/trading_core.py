from exchange.binance_client import BinanceMarketClient
from indicators.market_analyzer import MarketAnalyzer
from intelligence.fear_greed import FearGreedService
from intelligence.funding_rate import FundingRateService
from intelligence.open_interest import OpenInterestService
from core.scoring_engine import ScoringEngine
from services.signal_service import SignalService


class TradingCore:
    def __init__(self):
        self.market = BinanceMarketClient()
        self.analyzer = MarketAnalyzer()
        self.fear_greed = FearGreedService()
        self.funding_rate = FundingRateService()
        self.open_interest = OpenInterestService()
        self.scoring = ScoringEngine()
        self.signal_service = SignalService()

    def analyze_symbol(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        fear_greed_data: dict | None = None
    ) -> dict:
        klines = self.market.get_klines(symbol, interval, 250)
        analysis = self.analyzer.analyze(klines)

        if fear_greed_data is None:
            fear_greed_data = self.fear_greed.get_index()

        funding_data = self.funding_rate.get_funding_rate(symbol)
        open_interest_data = self.open_interest.get_open_interest(symbol)

        decision = self.scoring.make_decision(
            analysis=analysis,
            fear_greed_data=fear_greed_data,
            funding_data=funding_data,
            open_interest_data=open_interest_data
        )

        result = {
            "symbol": symbol,
            "interval": interval,
            **analysis,
            "fear_greed": fear_greed_data,
            "funding_rate": funding_data,
            "open_interest": open_interest_data,
            **decision
        }

        self.signal_service.save_signal(result)

        return result