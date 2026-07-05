import asyncio


class PositionWatchLoop:

    def __init__(self, core, paper, notifier, interval_seconds=10):
        self.core = core
        self.paper = paper
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.is_running = False

    async def start(self):
        if self.is_running:
            return

        self.is_running = True

        while True:
            try:
                position = self.paper.engine.trader.position

                if position:
                    price = self.core.market.get_price(position.symbol)
                    result = self.paper.check_position_text(price)

                    if result:
                        await self.notifier.send(result)

            except Exception as e:
                print(f"POSITION WATCH ERROR: {e}")

            await asyncio.sleep(self.interval_seconds)