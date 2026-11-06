"""Sidebar nav quota badge must rank only usable windows.

The badge is the third place (after the chips and the pane) that turns a
window percentage into a "lowest quota" claim. It used to read raw
``rec.windows`` and could therefore rank a positive window from a depleted,
paused or unverifiable row as if access were healthy. These tests execute the
shipped helpers from ``desktop/plugin.js`` with a fake query cache.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"

RUNNER = r"""
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");

function between(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) throw new Error("missing start marker: " + startMarker);
  const end = source.indexOf(endMarker, start);
  if (end < 0) throw new Error("missing end marker: " + endMarker);
  return source.slice(start, end);
}

// asList/asWindowList/asProvider + usableWindows (worstWindow marks the end).
const helpers = between("function asList(value) {", "function worstWindow(");
const nav = between("function navLowestQuota() {", "/** Label + icon for the nav row");
const factory = new Function(
  "queryClient",
  "QUOTA_QUERY_KEY",
  "isProviderEnabled",
  helpers + "\n" + nav + "\nreturn {navLowestQuota, usableWindows};",
);

function lowest(providers) {
  const client = {getQueriesData: () => [[["quota", "widget"], {providers}]]};
  return factory(client, ["quota", "widget"], () => true).navLowestQuota();
}

const window = (used) => ({label: "Weekly", used_percent: used});

const out = {
  // A positive window on a row whose own evidence says it is unusable must
  // never become the nav percentage.
  depleted: lowest({nous: {windows: [window(95)], access_state: "depleted"}}),
  paused: lowest({nous: {windows: [window(96)], access_state: "paused"}}),
  unavailable: lowest({nous: {windows: [window(97)], access_state: "unavailable"}}),
  flagged: lowest({nous: {windows: [window(98)], windows_usable: false}}),
  noCalls: lowest({nous: {windows: [window(99)], api_calls_available: false}}),
  fetchError: lowest({nous: {windows: [window(100)], unavailable_reason: "timeout"}}),
  // Free/plan-only rows have no percentage; they contribute nothing.
  free: lowest({nous: {windows: [], access_state: "free", plan: "Free"}}),
  // Mixed: the healthy row is the only rankable one.
  mixed: lowest({
    nous: {windows: [window(95)], access_state: "depleted"},
    anthropic: {windows: [window(90)], windows_usable: false},
    openrouter: {windows: [window(85)], api_calls_available: false},
    "openai-codex": {windows: [window(40)]},
  }),
  // A missing/NaN percentage is not a claim either.
  malformed: lowest({anthropic: {windows: [{label: "Weekly", used_percent: "50"}]}}),
  empty: lowest({}),
};

process.stdout.write(JSON.stringify(out));
"""


def _run() -> dict:
    completed = subprocess.run(
        ["node", "-e", RUNNER, str(PLUGIN)],
        check=False, capture_output=True, text=True, timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "nav probe failed: exit=%s stderr=%s"
            % (completed.returncode, completed.stderr.strip())
        )
    return json.loads(completed.stdout)


class NavLowestQuotaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _run()

    def test_unusable_rows_do_not_supply_the_nav_percentage(self) -> None:
        for name in ("depleted", "paused", "unavailable", "flagged", "noCalls", "fetchError"):
            with self.subTest(name=name):
                self.assertIsNone(self.r[name])

    def test_free_or_empty_rows_add_no_percentage(self) -> None:
        self.assertIsNone(self.r["free"])
        self.assertIsNone(self.r["empty"])
        self.assertIsNone(self.r["malformed"])

    def test_only_the_healthy_window_is_ranked(self) -> None:
        self.assertEqual(60, self.r["mixed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
