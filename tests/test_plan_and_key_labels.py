"""Plan names and key-slot labels shown in the quota widget.

Mocks only. Pins OpenAI's public plan names (prolite -> Pro 100, pro -> Pro 200)
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
        self.assertEqual(builtin._codex_plan_label("prolite"), "Pro 100")
        self.assertEqual(builtin._codex_plan_label("pro"), "Pro 200")
        self.assertEqual(builtin._codex_plan_label("plus"), "Plus")

    def test_unknown_plan_keeps_title_case_and_empty_is_none(self):
        self.assertEqual(builtin._codex_plan_label("synthetic-future"), "Synthetic-Future")
        self.assertIsNone(builtin._codex_plan_label(""))
        self.assertIsNone(builtin._codex_plan_label(None))


class CodexAccountNameTests(unittest.TestCase):
    def test_generic_login_label_falls_back_to_profile_email(self):
        from quota_providers import codex
        claims = {"https://api.openai.com/profile": {"email": "someone@example.com"}}
        row = {"label": "device_code"}
        self.assertEqual(codex._explicit_display_label(row, claims), "someone@example.com")

    def test_user_alias_wins_over_email(self):
        from quota_providers import codex
        claims = {"https://api.openai.com/profile": {"email": "someone@example.com"}}
        self.assertEqual(codex._explicit_display_label({"label": "Trabalho"}, claims), "Trabalho")

    def test_missing_profile_gives_no_name(self):
        from quota_providers import codex
        self.assertEqual(codex._explicit_display_label({"label": "device_code"}, {}), "")


class ClaudePlanTests(unittest.TestCase):
    def test_subscription_type_read_from_credentials_file(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as d:
            Path(d, ".credentials.json").write_text(
                _json.dumps({"claudeAiOauth": {"subscriptionType": "team"}}))
            self.assertEqual(builtin._claude_subscription_type(d), "Team")

    def test_missing_or_non_string_plan_is_none(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(builtin._claude_subscription_type(d))
            Path(d, ".credentials.json").write_text(
                _json.dumps({"claudeAiOauth": {"subscriptionType": 5}}))
            self.assertIsNone(builtin._claude_subscription_type(d))


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
        self.assertEqual(labels, ["key 1", "key 2"])


if __name__ == "__main__":
    unittest.main()
