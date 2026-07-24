import asyncio


class AutoLoop:

    def __init__(
        self,
        auto_state,
        auto_trader,
        notifier,
        interval_seconds=60,
    ):
        self.auto_state = auto_state
        self.auto_trader = auto_trader
        self.notifier = notifier
        self.interval_seconds = interval_seconds

        self.is_running = False
        self._stop_requested = False

    async def start(self):
        if self.is_running:
            print("⚠️ AutoLoop уже запущен.")
            return

        self.is_running = True
        self._stop_requested = False

        print("🤖 AutoLoop запущен")

        try:
            await asyncio.sleep(2)

            while (
                self.is_running
                and not self._stop_requested
            ):
                if not self.auto_state.enabled:
                    print("⏸ Автоторговля выключена.")
                    break

                try:
                    # Сканирование выполняется отдельно,
                    # чтобы не зависал Telegram-бот.
                    result = await asyncio.to_thread(
                        self.auto_trader.run_once
                    )

                    if result:
                        print(result)

                        try:
                            await self.notifier.send_async(result)
                        except Exception as notify_error:
                            print(
                                "TELEGRAM NOTIFY ERROR: "
                                f"{notify_error}"
                            )

                except Exception as error:
                    error_text = (
                        f"❌ Ошибка AutoLoop\n\n{error}"
                    )

                    print(f"AUTO ERROR: {error}")

                    try:
                        await self.notifier.send_async(error_text)
                    except Exception as notify_error:
                        print(
                            "TELEGRAM NOTIFY ERROR: "
                            f"{notify_error}"
                        )

                # Ждём небольшими частями, чтобы выключение
                # автоторговли срабатывало быстро.
                settings = getattr(self.auto_trader, "settings", None)
                if settings is not None:
                    # Interval remains intentionally conservative; the selected
                    # timeframe controls candles, not request frequency.
                    self.interval_seconds = max(self.interval_seconds, 60)
                seconds_left = self.interval_seconds

                while (
                    seconds_left > 0
                    and self.is_running
                    and not self._stop_requested
                    and self.auto_state.enabled
                ):
                    await asyncio.sleep(
                        min(5, seconds_left)
                    )
                    seconds_left -= 5

        finally:
            self.is_running = False
            self._stop_requested = False
            print("🛑 AutoLoop остановлен")

    def stop(self):
        self._stop_requested = True
        self.is_running = False
