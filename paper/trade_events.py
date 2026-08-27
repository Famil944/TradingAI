class TradeEvent:

    PARTIAL_TAKE_PROFIT = "PARTIAL_TAKE_PROFIT"
    BREAK_EVEN = "BREAK_EVEN"
    TRAILING_STOP = "TRAILING_STOP"
    POSITION_CLOSED = "POSITION_CLOSED"

    def init(self, event_type: str, data: dict):
        self.event_type = event_type
        self.data = data