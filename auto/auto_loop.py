import asyncio


class AutoLoop:

    def __init__(self, auto_state, auto_trader, interval_seconds=60):
        self.auto_state = auto_state
        self.auto_trader = auto_trader
        self.interval_seconds = interval_seconds
        self.is_running = False

    async def start(self):
        if self.is_running:
            return

        self.is_running = True
        await asyncio.sleep(2)

        while True:
            if self.auto_state.enabled:
                try:
                    result = self.auto_trader.run_once()
                    print("AUTO:", result)
                except Exception as e:
                    print("AUTO ERROR:", e)

            await asyncio.sleep(self.interval_seconds)