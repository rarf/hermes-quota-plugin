"""Real-Chromium regression for pinned cards and drag scrolling.

Opt in by having the Hermes desktop build available (the default
``~/.hermes/hermes-agent/apps/desktop`` works on a developer machine).
The Node harness builds the real ``desktop/plugin.js`` against the host's
compiled CSS and Playwright, then checks geometry, the single pin control,
whole-card pointer drag with clickable controls, edge autoscroll and wheel
scrolling in both bands, cross-band refusal, cue placement, stale-offset
reset, short-pane readability, and that the pinned band's opaque backdrop
covers the region's top edge. It also pins wheel containment at both shelf
boundaries, the measured resize limits through pointer, touch, keyboard,
quiet-view, viewport and pin-set changes, and the natural default height.
The minimum height is checked against real card and handle boxes: the first
card must be fully visible and the resize strip must start below its bottom
border, not across it.
Very short panes (180px and 200px tall) are checked in both the quiet and edit
views, with and without the Agent monitor: the monitor's tab row and summary
strip must not crush the card region, so the compact layout keeps one tab row
and both bands scrollable. A grouped card's account switch must re-measure the
shelf in both mounted panes through the stable group wrapper, pin-then-unpin
must leave Reset inert unless a real preference remains (including a provider
discovered while the card is pinned), clicking the resize handle must focus it
so the arrow keys work, and every layout control and grouped account button
must paint a visible focus ring under the host's global focus reset.
Double-click snapping is exercised with real Chromium double-clicks at the
strip: the card midpoint is the tipping point, scrolled shelf coordinates are
honoured, live card heights re-measure, the half-pane cap clamps a request
beyond it, and a snapped preference survives a pane width change while drag
still takes over afterwards.
Separate pinned/unpinned native scrollbars are checked for layout gutters,
hover paint, real pointer drags (including at the resizer overlap), dynamic
height and a dark-theme pass, with opt-out of Playwright's hidden-scrollbar
default. Synthetic data only; no credentials.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / 'widget_card_browser.cjs'
HOST = Path(os.environ.get('QUOTA_DESKTOP_DIR')
            or Path.home() / '.hermes' / 'hermes-agent' / 'apps' / 'desktop')
HOST_ROOT = HOST.parent.parent
READY = (
    shutil.which('node') is not None
    and ((HOST / 'node_modules' / 'playwright').exists()
         or (HOST_ROOT / 'node_modules' / 'playwright').exists())
    and (HOST / 'dist' / 'assets').is_dir()
)


@unittest.skipUnless(READY, 'Hermes desktop build + Playwright not available')
class PinnedCardBrowserTests(unittest.TestCase):
    def test_real_chromium_layout_drag_and_scroll(self):
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
        self.assertGreaterEqual(len(report['checks']), 100)
        names = {c['name'] for c in report['checks']}
        for required in (
            'scrollbar-scrollports-separate', 'scrollbar-native-gutters',
            'scrollbar-both-bands-overflow', 'scrollbar-confined-to-each-band',
            'scrollbar-card-widths-aligned', 'scrollbar-pinned-visible-on-hover',
            'scrollbar-list-visible-on-hover', 'scrollbar-drag-pinned-only',
            'scrollbar-drag-list-only', 'scrollbar-drag-at-resizer-edge',
            'scrollbar-dynamic-height', 'scrollbar-theme-gutters-and-alignment',
            'scrollbar-theme-thumb-painted', 'scrollbar-theme-restored',
            'scrollbar-rtl-confined',
            'wheel-top-contained', 'wheel-bottom-contained',
            'wheel-contained-without-overflow',
            'resizer-present', 'resizer-anchored-to-shelf-bottom',
            'resizer-range-from-cards', 'resize-drag-min',
            'resize-min-first-card-fully-visible',
            'resize-min-handle-below-first-card', 'resize-drag-max',
            'resize-keyboard', 'resize-touch', 'resize-cancel-reverts-preview',
            'resize-viewport-clamp', 'resize-repin-restores-height',
            'resize-width-remeasure', 'resize-works-in-quiet-view',
            'resize-one-pin-inert-fit', 'resize-inert-grip-stays-faint',
            'resize-one-pin-handle-clear',
            'resize-all-fit-multi-range',
            'resize-clamped-preference-set',
            'resize-clamped-preference-kept-on-pin',
            'resize-clamped-preference-restores',
            'pin-growth-tall-default', 'pin-growth-tall-half-cap',
            'pin-growth-normal-default', 'pin-growth-normal-half-cap',
            'pin-growth-short-default', 'resize-click-keeps-auto',
            'pin-growth-after-handle-click', 'resize-manual-height-persists',
            'resize-manual-survives-new-pins',
            'resize-manual-reclamps-and-restores',
            'resize-ceiling-returns-to-auto', 'pin-growth-after-ceiling-drag',
            'pin-growth-heals-legacy-ceiling',
            'short-pane-readable-fallback',
            'short-pane-resize-blocked-and-scrollable',
            'short-pane-180-pinned-usable',
            'short-pane-180-list-reachable',
            'short-pane-200-pinned-usable',
            'short-pane-200-list-reachable',
            'short-pane-180-edit-mode-nonzero',
            'short-pane-200-edit-mode-nonzero',
            'short-pane-160-edit-usable',
            'short-pane-160-edit-controls-and-bands-reachable',
            'short-pane-monitor-180-compact-chrome',
            'short-pane-monitor-180-both-bands-usable',
            'short-pane-monitor-200-compact-chrome',
            'short-pane-monitor-200-both-bands-usable',
            'short-pane-monitor-180-quiet-compact',
            'monitor-chrome-restored-tall-pane',
            'resize-tight-pane-floor-blocked',
            'shelf-reobserves-account-switch',
            'shelf-reobserves-second-pane',
            'reset-honesty-after-pin-unpin',
            'reset-honesty-keeps-absent-slot',
            'reset-honesty-new-provider-not-frozen',
            'focus-visible-layout-controls',
            'focus-visible-grouped-account',
            'keyboard-account-switch-keeps-focus',
            'resize-click-focuses-handle',
            'snap-boundaries-measured', 'snap-top-half-of-third-card',
            'snap-top-half-to-previous-card',
            'snap-bottom-half-to-card-bottom',
            'snap-scrolled-shelf-uses-content',
            'snap-beyond-half-pane-cap-clamps',
            'snap-last-card-ceiling-auto', 'snap-uses-live-card-heights',
            'snap-preference-survives-pane-width',
            'snap-then-drag-still-works',
        ):
            self.assertIn(required, names)
        print(f"{len(report['checks'])} real-Chromium pinned-card checks passed.")


if __name__ == '__main__':
    unittest.main()
