"""Real-Chromium responsiveness regression for sort/reset confirmations.

Opt in by having the Hermes desktop build available (the default
``~/.hermes/hermes-agent/apps/desktop`` works on a developer machine).
The Node harness builds the real ``desktop/plugin.js`` against the host's
compiled CSS and the host's real Radix ``ConfirmDialog`` (aliased from the
desktop source), then measures click-to-paint, click-to-close and pointer-to-
hover-fill with React Profiler commit times. It asserts the card list does not
re-render merely to open or cancel the question, that confirming closes
promptly, and that both footer buttons paint their hover fill within a frame
instead of the host's 100 ms ramp. Synthetic data only; no credentials.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_card_confirm_browser.cjs'
HOST = Path(os.environ.get('QUOTA_DESKTOP_DIR')
            or Path.home() / '.hermes' / 'hermes-agent' / 'apps' / 'desktop')
HOST_ROOT = HOST.parent.parent
READY = (
    shutil.which('node') is not None
    and ((HOST / 'node_modules' / 'playwright').exists()
         or (HOST_ROOT / 'node_modules' / 'playwright').exists())
    and (HOST / 'dist' / 'assets').is_dir()
    and (HOST / 'src' / 'components' / 'ui' / 'confirm-dialog.tsx').is_file()
)


@unittest.skipUnless(READY, 'Hermes desktop build + Playwright + ConfirmDialog source not available')
class ConfirmDialogBrowserTests(unittest.TestCase):
    def test_real_chromium_confirmation_latency(self):
        env = {**os.environ, 'QUOTA_DESKTOP_DIR': str(HOST),
               'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'}
        p = subprocess.run(
            ['node', str(HARNESS)], capture_output=True, text=True,
            timeout=240, env=env, cwd=str(ROOT),
        )
        if p.returncode:
            self.fail((p.stderr or p.stdout)[-6000:])
        start = p.stdout.find('{')
        self.assertGreaterEqual(start, 0, p.stdout)
        report = json.loads(p.stdout[start:])
        failed = [c for c in report['checks'] if not c['ok']]
        self.assertEqual(failed, [], json.dumps(failed, indent=2))
        self.assertEqual(report['errors'], [])
        self.assertGreaterEqual(len(report['checks']), 11)
        print('Confirmation timings:', json.dumps(report['timings']))


if __name__ == '__main__':
    unittest.main()
