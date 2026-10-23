"""read_config without a YAML library (CI's bare interpreters have neither ruamel nor PyYAML).

The fallback must still read the keys the monitor uses: JSON (valid YAML) and a
block-style subset (nested mappings, scalar lists, lists of mappings, comments).
"""
from __future__ import annotations

import builtins
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from monitor_fixtures import load

common = load("quota_monitor.common")

_real_import = builtins.__import__


def _no_yaml(name, *args, **kwargs):
    if name == "yaml" or name.startswith("ruamel"):
        raise ImportError(name)
    return _real_import(name, *args, **kwargs)


BLOCK = """\
# synthetic Hermes config
model:
  default: claude-test-model   # trailing comment
  provider: anthropic
credential_pool_strategies:
  anthropic: round_robin
  "openai-codex": 'least_used'
fallback_providers:
- provider: openrouter
  model: synthetic/model-a
- provider: nous
  model: synthetic/model-b
toolsets:
  - terminal
  - web
compression:
  enabled: true
  threshold: 0.85
  keep: 3
empty:
"""


class MiniYamlFallbackTest(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.yaml").write_text(text, encoding="utf-8")
            with mock.patch.object(builtins, "__import__", _no_yaml):
                return common.read_config(Path(d))

    def test_block_subset(self):
        cfg = self.read(BLOCK)
        self.assertEqual(cfg["model"], {"default": "claude-test-model", "provider": "anthropic"})
        self.assertEqual(cfg["credential_pool_strategies"],
                         {"anthropic": "round_robin", "openai-codex": "least_used"})
        self.assertEqual(cfg["fallback_providers"], [
            {"provider": "openrouter", "model": "synthetic/model-a"},
            {"provider": "nous", "model": "synthetic/model-b"},
        ])
        self.assertEqual(cfg["toolsets"], ["terminal", "web"])
        self.assertEqual(cfg["compression"], {"enabled": True, "threshold": 0.85, "keep": 3})
        self.assertIsNone(cfg["empty"])

    def test_json_is_yaml(self):
        self.assertEqual(self.read('{"credential_pool_strategies": {"anthropic": "fill_first"}}'),
                         {"credential_pool_strategies": {"anthropic": "fill_first"}})

    def test_unreadable_is_empty_not_an_error(self):
        self.assertEqual(self.read("model: [unclosed\n  - : :"), {})
        self.assertEqual(self.read(""), {})

    def test_ruamel_flow_document_with_plain_keys(self):
        # What `hermes config set` leaves in a config.yaml that was written as JSON.
        text = ('{"model": {"provider": "openrouter", "default": "synth/model-b"}, '
                '"fallback_providers": [{"provider": "anthropic", "model": "claude-synth"}], '
                'credential_pool_strategies: {anthropic: least_used}}\n')
        self.assertEqual(self.read(text), {
            "model": {"provider": "openrouter", "default": "synth/model-b"},
            "fallback_providers": [{"provider": "anthropic", "model": "claude-synth"}],
            "credential_pool_strategies": {"anthropic": "least_used"},
        })

    def test_simple_flow_values(self):
        cfg = self.read('toolsets: [terminal, "web"]\nmodel: {"default": "claude-test-model"}\n')
        self.assertEqual(cfg, {"toolsets": ["terminal", "web"], "model": {"default": "claude-test-model"}})

    def test_broken_ruamel_falls_through_to_the_next_parser(self):
        # A ruamel whose safe loader raises (not an ImportError) must not mask the config as empty.
        def broken_ruamel(name, *args, **kwargs):
            if name.startswith("ruamel"):
                raise RuntimeError("could not determine a constructor for the tag 'None'")
            if name == "yaml":
                raise ImportError(name)
            return _real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.yaml").write_text(BLOCK, encoding="utf-8")
            with mock.patch.object(builtins, "__import__", broken_ruamel):
                cfg = common.read_config(Path(d))
        self.assertEqual(cfg["model"], {"default": "claude-test-model", "provider": "anthropic"})


if __name__ == "__main__":
    unittest.main()
