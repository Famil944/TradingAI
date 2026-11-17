import tempfile
import unittest
from pathlib import Path

from services.otc_virtual_wallet import VirtualWallet, amount


class WalletTests(unittest.TestCase):
    def test_requested_payouts_and_idempotent_restart(self):
        for outcomes, expected in (
            (["WIN", "PENDING", "PENDING"], 9200),
            (["LOSS", "WIN", "PENDING"], 13000),
            (["LOSS", "LOSS", "WIN"], 22500),
            (["LOSS"] * 3, -97500),
        ):
            with (
                self.subTest(outcomes=outcomes),
                tempfile.TemporaryDirectory() as folder,
            ):
                path = Path(folder) / "wallet.db"
                wallet = VirtualWallet(path)
                wallet.enroll("cycle")
                wallet.settle("cycle", outcomes)
                VirtualWallet(path).settle("cycle", outcomes)
                state = wallet.state()
                self.assertEqual(state["balance"], 500000 + expected)
                self.assertEqual(state["profit"], expected)
                self.assertEqual(state["reserved"], 0)

    def test_partial_loss_only_once_unknown_is_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            wallet = VirtualWallet(Path(folder) / "wallet.db")
            wallet.enroll("cycle")
            wallet.settle("cycle", ["LOSS", "PENDING", "PENDING"])
            wallet.settle("cycle", ["LOSS", "PENDING", "PENDING"])
            self.assertEqual(wallet.state()["balance"], 490000)
            self.assertEqual(wallet.state()["reserved"], 87500)
            wallet.settle("cycle", ["LOSS", "UNKNOWN", "PENDING"])
            self.assertEqual(wallet.state()["reserved"], 25000)
            self.assertEqual(wallet.state()["counts"]["UNKNOWN"], 1)

    def test_draw_refund_and_cancel_release(self):
        with tempfile.TemporaryDirectory() as folder:
            wallet = VirtualWallet(Path(folder) / "wallet.db")
            wallet.enroll("draw")
            wallet.settle("draw", ["DRAW", "PENDING", "PENDING"])
            self.assertEqual(wallet.state()["balance"], 500000)
            wallet.enroll("cancel")
            wallet.cancel("cancel")
            self.assertEqual(wallet.state()["reserved"], 0)

    def test_edits_do_not_reprice_active_cycles_or_count_as_profit(self):
        with tempfile.TemporaryDirectory() as folder:
            wallet = VirtualWallet(Path(folder) / "wallet.db")
            wallet.enroll("old")
            wallet.change("stakes", "10 25 62,50")
            wallet.change("payout", "80")
            wallet.change("balance", "6000")
            wallet.enroll("new")
            wallet.settle("old", ["WIN", "PENDING", "PENDING"])
            wallet.settle("new", ["WIN", "PENDING", "PENDING"])
            s = wallet.state()
            self.assertEqual(s["balance"], 610000)
            self.assertEqual(s["profit"], 10000)
            self.assertEqual(s["adjustments"], 100000)

    def test_no_overdraft_from_parallel_cycles_and_input_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            wallet = VirtualWallet(Path(folder) / "wallet.db")
            for i in range(5):
                self.assertEqual(wallet.enroll(str(i))["status"], "ACTIVE")
            self.assertEqual(wallet.enroll("six")["status"], "SKIPPED")
            with self.assertRaises(ValueError):
                wallet.change("balance", "0")
            for text in ("NaN", "Infinity", "-1", "1.001"):
                with self.assertRaises(ValueError):
                    amount(text)
            with self.assertRaises(ValueError):
                wallet.change("stakes", "100 250")
            with self.assertRaises(ValueError):
                wallet.change("payout", "101")
