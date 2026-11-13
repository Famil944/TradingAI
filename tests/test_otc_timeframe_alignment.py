"""M5/M15 predictions are delivered only 15–20s before candle boundaries."""
import unittest
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import patch

from services.otc_groq import market_packet
from services.otc_market import Asset
from services.otc_ready import select_ready
from services.otc_ml.preview import select_forecast
from services.otc_delivery_guard import delivery_snapshot


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        # UTC anchor is divisible by both 5 and 15 minutes.
        self.anchor = 1800000000
        self.asset = Asset("A", True, True, 92, None, "Asset", self.anchor)

    def test_primary_skips_unaligned_m5_m15(self):
        now = self.anchor + 42  # next minute: +60, not M5/M15 boundary
        self.assertIsNone(select_ready({"A": []}, {"A": self.asset},
                                        {"A": now}, now, 300))
        self.assertIsNone(select_ready({"A": []}, {"A": self.asset},
                                        {"A": now}, now, 900))

    def test_xgboost_never_inspects_unaligned_higher_frame(self):
        now = self.anchor + 42
        calls = []
        class Runtime:
            def inspect(self, row, *_):
                seconds = row["expires_at"] - row["entry_at"]
                calls.append(seconds)
                return {"probabilities": None}
        self.asset = replace(self.asset, observed_at=now)
        select_forecast(Runtime(), {"A": []}, {"A": self.asset}, {"A": now}, now)
        self.assertNotIn(300, calls)
        self.assertNotIn(900, calls)

    def test_groq_offers_only_aligned_m5_m15(self):
        at = self.anchor + 42
        packet = market_packet({}, {}, {}, at, None)
        self.assertNotIn(5, packet["durations_minutes"])
        self.assertNotIn(15, packet["durations_minutes"])
        packet = market_packet({}, {}, {}, self.anchor + 900 - 18, None)
        self.assertIn(5, packet["durations_minutes"])
        self.assertIn(15, packet["durations_minutes"])

    def test_delivery_rejects_out_of_boundary_m5_m15(self):
        now = self.anchor + 42
        self.asset = replace(self.asset, observed_at=now)
        snapshot = ({}, {"A": self.asset}, {"A": now}, None)
        off = {"symbol": "A", "entry_at": self.anchor + 60, "expires_at": self.anchor + 360}
        self.assertIsNone(delivery_snapshot(off, snapshot, now))
        at = self.anchor + 300
        now = at - 18
        self.asset.observed_at = now
        snapshot = ({}, {"A": self.asset}, {"A": now}, None)
        good = {"symbol": "A", "entry_at": at, "expires_at": at + 300}
        self.assertIsNotNone(delivery_snapshot(good, snapshot, now))
        late = dict(good, entry_at=at - 60, expires_at=at + 240)
        self.assertIsNone(delivery_snapshot(late, snapshot, now))
