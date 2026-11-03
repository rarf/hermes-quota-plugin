"""Display-time expiry must not erase announcement history or account quota."""
import importlib
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from test_codex_special_reset import cache_module


class ExpiryTest(unittest.TestCase):
    def test_cli_hides_elapsed_and_recorded_but_keeps_future(self):
        cache_module()
        commands = importlib.import_module('quota_plugin_under_test.commands')
        now = datetime.now(timezone.utc)
        base = {'id': 'expiry', 'status': 'scheduled', 'reset_type': 'regular',
                'announced_at': '2026-10-01T00:00:00Z',
                'source_url': 'https://x.com/thsottiaux/status/123'}
        for scheduled, status, visible in [
            ('2000-01-01T00:00:00Z', 'scheduled', False),
            (now.isoformat(), 'scheduled', False),
            ('2099-01-01T00:00:00Z', 'recorded', False),
            ('2099-01-01T00:00:00Z', 'scheduled', True),
            (None, 'scheduled', True),
        ]:
            with self.subTest(scheduled=scheduled, status=status):
                cache = {'codex_special_reset': {'announcement': {
                    **base, 'scheduled_for': scheduled, 'status': status},
                    'fetched_at': now.isoformat(), 'stale': False}}
                self.assertEqual(bool(commands._special_reset_line(cache)), visible)


if __name__ == '__main__':
    unittest.main()
