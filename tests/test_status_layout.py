"""Layout contract for the /quota and `hermes quota` dashboard.

The dashboard is the human surface: friendly provider names, one block per
account, a bar per real meter, worst quota first, and no rows for providers this
machine has no credential for. These tests pin that contract without touching
the cache schema or the Desktop/TUI payloads other integrations consume.
"""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
_LOADED = {}


def _modules():
    """Load the plugin package with a stubbed Hermes home boundary, once."""
    if "commands" in _LOADED:
        return _LOADED["commands"], _LOADED["plugin"]
    name = "quota_layout_tests"
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(ROOT)]
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: ROOT
    with mock.patch.dict(sys.modules, {name: pkg, "hermes_constants": constants}):
        _LOADED["commands"] = importlib.import_module(name + ".commands")
        _LOADED["plugin"] = importlib.import_module(name + ".__init__")
    return _LOADED["commands"], _LOADED["plugin"]


def render(cache, provider_filter=None):
    cmd, _plugin = _modules()
    with mock.patch.object(cmd, "read_quota_cache", return_value=cache), \
            mock.patch.object(cmd, "_age_label", return_value="updated 2m ago"):
        return cmd._render_quota(provider_filter)


def window(label="Session", used=0.0, reset_at="2030-01-01T00:00:00+00:00"):
    return {"label": label, "used_percent": used, "reset_at": reset_at}


def record(label, windows=None, details=None, plan=None, reason=None, **extra):
    out = {
        "label": label,
        "plan": plan,
        "unavailable_reason": reason,
        "details": list(details or []),
        "windows": list(windows or []),
        "account_balances": [],
        "api_calls_available": None,
    }
    out.update(extra)
    return out


class QuotaLayoutTests(unittest.TestCase):
    def cache(self, providers):
        return {"fetched_at": "2030-01-01T00:00:00+00:00", "providers": providers}

    def test_providers_without_a_credential_are_hidden(self):
        text = render(self.cache({
            "anthropic": record("anthropic", [window()]),
            "cursor": record("cursor", reason="no-credentials"),
            "opencode-go": record("opencode-go", [window(used=2.0)]),
        }))
        self.assertIn("Anthropic", text)
        self.assertIn("OpenCode Go", text)
        self.assertNotIn("Cursor", text)
        self.assertIn("1 provider without a saved account hidden", text)

    def test_asking_for_a_hidden_provider_explains_why(self):
        text = render(self.cache({
            "anthropic": record("anthropic", [window()]),
            "cursor": record("cursor", reason="no-credentials"),
        }), "cursor")
        self.assertIn("Cursor", text)
        self.assertIn("unavailable (no-credentials)", text)

    def test_a_failing_account_stays_visible(self):
        text = render(self.cache({
            "anthropic:dead": record("anthropic · Dead", reason="config-invalid",
                                     details=["set configDir"], account_label="Dead"),
        }))
        self.assertIn("Anthropic · Dead", text)
        self.assertIn("unavailable (config-invalid)", text)
        self.assertIn("set configDir", text)

    def test_friendly_name_and_account_plan_on_the_heading(self):
        text = render(self.cache({
            "openai-codex": record(
                "openai-codex",
                [window("Weekly", used=96.0)],
                plan="Pro 100",
                account_label="ronaldrj@gmail.com",
            ),
        }))
        self.assertIn("OpenAI Codex · ronaldrj@gmail.com (Pro 100)", text)

    def test_bar_and_percentage_show_what_is_left(self):
        text = render(self.cache({"grok": record("grok", [window(used=76.0)])}))
        self.assertIn("██░░░░░░░░", text)
        self.assertIn("24% left", text)

    def test_worst_quota_first(self):
        text = render(self.cache({
            "anthropic": record("anthropic", [window(used=10.0)], plan="Max"),
            "grok": record("grok", [window(used=84.0)]),
            "copilot": record("copilot", [window(used=0.0)]),
        }))
        self.assertLess(text.index("xAI Grok"), text.index("Anthropic"))
        self.assertLess(text.index("Anthropic"), text.index("Copilot"))

    def test_window_without_a_percentage_says_no_data(self):
        text = render(self.cache({
            "deepseek": record("deepseek", [window(used=None)]),
        }))
        self.assertIn("no data", text)
        self.assertNotIn("% left", text)

    def test_details_belong_to_their_provider_block(self):
        text = render(self.cache({
            "nous": record("nous", details=["Spend this period: $0.00"], plan="Tier 5"),
        }))
        self.assertIn("Nous Portal (Tier 5)", text)
        self.assertIn("· Spend this period: $0.00", text)

    def test_alias_and_account_label_both_filter(self):
        cache = self.cache({
            "anthropic": record("anthropic", [window(used=10.0)]),
            "anthropic:account2": record(
                "anthropic · Conta 2", [window(used=100.0)],
                account_label="Conta 2", provider="anthropic"),
        })
        by_alias = render(cache, "claude")
        self.assertIn("Anthropic", by_alias)
        self.assertIn("Conta 2", by_alias)
        by_account = render(cache, "conta 2")
        self.assertIn("Conta 2", by_account)
        self.assertEqual(by_account.count("Session"), 1)

    def test_slash_filter_keeps_the_whole_phrase(self):
        cmd, _plugin = _modules()
        cache = self.cache({
            "anthropic": record("anthropic", [window(used=10.0)]),
            "anthropic:account2": record(
                "anthropic · Conta 2", [window(used=100.0)],
                account_label="Conta 2", provider="anthropic"),
        })
        with mock.patch.object(cmd, "read_quota_cache", return_value=cache), \
                mock.patch.object(cmd, "_age_label", return_value="updated now"):
            text = cmd.quota_command("conta 2")
        self.assertIn("Conta 2", text)
        self.assertEqual(text.count("Session"), 1)

    def test_no_account_at_all_is_a_configuration_hint(self):
        text = render(self.cache({
            "cursor": record("cursor", reason="no-credentials"),
            "kimi": record("kimi", reason="no-credentials"),
        }))
        self.assertIn("no provider has a saved account yet", text)

    def test_json_payload_keeps_its_shape_for_the_widgets(self):
        cmd, _plugin = _modules()
        cache = self.cache({"anthropic": record("anthropic", [window()])})
        with mock.patch.object(cmd, "read_quota_cache", return_value=cache), \
                mock.patch.object(cmd, "quota_cache_age_seconds", return_value=7.0):
            payload = json.loads(cmd._render_quota_json())
        # `codex_special_reset` is the additive announcement channel the
        # Desktop/TUI reset notice reads; the dashboard keys must not drift.
        self.assertEqual(
            set(payload),
            {
                "fetched_at", "providers", "age_s", "plugin_version", "installed_sha",
                "codex_special_reset", "timezone",
            },
        )
        self.assertEqual(payload["providers"], cache["providers"])

    def special(self, **overrides):
        entry = {
            "announcement": {
                "id": "2113162413008884099",
                "status": "scheduled",
                "reset_type": "regular",
                "announced_at": "2026-10-22T06:55:54+00:00",
                "source_url": "https://x.com/thsottiaux/status/2113162413008884099",
            },
            "fetched_at": "2026-10-22T11:35:39+00:00",
            "stale": False,
            "unavailable_reason": None,
        }
        entry.update(overrides)
        return entry

    def test_special_reset_announcement_is_one_line_above_the_blocks(self):
        cache = self.cache({"openai-codex": record("openai-codex", [window(used=97.0)])})
        cache["codex_special_reset"] = self.special()
        text = render(cache)
        self.assertIn("Codex special reset", text)
        self.assertIn("announced Oct 22", text)
        self.assertIn("scheduled", text)
        self.assertIn("codex-resets.com", text)
        self.assertLess(text.index("Codex special reset"), text.index("OpenAI Codex"))

    def test_special_reset_announcement_carries_the_local_time(self):
        from datetime import datetime
        cache = self.cache({"openai-codex": record("openai-codex", [window()])})
        cache["codex_special_reset"] = self.special()
        cmd, _plugin = _modules()
        expected = datetime.fromisoformat(
            "2026-10-22T06:55:54+00:00").astimezone(cmd.local_tz()).strftime("%b %d %H:%M")
        self.assertIn(f"announced {expected}", render(cache))

    def test_special_reset_uses_the_configured_zone_not_the_process_zone(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cmd, _plugin = _modules()
        cache = self.cache({"openai-codex": record("openai-codex", [window()])})
        cache["codex_special_reset"] = self.special()
        with mock.patch.object(cmd, "local_tz", return_value=ZoneInfo("Asia/Tokyo")):
            line = cmd._special_reset_line(cache)
        self.assertIn("announced Oct 22 15:55", line)
        self.assertEqual(
            datetime.fromisoformat("2026-10-22T06:55:54+00:00")
            .astimezone(ZoneInfo("Asia/Tokyo")).strftime("%b %d %H:%M"),
            "Oct 22 15:55",
        )

    def test_special_reset_prefers_the_scheduled_estimate_over_the_post_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cmd, _plugin = _modules()
        cache = self.cache({"openai-codex": record("openai-codex", [window()])})
        entry = self.special()
        entry["announcement"]["scheduled_for"] = "2026-10-22T17:00:00+00:00"
        cache["codex_special_reset"] = entry
        with mock.patch.object(cmd, "local_tz", return_value=ZoneInfo("Europe/Lisbon")), \
                mock.patch.object(cmd, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-22T12:00:00+00:00")
            line = cmd._special_reset_line(cache)
        self.assertIn("scheduled Oct 22 18:00", line)
        self.assertIn("announced 07:55", line)
        self.assertEqual(
            datetime.fromisoformat("2026-10-22T17:00:00+00:00")
            .astimezone(ZoneInfo("Europe/Lisbon")).strftime("%b %d %H:%M"),
            "Oct 22 18:00",
        )

    def test_banked_special_reset_keeps_its_own_wording(self):
        cache = self.cache({"openai-codex": record("openai-codex", [window()])})
        entry = self.special()
        entry["announcement"]["reset_type"] = "banked"
        cache["codex_special_reset"] = entry
        self.assertIn("Codex banked reset", render(cache))

    def test_invalid_special_reset_is_not_rendered(self):
        cache = self.cache({"openai-codex": record("openai-codex", [window()])})
        entry = self.special()
        entry["announcement"]["source_url"] = "https://example.com/not-a-post"
        cache["codex_special_reset"] = entry
        text = render(cache)
        self.assertNotIn("Codex special reset", text)
        self.assertNotIn("codex-resets.com", text)

    def test_special_reset_only_appears_for_the_codex_view(self):
        cache = self.cache({
            "openai-codex": record("openai-codex", [window()]),
            "grok": record("grok", [window()]),
        })
        cache["codex_special_reset"] = self.special()
        self.assertIn("Codex special reset", render(cache, "codex"))
        self.assertNotIn("Codex special reset", render(cache, "grok"))

    def test_footer_block_still_lists_every_dated_row(self):
        _cmd, plugin = _modules()
        cache = self.cache({
            "anthropic": record("anthropic", [window(used=10.0)]),
            "cursor": record("cursor", reason="no-credentials"),
        })
        block = plugin._format_quota_block(cache)
        self.assertIn("• anthropic: Session 90%", block)

    def test_nous_total_usable_balance_uses_its_own_label(self):
        text = render(self.cache({
            "nous": record(
                "nous",
                plan="Free",
                details=["Free tier - free models only"],
                account_balances=[{
                    "currency": "USD", "total_balance": "10.00",
                    "label": "Total usable",
                }],
                api_calls_available=True,
            ),
        }))
        self.assertIn("Nous Portal (Free)", text)
        self.assertIn("Total usable: $10.00 USD", text)
        self.assertIn("Free tier - free models only", text)

    def test_non_usd_balance_never_uses_a_dollar_prefix(self):
        text = render(self.cache({
            "nous": record(
                "nous",
                account_balances=[{
                    "currency": "CNY", "total_balance": "25.125",
                }],
            ),
        }))
        self.assertIn("Account balance: 25.125 CNY", text)
        self.assertNotIn("$25.125", text)
        self.assertNotIn("$", text)

    def test_balance_label_strips_terminal_control_characters(self):
        text = render(self.cache({
            "nous": record(
                "nous",
                account_balances=[{
                    "currency": "USD", "total_balance": "8.00",
                    "label": "Total\x1b[31m usable\x07\u202e",
                }],
            ),
        }))
        self.assertIn("Total usable: $8.00 USD", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertNotIn("\u202e", text)


if __name__ == "__main__":
    unittest.main()
