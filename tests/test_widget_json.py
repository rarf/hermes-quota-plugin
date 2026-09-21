"""Regression tests for desktop cli.exec JSON extraction."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "desktop" / "plugin.js"


def _parse_with_widget(output: str, schema_key: str) -> object:
    """Invoke the parser defined in the real widget source."""
    runner = r"""
const fs = require("fs");
const [sourcePath, output, schemaKey] = process.argv.slice(1);
const source = fs.readFileSync(sourcePath, "utf8");
const start = source.indexOf("function parseJsonOutput(");
const end = source.indexOf("\nfunction readSnapshot(", start);
if (start < 0 || end < 0) {
  throw new Error("parseJsonOutput is not present in desktop/plugin.js");
}
const parseJsonOutput = eval(`(${source.slice(start, end)})`);
process.stdout.write(JSON.stringify(parseJsonOutput(output, schemaKey)));
"""
    completed = subprocess.run(
        ["node", "-e", runner, str(PLUGIN), output, schema_key],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "widget parser failed: "
            f"exit={completed.returncode}, stderr={completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


class WidgetJsonOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "fetched_at": "2026-09-21T12:00:00+00:00",
            "installed_sha": "abc123",
            "providers": {"openai-codex": {"windows": []}},
        }

    def test_quota_json_after_diagnostic(self) -> None:
        output = "gateway: using cached quota\n" + json.dumps(self.payload)

        self.assertEqual(_parse_with_widget(output, "providers"), self.payload)

    def test_quota_json_before_trailing_diagnostic(self) -> None:
        output = json.dumps(self.payload) + "\ngateway: cache read complete"

        self.assertEqual(_parse_with_widget(output, "providers"), self.payload)

    def test_quota_skips_unrelated_json_object(self) -> None:
        unrelated = {"diagnostic": "gateway ready", "code": "cache-hit"}
        output = json.dumps(unrelated) + "\n" + json.dumps(self.payload)

        self.assertEqual(_parse_with_widget(output, "providers"), self.payload)

    def test_quota_recovers_from_delimiter_like_diagnostic_text(self) -> None:
        diagnostic = 'gateway diagnostic: { wrapper "{not JSON [still text]}"'
        output = diagnostic + "\n" + json.dumps(self.payload)

        self.assertEqual(_parse_with_widget(output, "providers"), self.payload)

    def test_quota_ignores_delimiters_inside_diagnostic_string(self) -> None:
        diagnostic = 'gateway diagnostic: "{not JSON [still text]}"'
        output = diagnostic + "\n" + json.dumps(self.payload)

        self.assertEqual(_parse_with_widget(output, "providers"), self.payload)

    def test_update_json_after_diagnostic_uses_installed_sha_schema(self) -> None:
        payload = {
            "installed_sha": "abc123",
            "installed_at": "2026-09-21T12:00:00Z",
        }
        output = "gateway: checking install { [done]\n" + json.dumps(payload)

        self.assertEqual(_parse_with_widget(output, "installed_sha"), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
