"""Plan names and key-slot labels shown in the quota widget.

Mocks only. Pins OpenAI's public plan names (prolite -> Pro 5x, pro -> Pro 20x)
and that environment-variable keys are named by slot, not "env1".
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers import builtin  # noqa: E402


class CodexPlanLabelTests(unittest.TestCase):
    def test_known_plans_use_public_names(self):
        self.assertEqual(builtin._codex_plan_label("prolite"), "Pro 5x")
        self.assertEqual(builtin._codex_plan_label("pro"), "Pro 20x")
        self.assertEqual(builtin._codex_plan_label("plus"), "Plus")

    def test_unknown_plan_keeps_title_case_and_empty_is_none(self):
        self.assertEqual(builtin._codex_plan_label("synthetic-future"), "Synthetic-Future")
        self.assertIsNone(builtin._codex_plan_label(""))
        self.assertIsNone(builtin._codex_plan_label(None))


class OpencodeKeyLabelTests(unittest.TestCase):
    def test_env_slot_is_not_named_env(self):
        import os
        from unittest import mock
        from quota_providers import opencode_go

        env = {"OPENCODE_GO_API_KEY": "sk-one", "OPENCODE_GO_API_KEY_2": "sk-two"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(opencode_go, "_pool_entries_from_store", return_value=[], create=True):
            entries = opencode_go._pool_entries()
        labels = [label for _token, label in entries]
        self.assertTrue(all(not label.startswith("env") for label in labels), labels)


if __name__ == "__main__":
    unittest.main()
