"""Lowest-quota ranking must skip paused rows and still rank an active one.

Three surfaces turn a window percentage into a "lowest quota" claim: the Agent
Bar summary strip (``lowestLeft``), the sidebar nav badge (``navLowestQuota``)
and the "work is nearly full, room elsewhere" hints (``accountRoomHints``).
Each iterated raw windows, so a paused row's withheld window could be ranked
as if it were remaining allowance -- and a paused account could be offered as
the roomy destination. These tests execute the shipped helpers from
``desktop/plugin.js`` against a paused row beside an ordinary active one.
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

// Brace matching ignores braces inside strings, template literals and
// comments, so extraction stops at the end of the named function.
function extract(name) {
  const start = source.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing function: " + name);
  let depth = 0;
  let seen = false;
  let i = start;
  while (i < source.length) {
    const ch = source[i];
    if (ch === "/" && source[i + 1] === "/") {
      i = source.indexOf("\n", i);
      if (i < 0) break;
      continue;
    }
    if (ch === "/" && source[i + 1] === "*") {
      const end = source.indexOf("*/", i + 2);
      if (end < 0) break;
      i = end + 2;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      const quote = ch;
      i += 1;
      while (i < source.length && source[i] !== quote) {
        if (source[i] === "\\") i += 1;
        i += 1;
      }
      i += 1;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      seen = true;
    } else if (ch === "}") {
      depth -= 1;
      if (seen && depth === 0) return source.slice(start, i + 1);
    }
    i += 1;
  }
  throw new Error("could not extract " + name);
}

const NAMES = [
  "asList", "asWindowList", "asProvider", "remainingPct", "usableWindows",
  "worstWindow", "finiteNumber", "isoToSeconds", "lowestLeft",
  "accountRoomHints", "navLowestQuota",
];

// Only the free variables the shipped helpers read: module state is replaced
// by the least surprising stand-ins, so the helpers themselves run unchanged.
const PRELUDE = `
const tr = (key) => (key === "windowFallback" ? "window" : key);
const QUOTA_QUERY_KEY = ["quota", "widget"];
const isProviderEnabled = () => true;
const isConfigured = (p) => Boolean(p && !p.unavailable_reason);
const baseProviderId = (pid) => {
  const text = String(pid || "");
  const colon = text.indexOf(":");
  return colon > 0 ? text.slice(0, colon) : text;
};
const providerEntries = (providers) => Object.entries(providers || {});
const providerDisplayName = (pid, p) => (p && p.account_label) || baseProviderId(pid);
`;

const factory = new Function(
  "queryClient",
  PRELUDE + NAMES.map(extract).join("\n\n") +
    "\nreturn { " + NAMES.join(", ") + " };",
);

const W = (used, label = "Weekly") => ({label, used_percent: used});
const pausedFull = {access_state: "paused", account_label: "Paused old", windows: [W(98)]};
const pausedRoom = {access_state: "paused", account_label: "Paused old", windows: [W(10)]};
const activeFull = {access_state: "active", account_label: "Active full", windows: [W(97)]};
const activeRoom = {access_state: "active", account_label: "Active", windows: [W(40)]};

function helpers(providers) {
  // The key contents are irrelevant to navLowestQuota; it reads providers only.
  const client = {getQueriesData: () => [[["quota", "widget"], {providers}]]};
  return factory(client);
}

const mixed = {"nous:old": pausedFull, "nous:new": activeRoom};

const out = {
  lowestMixed: helpers(mixed).lowestLeft(mixed),
  lowestPausedOnly: helpers({"nous:old": pausedFull}).lowestLeft({"nous:old": pausedFull}),
  lowestActiveOnly: helpers({"nous:new": activeRoom}).lowestLeft({"nous:new": activeRoom}),
  navMixed: helpers(mixed).navLowestQuota(),
  navPausedOnly: helpers({"nous:old": pausedFull}).navLowestQuota(),
  // The paused row is "nearly full": it must not claim room elsewhere.
  hintsPausedFull: helpers({"nous:old": pausedFull, "nous:new": activeRoom})
    .accountRoomHints({"nous:old": pausedFull, "nous:new": activeRoom}),
  // The paused row is the only roomy account: it must not be offered.
  hintsPausedRoom: helpers({"nous:full": activeFull, "nous:old": pausedRoom})
    .accountRoomHints({"nous:full": activeFull, "nous:old": pausedRoom}),
  // Control: two ordinary active accounts still produce the hint.
  hintsActive: helpers({"nous:full": activeFull, "nous:new": activeRoom})
    .accountRoomHints({"nous:full": activeFull, "nous:new": activeRoom}),
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
            "ranking probe failed: exit=%s stderr=%s"
            % (completed.returncode, completed.stderr.strip())
        )
    return json.loads(completed.stdout)


class PausedWindowRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r = _run()

    def test_agent_bar_ranks_the_active_row_not_the_paused_one(self) -> None:
        self.assertEqual({"left": 60, "label": "Active · Weekly"}, self.r["lowestMixed"])

    def test_agent_bar_has_no_percentage_when_only_paused_rows_exist(self) -> None:
        self.assertIsNone(self.r["lowestPausedOnly"])

    def test_agent_bar_still_ranks_an_ordinary_active_row(self) -> None:
        self.assertEqual({"left": 60, "label": "Active · Weekly"}, self.r["lowestActiveOnly"])

    def test_nav_ranks_the_active_row_not_the_paused_one(self) -> None:
        self.assertEqual(60, self.r["navMixed"])

    def test_nav_has_no_percentage_when_only_paused_rows_exist(self) -> None:
        self.assertIsNone(self.r["navPausedOnly"])

    def test_paused_row_does_not_claim_its_work_is_nearly_full(self) -> None:
        self.assertEqual([], self.r["hintsPausedFull"])

    def test_paused_row_is_never_offered_as_the_roomy_destination(self) -> None:
        self.assertEqual([], self.r["hintsPausedRoom"])

    def test_ordinary_active_pair_still_produces_the_room_hint(self) -> None:
        hints = self.r["hintsActive"]
        self.assertEqual(1, len(hints))
        self.assertEqual("Active full", hints[0]["full"])
        self.assertEqual("Active", hints[0]["room"])
        self.assertEqual(60, hints[0]["left"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
