"""Small clock drift must delay a tick, never change its timestamp."""

import unittest
from unittest.mock import AsyncMock, patch

from services.otc_data import wait_for_tick_time
from services.otc_market import Tick


class ClockBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffer_is_bounded_and_preserves_source_time(self):
        for ahead, should_wait in ((1.5, True), (-1, False), (3, False)):
            with (
                self.subTest(ahead=ahead),
                patch("services.otc_data.time.time", return_value=100),
                patch(
                    "services.otc_data.asyncio.sleep", new_callable=AsyncMock
                ) as sleep,
            ):
                tick = Tick("TEST", 100 + ahead, 1)
                await wait_for_tick_time(tick)
                self.assertEqual(sleep.await_count, int(should_wait))
                self.assertEqual(tick.time, 100 + ahead)
                if should_wait:
                    sleep.assert_awaited_once_with(ahead)


if __name__ == "__main__":
    unittest.main()
