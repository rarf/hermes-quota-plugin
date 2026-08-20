from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ManifestTests(unittest.TestCase):
    def test_backend_and_dashboard_versions_match(self):
        plugin_yaml = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        match = re.search(r"^version:\s*([^\s]+)\s*$", plugin_yaml, re.MULTILINE)
        self.assertIsNotNone(match)
        dashboard = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(match.group(1), dashboard["version"])


if __name__ == "__main__":
    unittest.main()
