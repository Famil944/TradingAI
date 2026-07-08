from enum import Enum


class TradingMode(Enum):
    SAFE = "SAFE"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


CURRENT_MODE = TradingMode.DEMO