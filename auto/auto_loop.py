import asyncio


class AutoLoop:

    def __init__(self, auto_state, auto_trader, notifier, interval_seconds=60):
        self.auto_state = auto_state
        self.auto_trader = auto_trader
        self.notifier = notifier
        self.interval_seconds = interval_seconds
        self.is_running = False

    async def start(self):
        if self.is_running:
            return

        self.is_running = True

        await asyncio.sleep(2)

        while True:
            try:
                if self.auto_state.enabled:
                    result = self.auto_trader.run_once()

                    print(result)

                    if result:
                        await self.notifier.send(result)

            except Exception as e:
                print(f"AUTO ERROR: {e}")

                await self.notifier.send(
                    f"❌ Ошибка AutoLoop\n\n{e}"
                )

            await asyncio.sleep(self.interval_seconds)