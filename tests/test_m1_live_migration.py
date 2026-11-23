import sqlite3
from services.otc_subscribers import Subscribers
from services.otc_market import ACTIVE_TIMEFRAMES
from services.otc_community_cycles import signals
from services.otc_exhaustion_shadow import decision
from services.otc_shadow_variants import pattern_directions
from tests.test_exhaustion_shadow import bars


def test_legacy_preferences_migrate_preserving_opt_out(tmp_path):
    path = tmp_path / "subscribers.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE timeframe_preferences(chat_id INTEGER NOT NULL, seconds INTEGER NOT NULL CHECK(seconds IN (120,300,900)), enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(chat_id,seconds))")
        db.executemany("INSERT INTO timeframe_preferences VALUES(?,?,?)", [(1,120,0),(2,120,1),(1,300,1)])
    store = Subscribers(path)
    assert store.timeframe_preferences(1)[60] is False
    assert store.timeframe_preferences(2)[60] is True
    assert store.timeframe_preferences(1)[300] is True
    store.set_timeframe(1,60,True)
    store = Subscribers(path)
    assert store.timeframe_preferences(1)[60] is True
    assert store.timeframe_preferences(1)[120] is False
    assert ACTIVE_TIMEFRAMES == (60,)


def test_m1_uses_same_closed_bar_patterns_as_m2():
    history = bars()
    assert signals(history,60,1800) == signals(history,120,1800)
    assert signals(history,60,1800)["exhaustion"] == decision(history,1800)
    assert pattern_directions(history,60,1800).get("bollinger_reversal") == pattern_directions(history,120,1800).get("bollinger_reversal")
