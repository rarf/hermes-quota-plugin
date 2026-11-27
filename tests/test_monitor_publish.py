"""Seam A publish leg: useMonitor shares each polled monitor snapshot with the
Desktop host through `host.publishMonitorLive`, so the sidebar's Open-now
section shows the same live rows.

Real desktop/plugin.js through tests/widget_monitor_renderer.cjs, SYNTHETIC
payloads only. The harness host records every publish; `noPublishDoor` drops
the door to prove the feature-detected call is a no-op on older hosts.
"""
import shutil
import unittest

from test_widget_monitor import NOW, quota_payload, run, snapshot


def live(monitor, **over):
    return run(component='live', data=quota_payload(), monitor=monitor,
               runEffects=True, **over)


@unittest.skipUnless(shutil.which('node'), 'Node.js is required for widget render tests')
class PublishLiveTests(unittest.TestCase):
    def test_publishes_snapshot_on_poll(self):
        snap = snapshot()
        out = live(snap)
        published = out.get('published')
        self.assertTrue(published, 'no publish recorded for a polled snapshot')
        expected = {'profile': 'work', 'live': snap['live'], 'fetchedAt': snap['generated_at']}
        for entry in published:
            self.assertEqual(entry, expected)

    def test_no_snapshot_no_publish(self):
        # LiveView dereferences the snapshot directly (pre-existing: the live
        # component only renders once a snapshot exists), so the null case is
        # exercised through the pane, which is the null-safe surface.
        out = run(component='pane', data=quota_payload(), monitor=None,
                  runEffects=True)
        self.assertEqual(out.get('published'), [])

    def test_missing_door_is_a_noop(self):
        snap = snapshot()
        out = live(snap, noPublishDoor=True)
        self.assertEqual(out.get('published'), [])
        self.assertIn('tree', out)
