"""QuotaResult keeps its original positional field order (backwards compat).

Third-party fetchers and older plugin revisions may construct ``QuotaResult``
positionally. New structured fields must be appended after the long-standing
``accounts`` / ``account_label`` pair, otherwise an old 8th positional argument
(``accounts``) would silently bind to ``access_state`` and the record would lose
its accounts. These tests pin the order.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_providers.base import QuotaAccount, QuotaResult


class QuotaResultPositionalCompatibilityTests(unittest.TestCase):
    def test_legacy_positional_accounts_and_label_still_bind(self):
        account = QuotaAccount(id="work", label="Work")
        result = QuotaResult(
            "anthropic", [], None, None, [], [], None, [account], "Work",
        )
        self.assertEqual(result.label, "anthropic")
        self.assertEqual(result.accounts, [account])
        self.assertEqual(result.account_label, "Work")
        self.assertIsNone(result.access_state)
        self.assertEqual(result.detail_facts, {})

    def test_new_fields_come_after_the_legacy_positional_fields(self):
        names = [field.name for field in dataclasses.fields(QuotaResult)]
        self.assertEqual(
            names[:9],
            [
                "label",
                "windows",
                "plan",
                "unavailable_reason",
                "details",
                "account_balances",
                "api_calls_available",
                "accounts",
                "account_label",
            ],
        )
        for new_field in ("access_state", "detail_facts", "account_org_key"):
            self.assertGreater(names.index(new_field), names.index("account_label"))

    def test_keyword_construction_keeps_new_fields_independent(self):
        result = QuotaResult(
            label="nous",
            accounts=[QuotaAccount(id="a")],
            account_label="A",
            access_state="active",
            detail_facts={"spend": "Spend this period: $1.00"},
        )
        self.assertIsInstance(result.accounts[0], QuotaAccount)
        self.assertEqual(result.accounts[0].id, "a")
        self.assertEqual(result.account_label, "A")
        self.assertEqual(result.access_state, "active")
        self.assertEqual(result.detail_facts["spend"], "Spend this period: $1.00")


if __name__ == "__main__":
    unittest.main()
