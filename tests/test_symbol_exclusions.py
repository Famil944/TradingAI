import unittest
from types import SimpleNamespace
from unittest.mock import patch
from services.otc_market import eligible
from services.otc_delivery_guard import check_data
from services.otc_rsi_shadow import candidates

class ExclusionTests(unittest.TestCase):
    def test_catalogue_exclusion_in_both_modes(self):
        def asset(symbol):
            return SimpleNamespace(symbol=symbol,is_otc=True,open=True,payout=95,payout_at=1000)
        for research in (False,True):
            self.assertEqual([a.symbol for a in eligible([asset('BITB_otc'),asset('BTCUSD_otc')],1000,research=research)],['BTCUSD_otc'])

    def test_stale_candidate_cannot_pass_final_gate(self):
        self.assertEqual(check_data({'symbol':'BITB_otc'},({}, {}, {},None),1000),'excluded_symbol')

    def test_shadow_does_not_analyse_excluded_symbol(self):
        with patch('services.otc_rsi_shadow.decision') as decide:
            self.assertEqual(candidates(({'BITB_otc':[]},{},{},None),1000),[])
        decide.assert_not_called()
