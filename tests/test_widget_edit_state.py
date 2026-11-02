"""Real-React regression for pane-local card edit mode.

Opt in by having the Hermes desktop build available (the default
``~/.hermes/hermes-agent/apps/desktop`` works on a developer machine).
``tests/widget_edit_state.cjs`` bundles the real ``desktop/plugin.js`` against
the host's React and jsdom, then drives real edit toggles through the DOM:
fresh mounts must be quiet, two mounted panes must not share an edit session,
a remount must not resume one, and an empty payload must clear edit mode
instead of hiding its only toggle.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_edit_state.cjs'
HOST = Path(os.environ.get('QUOTA_DESKTOP_DIR')
            or Path.home() / '.hermes' / 'hermes-agent' / 'apps' / 'desktop')
HOST_ROOT = HOST.parent.parent
DEPS = HOST / 'node_modules'
if not (DEPS / 'jsdom').exists():
    DEPS = HOST_ROOT / 'node_modules'
READY = (
    shutil.which('node') is not None
    and (HOST / 'package.json').exists()
    and (DEPS / 'esbuild').exists()
    and (DEPS / 'jsdom').exists()
    and (DEPS / 'react-dom').exists()
)


@unittest.skipUnless(READY, 'Hermes desktop build + React/jsdom not available')
class PaneLocalEditStateTests(unittest.TestCase):
    def test_edit_mode_is_pane_local_and_resets(self):
        env = {**os.environ, 'QUOTA_DESKTOP_DIR': str(HOST),
               'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8', 'TZ': 'UTC'}
        p = subprocess.run(
            ['node', str(HARNESS)], capture_output=True, text=True,
            timeout=120, env=env, cwd=str(ROOT),
        )
        if p.returncode:
            self.fail((p.stderr or p.stdout)[-6000:])
        start = p.stdout.find('{')
        self.assertGreaterEqual(start, 0, p.stdout)
        report = json.loads(p.stdout[start:])
        failed = [c for c in report['checks'] if not c['ok']]
        self.assertEqual(failed, [], json.dumps(failed, indent=2))
        self.assertEqual(report['failed'], [])
        names = {c['name'] for c in report['checks']}
        for required in (
            'fresh-pane-quiet',
            'second-pane-starts-quiet',
            'panes-edit-independently',
            'pane-exit-is-local',
            'remount-starts-quiet',
            'empty-payload-shows-no-controls',
            'empty-payload-does-not-resume-edit',
            'empty-mount-has-no-edit-affordance',
            'edit-mode-never-writes-storage',
        ):
            self.assertIn(required, names)


if __name__ == '__main__':
    unittest.main()
