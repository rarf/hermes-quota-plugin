"""Offline static tests for the desktop widget (desktop/plugin.js).

Run from the repo root:  python tests/test_desktop_widget.py
No network, no credentials, no DOM: the plugin file is checked as text so
the plain-JS ESM widget stays verifiable from the stdlib-only Python suite
(the same suite CI already runs).
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PLUGIN_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "desktop",
    "plugin.js",
)


def _plugin_source() -> str:
    with open(PLUGIN_JS, encoding="utf-8") as f:
        return f.read()


def _provider_svgs_entry(source: str, pid: str) -> str:
    """Extract the `pid: { ... }` object literal from PROVIDER_SVGS."""
    match = re.search(
        r"const PROVIDER_SVGS = \{.*?\n\};", source, re.DOTALL
    )
    if not match:
        raise AssertionError("PROVIDER_SVGS block not found in desktop/plugin.js")
    block = match.group(0)
    entry = re.search(
        rf"^\t(?:\"|')?{re.escape(pid)}(?:\"|')?: \{{(.*?)^\t\}},?",
        block,
        re.DOTALL | re.MULTILINE,
    )
    if not entry:
        raise AssertionError(f"PROVIDER_SVGS.{pid} entry not found")
    return entry.group(1)


def _provider_meta_line(source: str, pid: str) -> tuple:
    match = re.search(
        rf'^\t(?:\"|\')?{re.escape(pid)}(?:\"|\')?: \{{ name: "(.*?)", mono: "(.*?)" \}},$',
        source,
        re.MULTILINE,
    )
    if not match:
        raise AssertionError(f"PROVIDER_META.{pid} entry not found")
    return match.group(1), match.group(2)


class ZaiIconStaticTests(unittest.TestCase):
    """The inline Z.ai SVG must be offline, inline, and theme-driven."""

    @classmethod
    def setUpClass(cls):
        cls.source = _plugin_source()

    def test_zai_svg_entry_exists(self):
        entry = _provider_svgs_entry(self.source, "zai")
        self.assertIn('viewBox: "0 0 24 24"', entry)
        self.assertRegex(entry, r"body: '<path d=\"[^\"]+\"></path>'")

    def test_zai_svg_is_three_closed_subpaths(self):
        # The stylized Z is exactly three geometric pieces (top bar, thick
        # diagonal, bottom bar): three `M` subpaths, each closed with `Z`.
        entry = _provider_svgs_entry(self.source, "zai")
        d = re.search(r"<path d=\"([^\"]+)\">", entry)
        self.assertIsNotNone(d, "zai body must contain a <path d=...>")
        data = d.group(1) if d else ""
        self.assertEqual(3, data.count("M"), "expected three subpaths")
        self.assertEqual(3, data.count("Z"), "every subpath must be closed")
        # Coordinates must stay inside the 24x24 viewBox.
        for num in re.findall(r"-?\d+\.?\d*", data):
            self.assertLessEqual(float(num), 24.0)
            self.assertGreaterEqual(float(num), 0.0)

    def test_zai_svg_has_no_external_dependency(self):
        entry = _provider_svgs_entry(self.source, "zai")
        lowered = entry.lower()
        for banned in (
            "http://",
            "https://",
            "url(",
            "<image",
            "xlink:href",
            "base64",
            ".png",
            ".svg\"",
        ):
            self.assertNotIn(banned, lowered, f"zai icon must not reference {banned!r}")
        # Only <path> elements — no <img>, <script>, <use> or foreignObject.
        tags = re.findall(r"<(\w+)", entry)
        self.assertEqual(["path"], tags, "zai icon body must be <path> only")

    def test_icon_renders_with_theme_currentcolor(self):
        # ProviderBadge passes fill:"currentColor" for every PROVIDER_SVGS
        # entry; the zai path must therefore carry no fill of its own.
        entry = _provider_svgs_entry(self.source, "zai")
        self.assertNotIn("fill=", entry)
        self.assertIn('fill: "currentColor"', self.source)

    def test_zai_display_name(self):
        name, mono = _provider_meta_line(self.source, "zai")
        self.assertEqual("Z.ai", name)
        self.assertEqual("Z", mono)
        self.assertNotIn("Z.ai Coding Plan", self.source)

    def test_backend_provider_key_stays_zai(self):
        # The widget display name changed, but the cache/registry key must
        # not: the backend still registers the fetcher under "zai".
        import quota_providers  # noqa: F401  (imports register every fetcher)
        from quota_providers.registry import PROVIDER_FETCHERS

        self.assertIn("zai", PROVIDER_FETCHERS)

    def test_no_jsx_syntax_in_plugin(self):
        # The plugin is loaded uncompiled: raw JSX would be a syntax error
        # under `node --check` and must never appear.
        self.assertNotIn("=> <", self.source)
        self.assertNotIn("</", self.source.replace("</path>", "").replace("</svg>", ""))


if __name__ == "__main__":
    unittest.main()
