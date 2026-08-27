import asyncio


class PositionWatchLoop:

    def __init__(self, core, paper, notifier, interval_seconds=10):
        self.core = core
        self.paper = paper
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.is_running = False
        self._stop_requested = False

    async def start(self):
        if self.is_running:
            return

        self.is_running = True
        self._stop_requested = False

        try:
            while self.is_running and not self._stop_requested:
                try:
                    position = self.paper.engine.trader.position

                    if position:
                        price = await asyncio.to_thread(
                            self.core.market.get_price,
                            position.symbol,
                        )
                        result = self.paper.check_position_text(price)

                        if result:
                            await self.notifier.send_async(result)

                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    print(f"POSITION WATCH ERROR: {error}")

                await asyncio.sleep(self.interval_seconds)
        finally:
            self.is_running = False
            self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        self.is_running = False
