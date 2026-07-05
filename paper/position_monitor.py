class PositionMonitor:

    def __init__(self, core, paper):
        self.core = core
        self.paper = paper

    def check_current_position(self):
        position = self.paper.engine.trader.position

        if not position:
            return None

        symbol = position.symbol
        price = self.core.market.get_price(symbol)

        close_text = self.paper.check_position_text(price)

        if not close_text:
            return None

        return close_text