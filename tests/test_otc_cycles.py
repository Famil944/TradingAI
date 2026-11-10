import unittest

from services.otc_cycles import format_cycles, summarize


class CycleTests(unittest.TestCase):
    def rows(self, outcomes):
        return [
            dict(
                created_at=i * 1000,
                entry_at=i * 1000 + 180,
                expires_at=i * 1000 + 240,
                assessed_at=i * 1000 + 305 if outcome else None,
                delivery="SENT",
                outcome=outcome,
            )
            for i, outcome in enumerate(outcomes)
        ]

    def test_win_at_every_recovery_and_four_losses(self):
        for losses in range(4):
            result = summarize(self.rows(["LOSS"] * losses + ["WIN"]))
            self.assertEqual(
                (result["wins"], result["losses"], result["stage"]), (1, 0, 0)
            )
        result = summarize(self.rows(["LOSS"] * 4))
        self.assertEqual((result["wins"], result["losses"], result["stage"]), (0, 1, 0))

    def test_skip_cancel_draw_do_not_advance(self):
        result = summarize(self.rows(["LOSS", "SKIPPED", "CANCELLED", "DRAW", "WIN"]))
        self.assertEqual(
            (result["wins"], result["losses"], result["skipped"]), (1, 0, 2)
        )

    def test_overlap_cannot_be_recovery_and_pending_does_not_lose(self):
        rows = self.rows(["LOSS", "WIN", "LOSS"])
        rows[1]["created_at"] = 100
        result = summarize(rows)
        self.assertEqual((result["wins"], result["losses"], result["stage"]), (0, 0, 2))
        result = summarize(self.rows(["LOSS", None, "WIN"]))
        self.assertEqual((result["wins"], result["losses"], result["stage"]), (0, 0, 1))
        self.assertTrue(result["pending"])

    def test_since_does_not_restart_partial_chain(self):
        result = summarize(self.rows(["LOSS", "LOSS", "LOSS", "LOSS"]), since=2000)
        self.assertEqual(result["losses"], 1)
        self.assertEqual(result["wins"], 0)
        self.assertIn("Винрейт 0.0%", format_cycles(result, 5))

    def test_unknown_never_turns_into_win_or_loss(self):
        result = summarize(self.rows(["LOSS", "UNKNOWN", "WIN"]))
        self.assertEqual(
            (result["wins"], result["losses"], result["unavailable"]), (1, 0, 1)
        )
