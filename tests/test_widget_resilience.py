"""The widget must degrade, not lie, and must not block the render thread.

The widget renders whatever `quota_cache.json` happens to contain. A truncated
write, a hand edit, or a gateway diagnostic in the CLI's combined stdout/stderr
can leave that file in any shape, and every field below is nullable by design.

Pure helpers are extracted from `desktop/plugin.js` by `widget_extract.cjs` and
evaluated in isolation, so no React runtime is needed. Skipped without node.
"""
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "tests" / "widget_extract.cjs"


def run_js(calls: dict) -> dict:
    """Evaluate extracted helpers. `calls` maps a helper name to its args."""
    if shutil.which("node") is None:
        raise unittest.SkipTest("node not available")
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp) / "in.json"
        fixture.write_text(json.dumps(calls))
        proc = subprocess.run(
            ["node", str(EXTRACT), str(ROOT), str(fixture)],
            capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise AssertionError("node harness failed: %s" % proc.stderr[:2000])
        return json.loads(proc.stdout)


def threw(value) -> bool:
    return isinstance(value, dict) and "__error" in value


class ParseIsNotQuadraticTests(unittest.TestCase):
    """A brace-heavy diagnostic must not block the render thread."""

    def _time(self, n_bases: int) -> float:
        # Raw brace-heavy diagnostic text, not JSON-encoded: encoding escapes
        # the braces into a string literal, which the parser skips in one pass.
        # This is the shape that made queryFn block (gateway diagnostics are
        # concatenated onto the CLI's stdout).
        start = time.perf_counter()
        run_js({"parseJsonOutput": ["{" * n_bases, "providers"]})
        return time.perf_counter() - start

    def test_scales_linearly_not_quadratically(self):
        # Warm node twice so startup is not counted as growth.
        self._time(500)
        self._time(500)
        small = self._time(2000)
        large = self._time(16000)          # 8x the input
        # Measured on the unfixed parser: 0.055s -> 0.992s, an 18x ratio for
        # 8x input. A linear scan is ~8x. Assert on the ratio alone, with no
        # absolute floor -- a floor would let the quadratic version pass,
        # because node startup dominates at these sizes.
        ratio = large / max(small, 1e-9)
        self.assertLess(ratio, 12.0,
                        "parse time grew %.1fx for 8x input (small=%.3fs "
                        "large=%.3fs) -- the retry loop is rescanning"
                        % (ratio, small, large))

    def test_a_huge_diagnostic_is_fast(self):
        """60k unmatched openers blocked the render thread for ~19s before."""
        elapsed = self._time(60000)
        self.assertLess(elapsed, 3.0,
                        "60k unmatched openers took %.2fs on the render path"
                        % elapsed)

    def test_still_recovers_the_payload_after_a_diagnostic(self):
        payload = {"fetched_at": "2026-01-15T12:34:56Z", "age_s": 7, "providers": {}}
        text = ('gateway diagnostic: { wrapper "{not JSON [still text]}"'
                + "\n" + json.dumps(payload))
        got = run_js({"parseJsonOutput": [text, "providers"]})["parseJsonOutput"]
        self.assertEqual(got, payload)

    def test_still_skips_an_unrelated_json_object(self):
        payload = {"providers": {"a": {"label": "a"}}}
        text = json.dumps({"diagnostic": "ready"}) + "\n" + json.dumps(payload)
        got = run_js({"parseJsonOutput": [text, "providers"]})["parseJsonOutput"]
        self.assertEqual(got, payload)

    def test_a_well_formed_payload_is_unchanged(self):
        payload = {"fetched_at": "2026-01-15T12:34:56Z", "providers": {}}
        got = run_js({"parseJsonOutput": [json.dumps(payload), "providers"]})["parseJsonOutput"]
        self.assertEqual(got, payload)


class MalformedProviderRecordsTests(unittest.TestCase):
    """`x || []` is null-safe but not type-safe."""

    def test_non_array_windows_do_not_throw(self):
        for windows in ({}, "text", 7, None, [], [None]):
            with self.subTest(windows=windows):
                got = run_js({"worstWindow": [
                    {"label": "p", "windows": windows}]})["worstWindow"]
                self.assertFalse(threw(got), got)

    def test_worst_window_still_works_for_a_good_record(self):
        got = run_js({"worstWindow": [{"label": "p", "windows": [
            {"label": "A", "used_percent": 10.0},
            {"label": "B", "used_percent": 80.0},
        ]}]})["worstWindow"]
        self.assertEqual(got, 20)   # the tightest window wins

    def test_null_provider_does_not_throw(self):
        got = run_js({"worstWindow": [None]})["worstWindow"]
        self.assertFalse(threw(got), got)

    def test_as_list_normalises(self):
        self.assertEqual(run_js({"asList": [{}]})["asList"], [])
        self.assertEqual(run_js({"asList": ["x"]})["asList"], [])

    def test_as_list_keeps_a_real_array(self):
        self.assertEqual(run_js({"asList": [[1, 2]]})["asList"], [1, 2])

    def test_as_provider_normalises(self):
        for bad in (None, [], "x", 7):
            with self.subTest(bad=bad):
                self.assertIsNone(run_js({"asProvider": [bad]})["asProvider"])


class BalancePrecisionTests(unittest.TestCase):
    """The endpoint's decimal string must survive; Number() rounds it away."""

    def _text(self, value: str) -> str:
        return run_js({"balanceText": [
            {"total_balance": value, "currency": "USD"}]})["balanceText"]

    def test_ordinary_amounts_are_unchanged(self):
        self.assertIn("12.50", self._text("12.50"))
        self.assertIn("0.50", self._text("0.5"))       # at least two digits
        self.assertIn("12.50", self._text("12.5"))

    def test_sub_cent_precision_is_kept(self):
        # Regression already pinned by test_widget_balances.
        self.assertIn("0.00000001", self._text("0.00000001"))

    def test_past_2_53_is_not_rounded_to_a_neighbour(self):
        """Number('9007199254740993.00') is ...992 -- off by one."""
        rendered = self._text("9007199254740993.00")
        self.assertIn("9,007,199,254,740,993.00", rendered)
        self.assertNotIn("9,007,199,254,740,992", rendered)

    def test_decimal_beyond_double_precision_is_kept(self):
        """Number() keeps 17 significant digits; the string keeps them all
        up to the 20-digit cap balanceFractionDigits already sets (from #23)."""
        value = "0.123456789012345678"      # 18 digits: beyond a double
        self.assertIn(value, self._text(value))

    def test_the_20_digit_cap_is_respected(self):
        """balanceFractionDigits caps at 20 (your #23 change); keep that."""
        value = "0.123456789012345678901234"   # 24 digits
        rendered = self._text(value)
        self.assertIn("0.12345678901234567890", rendered)   # 20 digits
        self.assertNotIn("12345678901", rendered[13:])       # not 24

    def test_thousands_grouping(self):
        self.assertIn("1,234.50", self._text("1234.50"))

    def test_zero_still_renders(self):
        self.assertIn("0.00", self._text("0.00"))
        self.assertIn("0.00", self._text("0"))

    def test_negative_keeps_its_sign(self):
        self.assertIn("-5.25", self._text("-5.25"))


class RemainingPctTests(unittest.TestCase):
    def test_clamps_and_null_guards(self):
        for used, expected in ((10.0, 90), (150.0, 0), (-50.0, 100),
                               (100.0, 0), (0.0, 100)):
            with self.subTest(used=used):
                got = run_js({"remainingPct": [{"used_percent": used}]})["remainingPct"]
                self.assertEqual(got, expected)
        for bad in (None, "abc"):
            with self.subTest(bad=bad):
                got = run_js({"remainingPct": [{"used_percent": bad}]})["remainingPct"]
                self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
