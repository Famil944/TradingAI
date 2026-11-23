import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.otc_m1_cycles_shadow import candidates, M1Shadow, report, VERSION
from services.otc_strategy import Candle
from services.otc_market import ACTIVE_TIMEFRAMES


def test_same_rules_five_methods_independent_m1_slots_and_restart():
    now = 3640
    bars = [Candle(i*60, 100, 101, 99, 100) for i in range(60)]
    asset = SimpleNamespace(name="A", open=True, payout=92, observed_at=now)
    snapshot = ({"A": bars}, {"A": asset}, {"A": now}, None)
    pattern = dict(direction="UP", lower=99, upper=101,
                   candle=dict(open=100, high=101, low=99, close=100.5))
    with patch("services.otc_m1_cycles_shadow.signals", return_value={
            k: dict(direction="UP") for k in ("bb_reentry", "break_retest", "ema_pullback", "exhaustion")}) as live, \
         patch("services.otc_m1_cycles_shadow.pattern_directions", return_value={"bollinger_reversal": pattern}):
        rows = candidates(snapshot, now)
        assert len(rows) == 5
        live.assert_called_once_with(bars, 120, 3600)
        assert candidates(snapshot, 3650) == []
        assert candidates((snapshot[0], snapshot[1], {"A": now-9}, None), now) == []
    assert ACTIVE_TIMEFRAMES == (60,)
    assert all(r["cycle_seconds"] == 60 and r["expires_at"] == 3840 for r in rows)
    with tempfile.TemporaryDirectory() as folder:
        engine = M1Shadow(folder, clock=lambda: now)
        assert len(engine.journal.record(rows)) == 5
        assert engine.journal.record(rows) == []
        restarted = M1Shadow(folder, clock=lambda: now+1)
        assert {3660, 3720, 3780, 3840} <= restarted.sampler.targets["A"]
        assert not (Path(folder)/"data"/"community-live.db").exists()


def test_m1_full_cycle_second_win_and_unknown_separate():
    with tempfile.TemporaryDirectory() as folder:
        engine = M1Shadow(folder, clock=lambda: 1000)
        rows = [dict(symbol=code, direction="UP", entry_at=1020, expires_at=1200,
                     observed_at=1000, cycle_seconds=60, version=VERSION+":"+code,
                     gates={"cycle":True}, experiment=dict(strategy=code, cycle_seconds=60))
                for code in ("S1", "S5")]
        engine.journal.record(rows)
        engine.journal.candidates.samples([("S1",t,t,t,p) for t,p in [(1020,100),(1080,99),(1140,101)]])
        text = report(folder, "S1", now=1500)
        assert "100.0%" in text and "② Вторая — 1" in text
        assert "100.0%" not in report(folder,"S5",now=1500)
        assert "100.0%" not in report(folder,"S1",now=1050)
