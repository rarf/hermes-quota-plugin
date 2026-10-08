"""Account identifier and display label validation regression tests."""
import unittest
from unittest import mock
import sys
from test_claude_multi_account import _config_modules
from quota_providers import builtin


class AccountValidationTests(unittest.TestCase):
    def validate(self, account_id="work", label="Work"):
        settings = {"claudeAccounts": [{"id": account_id, "label": label, "configDir": "~/synthetic-claude"}]}
        with mock.patch.dict(sys.modules, _config_modules(settings)):
            return builtin._claude_accounts_setting()[0][0]

    def test_rejects_ambiguous_or_control_identifiers(self):
        for identifier in ["work:other", "with space", "a\nsecret", "équipe", "_reserved", "x" * 65]:
            with self.subTest(identifier=identifier):
                row = self.validate(identifier)
                self.assertEqual(row.get("error"), "config-invalid")
                self.assertNotIn(identifier, row.get("detail", ""))

    def test_accepts_portable_identifiers(self):
        for identifier in ["work", "account-2", "personal.main", "Team_1"]:
            self.assertEqual(self.validate(identifier)["id"], identifier)

    def test_rejects_long_or_control_labels_without_echo(self):
        for label in ["x" * 81, "Work\nSecret", "tab\tlabel", "\x00"]:
            with self.subTest(label=label):
                row = self.validate(label=label)
                self.assertEqual(row.get("error"), "config-invalid")
                self.assertNotIn("label", row)

    def test_accepts_unicode_display_labels(self):
        self.assertEqual(self.validate(label="Pessoal · Português")["label"], "Pessoal · Português")
