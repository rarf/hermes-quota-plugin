"""Codex special reset together with the fork's chip alerts, i18n and timezone.

Real `desktop/plugin.js` through `tests/widget_monitor_renderer.cjs` (TZ=UTC),
SYNTHETIC payloads only. Upstream's special-reset notice and the fork's
status-bar alerts (reset countdown / time to full) must live on the SAME chip,
the notice text follows the app language, and every timestamp follows the
payload's configured `timezone` while its format follows the app language.
"""
import unittest

from test_widget_monitor import iso, quota_payload, run, snapshot
from widget_harness import nodes, text, tooltip_text

SOURCE = "https://x.com/thsottiaux/status/2113162413008884099"
CREDIT = "https://codex-resets.com/"


def special_root(**announcement):
    value = {
        "id": "reset-2113162413008884099",
        "status": "scheduled",
        "reset_type": "regular",
        "announced_at": "2026-10-22T06:55:54Z",
        "source_url": SOURCE,
    }
    value.update(announcement)
    return {"announcement": value, "fetched_at": "2026-10-22T12:00:00Z", "stale": False}


def payload(**extra):
    data = quota_payload()
    data["providers"]["openai-codex"] = {"accounts": [{
        "label": "work",
        "windows": [
            {"label": "Session", "used_percent": 92, "reset_at": iso(3600)},
            {"label": "Weekly", "used_percent": 40, "reset_at": iso(4 * 86400)},
        ],
    }]}
    data["codex_special_reset"] = special_root()
    data.update(extra)
    return data


def attr(tree, name):
    return [n["props"][name] for n in nodes(tree) if name in n.get("props", {})]


def codex_chip(tree):
    """The status-bar chip node whose aria-label names OpenAI Codex."""
    buttons = [n for n in nodes(tree) if n.get("type") == "button"
               and "Codex" in str(n.get("props", {}).get("aria-label", ""))]
    assert len(buttons) == 1, buttons
    return buttons[0]


class SpecialResetWithChipAlertsTest(unittest.TestCase):
    def test_all_mode_codex_chip_has_alert_and_special_tip(self):
        out = run(component="status", data=payload(), monitor=snapshot())
        tree = out["tree"]
        chip = codex_chip(tree)
        # Fork: the countdown alert is on the Codex chip (92 % used).
        self.assertTrue(attr(chip, "data-chip-reset"), chip)
        self.assertRegex(chip["props"]["aria-label"], r"resets in (59m|1h 0m)")
        # Upstream: the announcement is in the hover tip, exactly once.
        tip = tooltip_text(tree)
        self.assertEqual(tip.count("Special reset announcement"), 1, tip)
        self.assertIn("Scheduled · waiting for execution", tip)
        self.assertIn(f"Data: {CREDIT}", tip)
        self.assertIn(f"Source: {SOURCE}", tip)

    def test_worst_mode_single_chip_keeps_alert_ring_and_special_suffix(self):
        out = run(component="status", data=payload(), monitor=snapshot(),
                  storage={"statusbarMode": "worst"})
        tree = out["tree"]
        self.assertEqual(len(attr(tree, "data-chip-reset")), 1)
        tip = tooltip_text(tree)
        self.assertEqual(tip.count("Special reset announcement"), 1, tip)
        self.assertIn("Quota breakdown:", tip)
        self.assertIn(CREDIT, tip)

    def test_alert_toggle_off_still_shows_special_tip(self):
        out = run(component="status", data=payload(), monitor=snapshot(),
                  storage={"statusbarQuotaAlerts": False})
        self.assertEqual(attr(out["tree"], "data-chip-alert"), [])
        self.assertIn("Special reset announcement", tooltip_text(out["tree"]))

    def test_pane_keeps_monitor_extras_and_special_notice(self):
        out = run(data=payload(), monitor=snapshot())
        rendered = text(out["tree"])
        self.assertEqual(rendered.count("Special reset announcement"), 1)
        self.assertIn("OpenAI Codex", rendered)
        self.assertTrue(attr(out["tree"], "data-quota-special-reset"))


class SpecialResetTranslatedTest(unittest.TestCase):
    def test_turkish_tip_and_notice(self):
        data = payload()
        status = run(component="status", data=data, monitor=snapshot(), locale="tr")
        tip = tooltip_text(status["tree"])
        self.assertIn("Özel sıfırlama duyurusu", tip)
        self.assertIn("Planlandı · uygulanması bekleniyor", tip)
        self.assertIn(f"Veri: {CREDIT}", tip)
        self.assertNotIn("Special reset", tip)
        self.assertNotIn("Scheduled", tip)
        pane = text(run(data=data, monitor=snapshot(), locale="tr")["tree"])
        self.assertIn("Kaynak duyuruyu görüntüle", pane)
        self.assertIn("Duyuru:", pane)
        # Turkish date format (month name in Turkish), not en-US.
        self.assertIn("Eki", pane)
        self.assertNotIn("Oct", pane)

    def test_banked_stale_strings_are_translated(self):
        root = special_root(reset_type="banked", status="scheduled")
        root["stale"] = True
        tip = tooltip_text(run(component="status", data=payload(codex_special_reset=root),
                               monitor=snapshot(), locale="de")["tree"])
        self.assertIn("Angesparter Reset · Geplant", tip)
        self.assertIn("Veraltet · geprüft", tip)
        self.assertNotIn("Banked", tip)

    def test_no_raw_special_key_in_any_language(self):
        for locale in ("en", "tr", "de", "es", "fr", "ru", "ja", "zh", "zh-hant", "ar"):
            with self.subTest(locale=locale):
                status = run(component="status", data=payload(), monitor=snapshot(), locale=locale)
                pane = run(data=payload(), monitor=snapshot(), locale=locale)
                blob = tooltip_text(status["tree"]) + text(pane["tree"])
                self.assertNotIn("specialReset", blob)


class TimezoneAndLocaleTest(unittest.TestCase):
    def test_announcement_uses_payload_zone_and_app_language(self):
        data = payload(timezone="Asia/Tokyo")  # 06:55Z -> 15:55 Tokyo
        en = text(run(data=data, monitor=snapshot())["tree"])
        self.assertRegex(en, r"0?3:55")
        tr = text(run(data=data, monitor=snapshot(), locale="tr")["tree"])
        self.assertIn("15:55", tr)  # Turkish 24 h clock, Tokyo wall time
        self.assertNotIn("06:55", tr)

    def test_reset_times_follow_payload_zone(self):
        # 23:30Z is 08:30 the NEXT day in Tokyo; host TZ is UTC.
        out = run(expr='(() => { noteDataTimeZone({timezone: "Asia/Tokyo"});'
                       ' const tokyo = absoluteReset("2030-01-01T23:30:00Z");'
                       ' noteDataTimeZone({timezone: ""});'
                       ' const host = absoluteReset("2030-01-01T23:30:00Z");'
                       ' return [tokyo, host]; })()')["value"]
        self.assertIn("Jan 2", out[0])
        self.assertRegex(out[0], r"8:30")
        self.assertIn("Jan 1", out[1])
        self.assertRegex(out[1], r"11:30")

    def test_invalid_zone_falls_back_to_host_clock(self):
        out = run(expr='(() => { noteDataTimeZone({timezone: "Not/AZone"});'
                       ' return [_dataTimeZone, absoluteReset("2030-01-01T23:30:00Z")]; })()')["value"]
        self.assertEqual(out[0], "")
        self.assertRegex(out[1], r"11:30")

    def test_calendar_day_labels_never_shift_with_zone(self):
        out = run(expr='(() => { noteDataTimeZone({timezone: "Pacific/Kiritimati"});'
                       ' const a = intl("DateTimeFormat", {weekday: "short", timeZone: "UTC"})'
                       '.format(new Date("2030-01-01T12:00:00Z"));'
                       ' noteDataTimeZone({timezone: ""}); return a; })()')["value"]
        self.assertEqual(out, "Tue")


if __name__ == "__main__":
    unittest.main()
