"""Real-Chromium regression for the Antigravity switch across surfaces/profiles.

The desktop plugin mounts the settings control in the route page *and* the
docked pane over one focused profile. This suite drives both mounted instances
in a real browser against a stubbed SDK whose `quota switch` calls stay open
until the test resolves them, so "write in flight" is real:

* the second surface cannot start a competing (opposite) write while the first
  one is unanswered, and the pill is disabled on both surfaces meanwhile;
* resolving the write frees the slot, refreshes once, and the next flip from
  the other surface is serialized after it;
* an optimistic off hides the Antigravity cards immediately, and turning it
  back on restores them;
* moving the focused profile shows that profile's own switch state and cards
  (a flip in one profile never repaints another), and a write carries the
  profile that was focused when it was issued;
* a failed write explains itself on both surfaces of that profile and stays
  behind when the focus moves to another profile.

Opt in by having the Hermes desktop build available (the default
``~/.hermes/hermes-agent/apps/desktop`` works on a developer machine).
Synthetic data only; no credentials.
"""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).resolve().parent / "widget_antigravity_switch_browser.cjs"


def _candidate_homes():
    """Home directories to search for the desktop build.

    A sandboxed ``HOME`` (tests, containers) hides the real one, so fall back to
    the account's passwd entry before giving up.
    """
    homes = []
    explicit = os.environ.get("QUOTA_DESKTOP_DIR")
    if explicit:
        return [Path(explicit)]
    homes.append(Path.home())
    try:
        import pwd

        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if account not in homes:
            homes.append(account)
    except (ImportError, KeyError):
        pass
    return homes


def _host_dir():
    for home in _candidate_homes():
        host = home / ".hermes" / "hermes-agent" / "apps" / "desktop"
        if (host / "dist" / "assets").is_dir():
            return host
    return _candidate_homes()[0] / ".hermes" / "hermes-agent" / "apps" / "desktop"


HOST = _host_dir()
ROOT_NODE = HOST.parent.parent / "node_modules"
READY = (
    shutil.which("node") is not None
    and (HOST / "dist" / "assets").is_dir()
    and ((HOST / "node_modules" / "playwright").exists()
         or (ROOT_NODE / "playwright").exists())
    and ((HOST / "node_modules" / "esbuild").exists()
         or (ROOT_NODE / "esbuild").exists())
)

REQUIRED_CHECKS = {
    "both-surfaces-start-on",
    "pane-shows-antigravity",
    "optimistic-off-repaints-both-surfaces",
    "pending-write-disables-both-surfaces",
    "optimistic-off-hides-the-card-now",
    "exactly-one-switch-write",
    "first-write-is-off-for-work",
    "second-surface-click-is-inert-while-busy",
    "refused-click-keeps-the-optimistic-value",
    "still-one-switch-write-after-settling",
    "one-follow-up-refresh",
    "busy-cleared-on-the-other-surface",
    "settled-off-keeps-the-card-hidden",
    "second-flip-is-on-and-serialized",
    "on-restores-the-card",
    "work-off-before-the-profile-switch",
    "profile-switch-shows-the-new-profiles-state",
    "profile-switch-keeps-the-new-profiles-cards",
    "home-flip-hides-home-cards",
    "profile-switch-back-keeps-work-off",
    "home-flip-did-not-write-work",
    "each-write-carries-its-own-profile",
    "failed-write-explains-itself-on-both-surfaces",
    "failed-write-reverts-the-optimistic-value",
    "failure-does-not-follow-the-profile-switch",
}


@unittest.skipUnless(READY, "Hermes desktop build + Playwright not available")
class AntigravitySwitchBrowserTests(unittest.TestCase):
    def test_multi_surface_and_profile_switch_regressions(self) -> None:
        env = {
            **os.environ,
            "QUOTA_DESKTOP_DIR": str(HOST),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TZ": "UTC",
        }
        completed = subprocess.run(
            ["node", str(HARNESS)],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            cwd=str(ROOT),
        )
        if completed.returncode:
            self.fail((completed.stderr or completed.stdout)[-6000:])
        start = completed.stdout.find("{")
        self.assertGreaterEqual(start, 0, completed.stdout)
        report = json.loads(completed.stdout[start:])

        failed = [check for check in report["checks"] if not check["ok"]]
        self.assertEqual(failed, [], json.dumps(failed, indent=2))
        self.assertEqual(report["errors"], [])
        names = {check["name"] for check in report["checks"]}
        self.assertEqual(sorted(REQUIRED_CHECKS - names), [])
        self.assertGreaterEqual(len(report["checks"]), len(REQUIRED_CHECKS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
