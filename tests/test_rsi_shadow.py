import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from services.otc_strategy import Candle
from services.otc_rsi_shadow import decision, report, RSIShadow, VERSION

class RSIShadowTests(unittest.TestCase):
    def test_closed_only_crossing_and_trend(self):
        bars = [Candle(i*60, 100+i*.1, 102+i*.1, 99+i*.1, 101+i*.1) for i in range(250)]
        with patch('services.otc_rsi_shadow.rsi', side_effect=[6,4]):
            result = decision(bars+[Candle(15000,1,999,1,999)],15000)
        self.assertEqual(result['direction'],'UP')
        self.assertEqual(result['last_closed'],14940)
        with patch('services.otc_rsi_shadow.rsi', side_effect=[4,3]):
            self.assertIsNone(decision(bars,15000))
        self.assertIsNone(decision(bars[:-1],15000))
        self.assertIsNone(decision(bars[:100],6000))
        self.assertIsNone(decision(bars[:50]+bars[51:],15000))

    def test_down_and_no_countertrend(self):
        bars=[Candle(i*60,200-i*.1,201-i*.1,198-i*.1,199-i*.1) for i in range(250)]
        with patch('services.otc_rsi_shadow.rsi', side_effect=[94,96]):
            self.assertEqual(decision(bars,15000)['direction'],'DOWN')
        with patch('services.otc_rsi_shadow.rsi', side_effect=[6,4]):
            self.assertIsNone(decision(bars,15000))

    def test_separate_storage_and_restart_deduplication(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            observer=RSIShadow(root,clock=lambda:1000)
            row=dict(symbol='A',direction='UP',entry_at=1020,expires_at=1380,
                     observed_at=1000,cycle_seconds=120,version=VERSION,gates={"cycle":True},
                     experiment=dict(strategy=VERSION,cycle_seconds=120))
            self.assertEqual(len(observer.journal.record([row])),1)
            restarted=RSIShadow(root,clock=lambda:1001)
            self.assertEqual(restarted.journal.record([row]),[])
            self.assertTrue({1020,1140,1260,1380} <= restarted.sampler.targets['A'])
            self.assertIn('M2 · прогнозов 1',report(root,1001))
            self.assertEqual({p.name for p in (root/'data').glob('*.db')},{'rsi2-shadow.db'})
