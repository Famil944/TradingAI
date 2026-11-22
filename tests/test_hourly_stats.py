import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from types import SimpleNamespace
from services.otc_hourly_stats import hourly_groups, hourly_text
from bot import community


def row(day=1,hour=21,outcome='WIN',**extra):
    return dict(entry_at=datetime(2026,11,day,hour,tzinfo=timezone.utc).timestamp(),
                outcome=outcome,**extra)


class HourlyTests(unittest.TestCase):
    def test_moscow_first_entry_not_resolution_or_attempts(self):
        rows=[row(outcome='WIN',attempts=['LOSS','LOSS','WIN'],resolved_at=1),
              row(outcome='LOSS'),row(outcome='UNKNOWN'),row(outcome='DRAW'),
              row(outcome='PENDING'),dict(outcome='WIN')]
        groups,missing=hourly_groups(rows)
        self.assertEqual(groups[0]['known'],2)
        self.assertEqual(groups[0]['wr'],50)
        self.assertEqual(groups[0]['unknown'],1)
        self.assertEqual(groups[21]['total'],0)
        self.assertEqual(missing,1)
        self.assertLess(len(hourly_text(rows,'test')),4096)

    def test_small_samples_and_missing_prices_do_not_rank(self):
        rows=[row(day=day) for day in (1,2,3) for _ in range(7)]
        self.assertTrue(hourly_groups(rows)[0][0]['enough'])
        self.assertFalse(hourly_groups(rows[:14])[0][0]['enough'])
        self.assertFalse(hourly_groups(rows+[row(outcome='UNKNOWN') for _ in range(10)])[0][0]['enough'])
        self.assertFalse(hourly_groups([row() for _ in range(25)])[0][0]['enough'])

    def test_personal_source_period_and_expiry_filters(self):
        rows=[row(id='mine',seconds=120,sent_at=100),
              row(id='other',seconds=120,sent_at=100,outcome='LOSS'),
              row(id='old',seconds=120,sent_at=1,outcome='LOSS'),
              row(id='m5',seconds=300,sent_at=100,outcome='LOSS')]
        store=SimpleNamespace(delivered_ids=lambda user: {'mine','old','m5'})
        with patch.object(community,'snapshots',return_value=rows) as snapshots, \
             patch.object(community,'stats_since',return_value=50), \
             patch.object(community,'subscribers',return_value=store), \
             patch.object(community,'reset_note',return_value=''):
            text=community.hourly_dashboard('main','7d','2','personal',42)
        snapshots.assert_called_once_with('main')
        self.assertIn('100.0% · 1/0',text)
        self.assertIn('мои сигналы',text)
        self.assertNotIn('other',text)

    def test_button_preserves_filters(self):
        menu=community.cycle_menu('main','30d','2','personal')
        buttons=[b.callback_data for r in menu.inline_keyboard for b in r]
        self.assertIn('c:hours:main:30d:2:personal',buttons)
        self.assertTrue(all(len(b.encode())<=64 for b in buttons))
