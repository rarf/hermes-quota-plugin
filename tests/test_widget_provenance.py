"""Widget provenance contract (runs the real desktop/plugin.js offline)."""
import unittest

from widget_harness import render, text


class WidgetProvenanceTests(unittest.TestCase):
    def _row(self, provider, pid="openai-codex"):
        return text(render(component="row", id=pid, provider=provider))

    def test_pane_shows_source_and_billing_mode(self):
        out = self._row({
            "label": "openai-codex",
            "plan": "Pro 100",
            "windows": [{"label": "Session", "used_percent": 20}],
            "details": [],
            "provenance": {
                "source": "chatgpt.com backend usage endpoint (codex identity)",
                "billing_mode": "subscription",
                "meter": "subscription allowance",
                "plan_source": "provider-response",
            },
        })
        self.assertIn("chatgpt.com backend usage endpoint", out)
        self.assertIn("subscription allowance", out)

    def test_pane_marks_a_verified_plan_source(self):
        out = self._row({
            "label": "openai-codex",
            "plan": "Pro 100",
            "windows": [{"label": "Session", "used_percent": 20}],
            "details": [],
            "provenance": {"plan_source": "provider-response", "billing_mode": "subscription"},
        })
        self.assertIn("Pro 100 (provider-response)", out)

    def test_pane_marks_a_user_label_plan_as_unverified(self):
        out = self._row({
            "provider": "anthropic",
            "label": "anthropic · Work",
            "plan": "Team",
            "windows": [{"label": "Current week", "used_percent": 5}],
            "details": [],
            "provenance": {"source": "api.anthropic.com/api/oauth/usage",
                           "plan_source": "user-label", "billing_mode": "subscription"},
        }, pid="anthropic:work")
        self.assertIn("api.anthropic.com/api/oauth/usage", out)
        # A user-chosen label is disclosed as such; it is never shown as a
        # verified plan with no provenance marker.
        self.assertIn("Team (user-label)", out)

    def test_pane_surfaces_a_budget_scoping_note(self):
        out = self._row({
            "label": "grok",
            "windows": [{"label": "Weekly Limit", "used_percent": 3}],
            "details": [],
            "provenance": {"source": "grok.com GetGrokCreditsConfig (Build/weekly)",
                           "meter": "subscription allowance", "billing_mode": "subscription",
                           "note": "whether this shares the Hermes xai-oauth budget "
                                   "cannot be established from the browser session."},
        }, pid="grok")
        self.assertIn("xai-oauth", out)


if __name__ == "__main__":
    unittest.main()
