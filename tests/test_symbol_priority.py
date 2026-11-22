import unittest
from services.otc_market import Asset, eligible
from services.otc_pair_rotation import choose_pair_swap

class PriorityTests(unittest.TestCase):
    def asset(self,symbol,payout=95,opened=True):
        return Asset(symbol,True,opened,payout,1000,observed_at=1000)

    def test_priority_does_not_bypass_closure_or_exclusion(self):
        for research in (False,True):
            result=eligible([self.asset('Z_otc',100),self.asset('AUDCAD_otc',90),self.asset('AMZN_otc',95,False),self.asset('BITB_otc')],1000,research=research)
            self.assertEqual([a.symbol for a in result],['AUDCAD_otc','Z_otc'])

    def swap(self,chosen,other,protected=set()):
        assets=[self.asset(chosen),self.asset(other)]
        return choose_pair_swap(assets,[chosen],2000,{chosen:0},{chosen:2000},{chosen:2000},protected,set(),{})

    def test_preferred_replaces_healthy_fallback_but_not_reverse(self):
        self.assertEqual(self.swap('Z_otc','AUDCAD_otc')[2],'owner_preference')
        self.assertIsNone(self.swap('AUDCAD_otc','Z_otc'))
        self.assertIsNone(self.swap('Z_otc','AUDCAD_otc',{'Z_otc'}))
