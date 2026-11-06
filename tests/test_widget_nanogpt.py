"""Render NanoGPT's subscription windows and separate cash balance offline."""
import unittest
from widget_harness import render, text

PROVIDER = {
    'windows': [{'label': 'Weekly tokens', 'used_percent': 25},
                {'label': 'Daily images', 'used_percent': 37}],
    'details': ['Prepaid balance: USD 27.30 (separate from included quota)'],
    'account_balances': [{'currency': 'USD', 'total_balance': '27.30'}],
}


class NanoGPTWidgetTests(unittest.TestCase):
    def test_provider_name_windows_and_balance(self):
        for mode in ('clean', 'dense'):
            with self.subTest(mode=mode):
                output = text(render(component='row', id='nano-gpt', mode=mode, provider=PROVIDER))
                self.assertIn('NanoGPT', output)
                self.assertIn('Weekly tokens', output)
                self.assertIn('Daily images', output)
                self.assertIn('27.30', output)

    def test_status_chip_uses_quota_instead_of_balance(self):
        output = text(render(component='chip', id='nano-gpt', provider=PROVIDER))
        self.assertIn('NanoGPT', output)
        self.assertIn('63%', output)


if __name__ == '__main__':
    unittest.main()
