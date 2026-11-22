import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from services.otc_strategy import Candle
from services.otc_exhaustion_shadow import decision, ExhaustionShadow, ShadowObservers, report, VERSION
from services.otc_rsi_shadow import RSIShadow


def bars():
    result = [Candle(i*60,100,100.5,99.5,100+(i%2)*.1) for i in range(26)]
    for i, close in enumerate([99.4,98.8,98.2],26):
        result.append(Candle(i*60,close+.4,close+.5,close-.2,close))
    result.append(Candle(29*60,98.2,99.3,97.5,99))
    return result


class ExhaustionTests(unittest.TestCase):
    def test_symmetric_confirmation_closed_only(self):
        history=bars()
        self.assertEqual(decision(history,1800)['direction'],'UP')
        future=Candle(1800,1,1000,1,1000)
        self.assertEqual(decision(history+[future],1800),decision(history,1800))
        mirror=[Candle(b.time,200-b.open,200-b.low,200-b.high,200-b.close) for b in history]
        self.assertEqual(decision(mirror,1800)['direction'],'DOWN')
        history[-1]=Candle(1740,98.2,99.3,97.5,98)
        self.assertIsNone(decision(history,1800))
        self.assertIsNone(decision(bars()[:-1],1800))
        self.assertIsNone(decision(bars()[1:],1800))
        self.assertIsNone(decision(bars()[:10]+bars()[11:],1800))

    def test_generation_window_and_separate_restart_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            engine=ExhaustionShadow(root,clock=lambda:1840)
            asset=SimpleNamespace(open=True,payout=95,observed_at=1840,name='A')
            snapshot=({'A':bars()},{'A':asset},{'A':1840},None)
            with patch('services.otc_rsi_shadow.check_data',return_value=None):
                rows=engine.candidate_rows(snapshot,1840)
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['cycle_seconds'],120)
                self.assertEqual(rows[0]['version'],VERSION)
                self.assertEqual(engine.candidate_rows(snapshot,1850),[])
            self.assertEqual(len(engine.journal.record(rows)),1)
            restarted=ExhaustionShadow(root,clock=lambda:1841)
            self.assertEqual(restarted.journal.record(rows),[])
            self.assertTrue({1860,1980,2100,2220} <= restarted.sampler.targets['A'])
            rsi=RSIShadow(root,clock=lambda:1841)
            self.assertNotEqual(rsi.journal.path,restarted.journal.path)
            group=ShadowObservers(rsi,restarted)
            group.observe(*snapshot)
            self.assertIsNotNone(restarted.snapshot)
            group.snapshot=None
            self.assertIsNone(rsi.snapshot)
            self.assertIsNone(restarted.snapshot)

    def test_report_uses_full_cycle_not_first_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            engine=ExhaustionShadow(root,clock=lambda:1000)
            row=dict(symbol='A',direction='UP',entry_at=1020,expires_at=1380,
                     observed_at=1000,cycle_seconds=120,version=VERSION,gates={'cycle':True},
                     experiment=dict(strategy=VERSION,cycle_seconds=120))
            engine.journal.record([row])
            engine.journal.candidates.samples([('A',t,t,t,p) for t,p in [(1020,100),(1140,99),(1260,101)]])
            text=report(root,1400)
            self.assertIn('100.0%',text)
            self.assertIn('② Вторая — 1',text)
            self.assertIn('❌ Поражения: 0',text)
            self.assertNotIn('100.0%',report(root,1100))
