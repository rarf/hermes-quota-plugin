"""Codex special-reset announcement in every Desktop quota surface.

Runs the real ``desktop/plugin.js`` through the shared offline renderer
(``tests/widget_renderer.cjs``) - no network, credentials or DOM. The
announcement is provider-level metadata from the cache root, never an
account entitlement, so the tests also pin down that it is shown once and
never per account.
"""

from __future__ import annotations

import unittest

from widget_harness import nodes, render, text, tooltip_text

SOURCE = "https://x.com/thsottiaux/status/2113162413008884099"
CREDIT = "https://codex-resets.com/"


def announcement(**overrides):
    value = {
        "id": "reset-2113162413008884099",
        "status": "scheduled",
        "reset_type": "regular",
        "announced_at": "2026-10-22T06:55:54Z",
        "source_url": SOURCE,
    }
    value.update(overrides)
    return value


def special_root(value=None, **overrides):
    root = {
        "announcement": announcement() if value is None else value,
        "fetched_at": "2026-10-22T12:00:00Z",
        "stale": False,
        "unavailable_reason": None,
    }
    root.update(overrides)
    return root


def codex_account(label, used):
    return {
        "label": label,
        "windows": [
            {"label": "Session", "used_percent": used, "reset_at": "2026-10-23T13:00:00Z"},
            {"label": "Weekly", "used_percent": used + 10, "reset_at": "2026-10-28T14:30:00Z"},
        ],
    }


def payload(codex=True, root=None, **extra):
    providers = {}
    if codex:
        providers["openai-codex"] = {
            "accounts": [codex_account("Personal", 20), codex_account("Work", 60)]
        }
    providers["anthropic"] = {
        "label": "Work",
        "windows": [{"label": "Weekly", "used_percent": 10, "reset_at": "2026-10-29T10:00:00Z"}],
    }
    data = {
        "fetched_at": "2026-10-22T12:00:00Z",
        "age_s": 4,
        "plugin_version": "2.11.0",
        "providers": providers,
    }
    if root is not None:
        data["codex_special_reset"] = root
    data.update(extra)
    return data


class SpecialResetDesktopTests(unittest.TestCase):
    def pane(self, data, mode="dense", **kwargs):
        return text(render(data=data, mode=mode, **kwargs))

    def status(self, data, mode="all"):
        return render(component="status", data=data, statusMode=mode)

    def test_pane_shows_one_provider_level_announcement_with_credit_and_source(self):
        for mode in ("clean", "dense"):
            with self.subTest(mode=mode):
                rendered = self.pane(payload(root=special_root()), mode=mode)
                self.assertEqual(rendered.count("Special reset announcement"), 1)
                self.assertIn("Announced Oct 22, 2026", rendered)
                self.assertIn("06:55", rendered)  # hour, not just the day
                self.assertIn("Scheduled · waiting for execution", rendered)
                self.assertIn(CREDIT, rendered)
                self.assertIn("View source announcement", rendered)
                self.assertIn(SOURCE, self.hrefs(payload(root=special_root())))
                # Account renewal stays its own value; the announcement date
                # is not borrowed as a reset_at.
                self.assertNotIn("Oct 22, 2026 · Scheduled", rendered.replace("Announced Oct 22, 2026 · Scheduled", ""))
                self.assertNotIn("happened", rendered.lower())
                self.assertNotIn("for you", rendered.lower())

    def test_unknown_schedule_stays_visible(self):
        data = payload(root=special_root(announcement(scheduled_for=None)))
        self.assertIn("Special reset announcement", self.pane(data))
        self.assertIn("Special reset announcement", tooltip_text(self.status(data)))

    def test_elapsed_notice_is_hidden_in_pane_and_status_without_losing_quota(self):
        for scheduled in ("2000-01-01T00:00:00Z", "2000-01-01T01:00:00+01:00"):
            data = payload(root=special_root(announcement(scheduled_for=scheduled)))
            for mode in ("dense", "detailed", "ring"):
                self.assertNotIn("Special reset announcement", self.pane(data, mode=mode))
            self.assertNotIn("Special reset announcement", tooltip_text(self.status(data)))
            self.assertIn("Codex", self.pane(data))

    def test_announcement_hour_follows_the_payload_timezone(self):
        rendered = self.pane(payload(root=special_root(), timezone="Asia/Tokyo"))
        self.assertRegex(rendered, r"0?3:55")  # 06:55Z -> 15:55 Tokyo
        self.assertNotIn("06:55", rendered)

    def test_scheduled_estimate_is_shown_next_to_the_announcement(self):
        root = special_root(announcement(scheduled_for="2099-10-22T17:00:00Z"))
        data = payload(root=root, timezone="Europe/Lisbon")
        rendered = self.pane(data)
        self.assertIn("Scheduled for Oct 22, 2099", rendered)
        self.assertRegex(rendered, r"0?6:00")  # 17:00Z -> 18:00 Lisbon
        self.assertIn("Scheduled for", tooltip_text(self.status(data)))

    def test_banked_and_recorded_wording_is_state_not_execution(self):
        root = special_root(announcement(reset_type="banked", status="recorded",
                                        announced_at="2026-10-20T06:33:48Z"))
        rendered = self.pane(payload(root=root))
        self.assertNotIn("Special reset announcement", rendered)
        self.assertNotIn("Banked reset", rendered)
        self.assertNotIn("Recorded", rendered)
        self.assertNotIn("executed", rendered.lower())
        self.assertNotIn("happened", rendered.lower())
        self.assertNotIn("for you", rendered.lower())

    def test_stale_announcement_is_visibly_qualified_without_alarm(self):
        root = special_root(stale=True, fetched_at="2026-10-20T12:00:00Z",
                            unavailable_reason="http-429")
        rendered = self.pane(payload(root=root))
        self.assertIn("Special reset announcement", rendered)
        self.assertIn("Stale", rendered)
        self.assertIn("Oct 20, 2026", rendered)
        self.assertIn(CREDIT, rendered)

    def test_missing_or_malformed_freshness_is_not_presented_as_fresh(self):
        for root in (
            {"announcement": announcement()},  # no stale flag, no fetched_at
            special_root(stale=False, fetched_at="not-a-date"),
            special_root(stale=False, fetched_at=None),
        ):
            with self.subTest(root=root):
                rendered = self.pane(payload(root=root))
                self.assertIn("Special reset announcement", rendered)
                self.assertIn("Stale", rendered)
                tip = tooltip_text(self.status(payload(root=root)))
                self.assertIn("Stale", tip)

    def test_malformed_or_absent_metadata_never_breaks_quota(self):
        malformed = [
            None,
            {},
            {"announcement": "text"},
            {"announcement": announcement(status="executing")},
            {"announcement": announcement(reset_type="forecast")},
            {"announcement": announcement(announced_at="2026-10-22T06:55:54")},
            {"announcement": announcement(announced_at="22/10/2026")},
            {"announcement": announcement(source_url="https://evil.example/status/1")},
            {"announcement": announcement(source_url="http://x.com/thsottiaux/status/1")},
            {"announcement": announcement(source_url="https://x.com/thsottiaux/status/not-digits")},
            {"announcement": {**announcement(), "id": ""}},
        ]
        for root in malformed:
            with self.subTest(root=root):
                rendered = self.pane(payload(root=root))
                self.assertNotIn("Special reset announcement", rendered)
                self.assertNotIn(CREDIT, rendered)
                self.assertNotIn("thsottiaux", rendered)
                self.assertIn("OpenAI Codex", rendered)  # quota remains usable
                self.assertIn("% left", rendered)

    def test_announcement_is_codex_only_even_when_other_metadata_is_present(self):
        data = payload(codex=False, root=special_root())
        self.assertNotIn("Special reset announcement", self.pane(data))
        self.assertNotIn(CREDIT, self.pane(data))
        # Disabling Codex hides its provider notice as well.
        rendered = self.pane(payload(root=special_root()), disabled=["openai-codex"])
        self.assertNotIn("Special reset announcement", rendered)

    def test_statusbar_hover_includes_one_announcement_in_all_and_worst_modes(self):
        data = payload(root=special_root())
        for mode in ("all", "worst"):
            with self.subTest(mode=mode):
                tip = tooltip_text(self.status(data, mode=mode))
                self.assertEqual(tip.count("Special reset announcement"), 1, tip)
                self.assertIn("Announced Oct 22, 2026", tip)
                self.assertIn("Scheduled · waiting for execution", tip)
                self.assertIn(CREDIT, tip)
                self.assertIn(SOURCE, tip)
                self.assertNotIn("happened", tip.lower())
                self.assertNotIn("for you", tip.lower())

    def test_statusbar_hover_hides_announcement_outside_codex(self):
        data = payload(codex=False, root=special_root())
        for mode in ("all", "worst"):
            with self.subTest(mode=mode):
                tip = tooltip_text(self.status(data, mode=mode))
                self.assertNotIn("Special reset announcement", tip)
                self.assertNotIn(CREDIT, tip)

    def test_statusbar_hover_qualifies_stale_and_rejects_unsafe_source(self):
        root = special_root(announcement(announced_at="2026-10-20T06:33:48Z"), stale=True)
        tip = tooltip_text(self.status(payload(root=root)))
        self.assertIn("Stale", tip)
        self.assertIn("Announced Oct 20, 2026", tip)
        bad = special_root(announcement(source_url="https://evil.example/status/1"))
        self.assertNotIn("evil.example", tooltip_text(self.status(payload(root=bad))))

    def test_link_affordances_expose_only_fixed_or_allowlisted_urls(self):
        data = payload(root=special_root())
        tree = render(data=data)
        urls = [
            node["props"].get("href")
            for node in nodes(tree)
            if node.get("type") == "a" and node.get("props", {}).get("href")
        ]
        self.assertIn(CREDIT, urls)
        self.assertIn(SOURCE, urls)
        self.assertTrue(all(url.startswith("https://") for url in urls), urls)
        bad = special_root(announcement(source_url="https://evil.example/status/1"))
        self.assertEqual(self.hrefs(bad), [])

    def test_special_reset_links_open_through_desktop_os_bridge(self):
        tree = render(data=payload(root=special_root()), clickLinks=True)
        self.assertEqual(tree["openedExternal"], [CREDIT, SOURCE])

    def test_special_reset_notice_is_inside_openai_card_when_another_provider_precedes_it(self):
        data = payload(root=special_root())
        data["providers"] = {"anthropic": data["providers"]["anthropic"],
                             "openai-codex": data["providers"]["openai-codex"]}
        tree = render(data=data)

        def subtree_text(node):
            if isinstance(node, dict):
                children = node.get("props", {}).get("children")
                return (str(children) if isinstance(children, str) else "") + " " + subtree_text(children)
            if isinstance(node, list):
                return " ".join(subtree_text(child) for child in node)
            return "" if node is None else str(node)

        cards = [node for node in nodes(tree)
                 if "rounded-lg border" in node.get("props", {}).get("className", "")]
        self.assertTrue(any("OpenAI Codex" in subtree_text(card)
                            and "Special reset announcement" in subtree_text(card)
                            for card in cards), "announcement should be nested in OpenAI Codex card")

    def hrefs(self, data):
        tree = render(data=data)
        return [
            node["props"].get("href")
            for node in nodes(tree)
            if node.get("type") == "a" and node.get("props", {}).get("href")
        ]


if __name__ == "__main__":
    unittest.main()
