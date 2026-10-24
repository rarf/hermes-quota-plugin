"""A failed poll keeps the last good quota data on screen, visibly stale.

The pane used to replace the whole provider list with the bare
"Quota backend unavailable" line whenever the CLI round-trip failed, even
though a payload (the last success, or the persisted snapshot) was still on
hand -- so the pane "blinked" on every transient gateway hiccup. The bare
error is now reserved for the case where there is no data at all.
"""
import unittest

from widget_harness import text
from test_widget_monitor import quota_payload, run, snapshot

STALE_BANNER = 'Backend unreachable'
BARE_ERROR = 'Quota backend unavailable'


class StaleFallbackTests(unittest.TestCase):
    def test_error_with_data_keeps_the_list_and_marks_it_stale(self):
        out = run(component='pane', data=quota_payload(), monitor=snapshot(),
                  isError=True)
        body = text(out['tree'])
        self.assertIn(STALE_BANNER, body)
        self.assertNotIn(BARE_ERROR, body)
        # The provider cards themselves survive the failed poll.
        self.assertIn('work', body)
        self.assertIn('Current session', body)

    def test_error_without_data_still_reports_the_bare_error(self):
        out = run(component='pane', monitor=snapshot(), isError=True)
        body = text(out['tree'])
        self.assertIn(BARE_ERROR, body)
        self.assertNotIn(STALE_BANNER, body)

    def test_a_fresh_poll_never_shows_the_stale_banner(self):
        out = run(component='pane', data=quota_payload(), monitor=snapshot())
        body = text(out['tree'])
        self.assertNotIn(STALE_BANNER, body)
        self.assertNotIn(BARE_ERROR, body)


if __name__ == '__main__':
    unittest.main()
