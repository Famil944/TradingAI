"""Non-destructive, owner-authorized S1-S4 winrate reset tests."""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import community
from services.otc_winrate_reset import WinrateReset


MSK = ZoneInfo("Europe/Moscow")


def moment(hour=12):
    return datetime(2026, 11, 18, hour, tzinfo=MSK).timestamp()


def signal(identifier, when, outcome):
    return dict(
        id=identifier, origin="main", strategy="bollinger_live",
        version="v1", seconds=120, label="Private", symbol="TEST OTC",
        direction="UP", created_at=when - 5, sent_at=when,
        entry_at=when + 20, expires_at=when + 380,
        resolved_at=when + 380, outcome=outcome,
        attempts=[outcome],
    )


class ResetStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "reset.db"
        self.store = WinrateReset(self.path)

    def test_default_empty_and_persistent_after_restart(self):
        self.assertEqual(self.store.cutoff(), 0)
        token = self.store.prepare(now=1000)
        self.assertTrue(self.store.confirm(token, now=1003))
        self.assertEqual(WinrateReset(self.path).cutoff(), 1003)
        self.assertFalse(self.store.confirm(token, now=1005))
        self.assertEqual(self.store.cutoff(), 1003)

    def test_expired_and_wrong_confirmation_protected(self):
        token = self.store.prepare(now=1000)
        self.assertFalse(self.store.confirm("invalid", now=1001))
        self.assertFalse(self.store.confirm(token, now=1301))
        self.assertEqual(self.store.cutoff(), 0)
        second = self.store.prepare(now=2000)
        self.assertFalse(self.store.confirm(token, now=2001))
        self.assertTrue(self.store.cancel(second))
        self.assertFalse(self.store.confirm(second, now=2002))
        self.assertEqual(self.store.cutoff(), 0)

    def test_reset_does_not_change_history_or_live_outcomes(self):
        first = signal("old", moment(9), "LOSS")
        second = signal("new", moment(13), "WIN")
        rows = [first, second]
        with patch.object(community, "winrate_reset", return_value=self.store), \
             patch.object(community, "snapshots",
                          side_effect=lambda source: rows if source == "main" else []), \
             patch.object(community.time, "time", return_value=moment(14)):
            before = community.compact_overview("all", "all")
            self.assertIn("S1  M2 50.0% (n=2) · M5 — (n=0) · M15 — (n=0)", before)
            token = self.store.prepare(now=moment(12))
            self.assertTrue(self.store.confirm(token, now=moment(12)))
            for period in ("today", "7d", "30d", "all"):
                text = community.compact_overview("all", period)
                self.assertIn("S1  M2 100.0% (n=1) · M5 — (n=0) · M15 — (n=0)", text)
                self.assertIn("Новый отсчёт с", text)
                self.assertNotIn("M2 50.0%", text)
            owner = community.overview_text("all")
            self.assertIn("S1 Боллинджер ×3\nM2 100.0% (n=1) · M5 — (n=0) · M15 — (n=0)", owner)
            details = community.report_for("main", "all", "B", period="all")
            self.assertIn("WIN 1 · LOSS 0", details)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["outcome"], "LOSS")

    def test_empty_after_reset_shows_no_claimed_winrate(self):
        rows = [signal("old", moment(9), "WIN")]
        with patch.object(community, "winrate_reset", return_value=self.store), \
             patch.object(community, "snapshots", return_value=rows), \
             patch.object(community.time, "time", return_value=moment(14)):
            token = self.store.prepare(now=moment(14))
            self.assertTrue(self.store.confirm(token, now=moment(14)))
            text = community.compact_overview("all", "all")
            self.assertIn("S1  M2 — (n=0) · M5 — (n=0) · M15 — (n=0)", text)
            self.assertNotIn("100.0%", text)


class OwnerOnlyResetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = WinrateReset(Path(folder.name) / "reset.db")

    def fake_message(self, chat_id=1):
        return SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            from_user=SimpleNamespace(id=chat_id),
            answer=AsyncMock(),
        )

    async def test_command_requires_owner_private_chat(self):
        msg = self.fake_message(4)
        with patch.object(community, "winrate_reset", return_value=self.store), \
             patch.object(community, "is_owner", return_value=False):
            await community.reset_winrate_command(msg)
        msg.answer.assert_not_awaited()
        self.assertEqual(self.store.cutoff(), 0)

    async def test_owner_can_confirm_once_through_button(self):
        msg = self.fake_message(1)
        with patch.object(community, "winrate_reset", return_value=self.store), \
             patch.object(community, "is_owner", return_value=True), \
             patch.object(community, "settings", SimpleNamespace(admin_chat_id=1)):
            await community.reset_winrate_command(msg)
            menu = msg.answer.await_args.kwargs["reply_markup"]
            callback = menu.inline_keyboard[0][0].callback_data
            self.assertTrue(callback.startswith("c:wr_reset:confirm:"))
            query = SimpleNamespace(
                message=msg, from_user=msg.from_user,
                data=callback, answer=AsyncMock(),
            )
            await community.callbacks(query)
            self.assertGreater(self.store.cutoff(), 0)
            original_cutoff = self.store.cutoff()
            await community.callbacks(query)
            self.assertEqual(self.store.cutoff(), original_cutoff)
            self.assertGreaterEqual(query.answer.await_count, 2)

    async def test_subscriber_cannot_trigger_reset_callback(self):
        msg = self.fake_message(2)
        query = SimpleNamespace(
            message=msg, from_user=msg.from_user,
            data="c:wr_reset:ask", answer=AsyncMock(),
        )
        with patch.object(community, "winrate_reset", return_value=self.store), \
             patch.object(community, "is_owner", return_value=False):
            await community.callbacks(query)
        self.assertEqual(self.store.cutoff(), 0)
        msg.answer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
