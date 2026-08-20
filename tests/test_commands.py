from __future__ import annotations

import argparse
import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_package():
    if "quota_plugin" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "quota_plugin",
            ROOT / "__init__.py",
            submodule_search_locations=[str(ROOT)],
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["quota_plugin"] = module
        spec.loader.exec_module(module)
    return sys.modules["quota_plugin"]


class QuotaCliTests(unittest.TestCase):
    def test_provider_can_be_selected_without_provider_keyword(self):
        load_package()
        from quota_plugin import commands

        parser = argparse.ArgumentParser()
        commands.setup_argparse(parser)
        args = parser.parse_args(["grok"])

        self.assertEqual(args.quota_command, "grok")

    def test_direct_provider_alias_renders_only_that_provider(self):
        load_package()
        from quota_plugin import commands

        original = commands._render_quota
        commands._render_quota = lambda provider: f"provider={provider}"
        try:
            parser = argparse.ArgumentParser()
            commands.setup_argparse(parser)
            args = parser.parse_args(["grok"])
            output = io.StringIO()
            with redirect_stdout(output):
                commands._handle_cli(args)
        finally:
            commands._render_quota = original

        self.assertEqual(output.getvalue().strip(), "provider=grok")


if __name__ == "__main__":
    unittest.main()
