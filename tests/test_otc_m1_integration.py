"""Integration: strict M1 is isolated from M5 and preserves research evidence."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.otc_market import Asset
from services.otc_ml.preview import MLPreview, PreviewJournal
from services.otc_ready import ReadyPrimary, VERSION, select_ready
from tests.test_otc_confirmations import history


def fake_checks(direction, count):
    return {
        "count": count,
        "available": 5,
        "total": 5,
        "checks": {key: ("agree" if direction == "UP" else "against")
                   for key in ("ema", "macd", "candle", "rsi", "alignment")},
        "market_state": {"state": "uptrend"},
    }


class M1SelectionIntegrationTests(unittest.TestCase):
    def test_baseline_and_strict_flags_are_recorded(self):
        bars = history()
        now = bars[-1].time + 100
        asset = Asset("A", True, True, 92, None, "A", now)
        audit = []
        with (
            patch("services.otc_ready.build",
                  side_effect=lambda *_: fake_checks("UP", 4)
                  if _[1] == "UP" else fake_checks("DOWN", 0)),
            patch("services.otc_ready.evaluate_policy",
                  return_value={"reason": "eligible", "strategy": "trend_continuation",
                                "gates": {"trend_aligned": True}}),
            patch("services.otc_ready.evaluate_m1",
                  return_value={"passed": False, "reason": "m1_stale_tick",
                                "gates": {"fresh_tick": False}, "metrics": {}}),
        ):
            selected = select_ready({"A": bars}, {"A": asset}, {"A": now},
                                    now, 60, audit=audit)
            self.assertIsNone(selected)
            self.assertEqual(audit[0]["version"], VERSION)
            self.assertTrue(audit[0]["m1_baseline_eligible"])
            self.assertFalse(audit[0]["m1_strict_eligible"])
            self.assertEqual(audit[0]["reason"], "m1_stale_tick")
        # Re-run with strict disabled, preserving the shadow veto for comparison.
        with (
            patch("services.otc_ready.build", side_effect=[
                fake_checks("UP", 4), fake_checks("DOWN", 0),
            ]),
            patch("services.otc_ready.evaluate_policy",
                  return_value={"reason": "eligible", "strategy": "trend_continuation",
                                "gates": {"trend_aligned": True}}),
            patch("services.otc_ready.evaluate_m1",
                  return_value={"passed": False, "reason": "m1_stale_tick",
                                "gates": {"fresh_tick": False}, "metrics": {}}),
        ):
            old = select_ready({"A": bars}, {"A": asset}, {"A": now},
                               now, 60, m1_strict_enabled=False)
            self.assertIsNotNone(old)
            self.assertFalse(old["m1_strict_active"])

    def test_experimental_market_policy_is_shadow_only_by_default(self):
        bars = history()
        now = bars[-1].time + 40  # next entry is a 5m boundary
        asset = Asset("A", True, True, 92, None, "A", now)
        rejected_policy = {
            "reason": "filter_not_doji",
            "strategy": "trend_continuation",
            "gates": {"not_doji": False},
        }
        with (
            patch("services.otc_ready.build", side_effect=[
                fake_checks("UP", 4), fake_checks("DOWN", 0),
            ]),
            patch("services.otc_ready.evaluate_policy", return_value=rejected_policy),
        ):
            selected = select_ready(
                {"A": bars}, {"A": asset}, {"A": now}, now, 300
            )
            self.assertIsNotNone(selected)
            self.assertFalse(selected["policy_enforced"])
        with (
            patch("services.otc_ready.build", side_effect=[
                fake_checks("UP", 4), fake_checks("DOWN", 0),
            ]),
            patch("services.otc_ready.evaluate_policy", return_value=rejected_policy),
        ):
            blocked = select_ready(
                {"A": bars}, {"A": asset}, {"A": now}, now, 300,
                policy_enforced=True,
            )
            self.assertIsNone(blocked)

    def test_m5_does_not_call_m1_policy(self):
        bars = history()
        now = bars[-1].time + 40  # entry index 1000, a 5m boundary
        asset = Asset("A", True, True, 92, None, "A", now)
        with (
            patch("services.otc_ready.build", side_effect=[
                fake_checks("UP", 4), fake_checks("DOWN", 0),
            ]),
            patch("services.otc_ready.evaluate_policy",
                  return_value={"reason": "eligible", "strategy": "trend_continuation",
                                "gates": {"trend_aligned": True}}),
            patch("services.otc_ready.evaluate_m1",
                  side_effect=AssertionError("M1 evaluated for M5")),
        ):
            selected = select_ready({"A": bars}, {"A": asset}, {"A": now},
                                    now, 300)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["expires_at"] - selected["entry_at"], 300)


class M1FinalTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_tick_prevents_send_even_after_initial_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = PreviewJournal(Path(folder) / "ready.db")
            now = 1800000042
            ready = ReadyPrimary(journal, clock=lambda: now)
            selected = {
                "symbol": "A", "direction": "UP", "entry_at": 1800000060,
                "expires_at": 1800000120, "m1_strict_active": True,
                "m1_policy": {
                    "metrics": {"m1_reference_close": 1.0, "m1_atr_price": 0.001}
                },
            }
            with patch.object(MLPreview, "publish", new_callable=AsyncMock,
                              return_value=True) as sender:
                ready.recent_ticks["A"] = (1.0, now - 10, now - 10)
                self.assertFalse(await ready.publish(selected, None, 1))
                sender.assert_not_awaited()
                ready.recent_ticks["A"] = (1.0001, now - 0.5, now - 0.1)
                self.assertTrue(await ready.publish(selected, None, 1))
                sender.assert_awaited_once()
