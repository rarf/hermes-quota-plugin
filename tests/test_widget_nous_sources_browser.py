"""Real-Chromium visual/contract test for the Billing + account Nous card.

Optional: skips unless the Hermes desktop build and Playwright are available
(the same condition as the other browser suites). The Node harness renders the
real ``desktop/plugin.js`` against the host's compiled CSS with synthetic Free
Billing and cached account snapshots; it checks that a same-organisation/same-plan
cached rate-limit line (a match hint, not account proof) and an enabled fresh
Free tool pool survive, while
mismatched plans, stale caches and stale paid or financial facts never reach the
card. It also renders the live paid usage-window shape (Billing gauges plus the
compatible account Total usable/credit/renewal facts and exact card labels) and
checks every documented published tier; it can load a real sanitized Billing
response plus the real `quota status --json --cached` payload via
`QUOTA_BILLING_FIXTURE` / `QUOTA_QUOTA_FIXTURE`. No credentials or network
calls are used by the harness itself.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "widget_nous_sources_browser.cjs"
HOST = Path(os.environ.get("QUOTA_DESKTOP_DIR")
            or Path.home() / ".hermes" / "hermes-agent" / "apps" / "desktop")
HOST_ROOT = HOST.parent.parent
READY = (
    shutil.which("node") is not None
    and ((HOST / "node_modules" / "playwright").exists()
         or (HOST_ROOT / "node_modules" / "playwright").exists())
    and (HOST / "dist" / "assets").is_dir()
)


@unittest.skipUnless(READY, "Hermes desktop build + Playwright not available")
class NousSourceBrowserTests(unittest.TestCase):
    def test_real_free_card_keeps_tool_pool_and_hides_stale_paid(self) -> None:
        env = {**os.environ, "QUOTA_DESKTOP_DIR": str(HOST),
               "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TZ": "UTC"}
        completed = subprocess.run(
            ["node", str(HARNESS)], capture_output=True, text=True,
            timeout=180, env=env, cwd=str(ROOT),
        )
        if completed.returncode:
            self.fail((completed.stderr or completed.stdout)[-6000:])
        start = completed.stdout.find("{")
        self.assertGreaterEqual(start, 0, completed.stdout)
        report = json.loads(completed.stdout[start:])
        failed = [check for check in report["checks"] if not check["ok"]]
        self.assertEqual([], failed, json.dumps(failed, indent=2))
        self.assertGreaterEqual(len(report["checks"]), 8)


if __name__ == "__main__":
    unittest.main()
