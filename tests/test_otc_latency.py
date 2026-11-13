import unittest
from types import SimpleNamespace

from services.otc_delivery_guard import delivery_snapshot


class DeliverySafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000042.0
        self.asset = SimpleNamespace(open=True, payout=92.0, observed_at=self.now)
        self.selected = dict(symbol="A", entry_at=1800000060, expires_at=1800000120)
        self.snapshot = ({}, {"A": self.asset}, {"A": self.now}, None)

    def test_fresh_snapshot_rechecks_at_delivery(self):
        evidence = delivery_snapshot(self.selected, self.snapshot, self.now)
        self.assertEqual(evidence["quote_age_s"], 0)
        self.assertFalse(evidence["platform_match_verified"])

    def test_stale_quote_or_payout_blocks(self):
        self.assertIsNone(delivery_snapshot(self.selected, ({}, {"A": self.asset}, {"A": self.now - 9}, None), self.now))
        self.asset.observed_at = self.now - 61
        self.assertIsNone(delivery_snapshot(self.selected, self.snapshot, self.now))

    def test_late_arrival_blocks(self):
        self.assertIsNone(delivery_snapshot(self.selected, self.snapshot, self.now + 4))
        self.assertIsNone(delivery_snapshot(self.selected, None, self.now))

    def test_closed_market_blocks(self):
        self.asset.open = False
        self.assertIsNone(delivery_snapshot(self.selected, self.snapshot, self.now))
