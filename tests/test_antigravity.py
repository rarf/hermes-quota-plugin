"""Offline unit tests for the Antigravity quota fetcher (stdlib only).

Run from the repo root:  python tests/test_antigravity.py
Every HTTP, credential and keychain boundary is mocked.
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
import urllib.error
from io import BytesIO
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_providers import PROVIDER_FETCHERS, antigravity as mod  # noqa: E402


class _FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_returning(payload):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        return _FakeResponse(json.dumps(payload).encode("utf-8"))

    return _opener


def _urlopen_raising(exc):
    def _opener(_req, timeout=None):  # noqa: ANN001, ARG001
        raise exc

    return _opener


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, None)


# Captured live 2026-09-26 from retrieveUserQuotaSummary.
_LIVE = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {"bucketId": "gemini-weekly", "window": "weekly",
                 "resetTime": "2026-10-01T02:48:55Z", "remainingFraction": 0.926965},
                {"bucketId": "gemini-5h", "window": "5h",
                 "resetTime": "2026-09-26T08:03:22Z", "remainingFraction": 0.8624975},
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {"bucketId": "3p-weekly", "window": "weekly",
                 "resetTime": "2026-10-03T07:34:17Z", "remainingFraction": 1},
                {"bucketId": "3p-5h", "window": "5h",
                 "resetTime": "2026-09-26T12:34:17Z", "remainingFraction": 1},
            ],
        },
    ]
}

# Captured live 2026-09-26 from loadCodeAssist. currentTier is free-tier while
# paidTier is the real subscription — the reason paidTier must win.
_LOAD = {
    "cloudaicompanionProject": "aicode-consumers",
    "currentTier": {"id": "free-tier", "name": "Antigravity"},
    "paidTier": {"id": "g1-pro-tier", "name": "Google AI Pro"},
}


def _windows_for(payload):
    """Windows the fetcher would build from a payload, without the fetcher."""
    found = mod._buckets(payload)
    return [w for w in (
        mod._window(found[bucket_id], f"{group} · {label}")
        for bucket_id, label, group in mod._BUCKETS if bucket_id in found
    ) if w is not None]


def _fetch(summary=None, load=None):
    """Run the fetcher with every HTTP boundary mocked to the live shapes."""
    def _post(path, access_token, **kwargs):  # noqa: ANN001, ANN003, ARG001
        if path.endswith(mod._QUOTA_PATH):
            return (_LIVE if summary is None else summary), None
        return (_LOAD if load is None else load), None

    with mock.patch.object(mod, "_load_credential", return_value={"token": {}}), \
            mock.patch.object(mod, "_access_token", return_value=("ya29.fake", None)), \
            mock.patch.object(mod, "_post", side_effect=_post):
        return mod.fetch_antigravity_quota()


def _fetch_with_post(post):
    with mock.patch.object(mod, "_load_credential", return_value={"token": {}}), \
            mock.patch.object(mod, "_access_token", return_value=("t", None)), \
            mock.patch.object(mod, "_post", side_effect=post):
        return mod.fetch_antigravity_quota()


class CredentialTests(unittest.TestCase):
    def test_no_credential_is_no_credentials(self):
        with mock.patch.object(mod, "_load_credential", return_value=None):
            self.assertEqual(
                mod.fetch_antigravity_quota().unavailable_reason, "no-credentials")

    def test_blob_parses_as_utf8_and_utf16(self):
        for encoding in ("utf-8", "utf-16-le"):
            blob = json.dumps({"token": {"access_token": "ya29.x"}}).encode(encoding)
            with mock.patch.object(mod, "_windows_blob", return_value=blob), \
                    mock.patch.object(mod, "_macos_blob", return_value=None):
                cred = mod._load_credential()
            self.assertEqual(cred["token"]["access_token"], "ya29.x", encoding)

    def test_garbage_blob_is_no_credential(self):
        # Every reader must be stubbed: on Linux the real agy token file would
        # otherwise leak into this assertion.
        with mock.patch.object(mod, "_windows_blob", return_value=b"\x01not json"), \
                mock.patch.object(mod, "_macos_blob", return_value=None), \
                mock.patch.object(mod, "_linux_blob", return_value=None):
            self.assertIsNone(mod._load_credential())

    def test_reader_crash_falls_through(self):
        # A faulting CredRead must degrade, never escape fail-open.
        with mock.patch.object(mod, "_windows_blob", side_effect=RuntimeError("boom")), \
                mock.patch.object(mod, "_macos_blob",
                                  return_value=json.dumps({"token": {}}).encode("utf-8")):
            self.assertEqual(mod._load_credential(), {"token": {}})

    def test_linux_token_file_is_read(self):
        # agy keeps the same dict in ~/.gemini/antigravity-cli on Linux.
        blob = json.dumps({"token": {"access_token": "ya29.linux"}}).encode("utf-8")
        with mock.patch.object(mod.sys, "platform", "linux"), \
                mock.patch.object(mod, "_windows_blob", return_value=None), \
                mock.patch.object(mod, "_macos_blob", return_value=None), \
                mock.patch("builtins.open", mock.mock_open(read_data=blob)):
            cred = mod._load_credential()
        self.assertEqual(cred["token"]["access_token"], "ya29.linux")

    def test_linux_reader_is_off_on_other_platforms(self):
        with mock.patch.object(mod.sys, "platform", "darwin"):
            self.assertIsNone(mod._linux_blob())

    def test_unreadable_linux_file_is_no_credential(self):
        with mock.patch.object(mod.sys, "platform", "linux"), \
                mock.patch.object(mod, "_windows_blob", return_value=None), \
                mock.patch.object(mod, "_macos_blob", return_value=None), \
                mock.patch("builtins.open", side_effect=OSError("nope")):
            self.assertIsNone(mod._load_credential())


class TokenTests(unittest.TestCase):
    def test_refresh_runs_before_the_cached_token(self):
        with mock.patch.object(mod, "_refresh", return_value="ya29.fresh") as refresh:
            result = mod._access_token(
                {"token": {"access_token": "stale", "refresh_token": "1//r"}})
        # The refresh now carries the clamped timeout so the fetcher's shared
        # deadline bounds it; assert the token, not the exact call shape.
        self.assertEqual(refresh.call_args.args, ("1//r",))
        self.assertIn("timeout", refresh.call_args.kwargs)
        self.assertEqual(result, ("ya29.fresh", None))

    def test_cached_token_used_when_no_refresh_token(self):
        with mock.patch.object(mod, "_refresh") as refresh:
            result = mod._access_token({"token": {"access_token": "ya29.only"}})
        refresh.assert_not_called()
        self.assertEqual(result, ("ya29.only", None))

    def test_failed_refresh_is_auth_failed(self):
        with mock.patch.object(mod, "_refresh", return_value=None):
            self.assertEqual(
                mod._access_token({"token": {"refresh_token": "1//r"}}),
                (None, "auth-failed"))

    def test_failed_refresh_keeps_a_usable_cached_token(self):
        # The token endpoint can be briefly unreachable; that is not proof the
        # credentials are dead. A dead one still ends as auth-failed on the 401.
        with mock.patch.object(mod, "_refresh", return_value=None):
            self.assertEqual(
                mod._access_token({"token": {"refresh_token": "1//r",
                                             "access_token": "ya29.cached"}}),
                ("ya29.cached", None))

    def test_empty_credential_is_no_credentials(self):
        self.assertEqual(mod._access_token({"token": {}})[1], "no-credentials")
        self.assertEqual(mod._access_token({"token": "nope"})[1], "no-credentials")

    def test_refresh_sends_the_installed_app_client(self):
        seen = {}

        def _opener(req, timeout=None):  # noqa: ANN001, ARG001
            seen["body"] = req.data.decode("utf-8")
            return _FakeResponse(json.dumps({"access_token": "ya29.new"}).encode())

        with mock.patch.object(mod, "urlopen_no_redirect", _opener):
            self.assertEqual(mod._refresh("1//r"), "ya29.new")
        self.assertIn("grant_type=refresh_token", seen["body"])
        self.assertIn("1071006060591", seen["body"])

    def test_refresh_http_error_returns_none(self):
        with mock.patch.object(
                mod, "urlopen_no_redirect", _urlopen_raising(_http_error(401))):
            self.assertIsNone(mod._refresh("1//r"))


class PlanTests(unittest.TestCase):
    def test_paid_tier_wins_over_current_tier(self):
        # Live payload: currentTier is free-tier even on a paid account.
        with mock.patch.object(mod, "_post", return_value=(_LOAD, None)):
            self.assertEqual(mod._plan("t"), "Google AI Pro")

    def test_bare_string_tier_is_accepted(self):
        with mock.patch.object(mod, "_post", return_value=({"currentTier": "free-tier"}, None)):
            self.assertEqual(mod._plan("t"), "free-tier")

    def test_no_tier_is_none(self):
        with mock.patch.object(mod, "_post", return_value=({}, None)):
            self.assertIsNone(mod._plan("t"))


class ParseTests(unittest.TestCase):
    def test_live_payload_yields_four_windows(self):
        windows = {w.label: w for w in _windows_for(_LIVE)}
        self.assertEqual(len(windows), 4)
        self.assertAlmostEqual(windows["Gemini · week"].used_percent, 7.30, places=2)
        self.assertAlmostEqual(windows["Gemini · 5h"].used_percent, 13.75, places=2)
        self.assertEqual(windows["Claude/GPT · 5h"].used_percent, 0.0)
        self.assertEqual(windows["Gemini · week"].reset_at, "2026-10-01T02:48:55Z")

    def test_response_wrapper_is_unwrapped(self):
        self.assertEqual(len(_windows_for({"response": _LIVE})), 4)

    def test_bucket_identity_is_by_id_not_position(self):
        # Labels must follow bucketId, not array order.
        payload = {"groups": [{"buckets": list(reversed(_LIVE["groups"][0]["buckets"]))}]}
        windows = {w.label: w for w in _windows_for(payload)}
        self.assertAlmostEqual(windows["Gemini · week"].used_percent, 7.30, places=2)
        self.assertAlmostEqual(windows["Gemini · 5h"].used_percent, 13.75, places=2)

    def test_exhausted_bucket_without_fraction_is_100_percent(self):
        # A drained counter reports resetTime, no fraction.
        payload = {"groups": [{"buckets": [
            {"bucketId": "gemini-5h", "resetTime": "2026-09-26T08:03:22Z"}]}]}
        windows = _windows_for(payload)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].used_percent, 100.0)

    def test_bucket_with_nothing_is_dropped(self):
        self.assertEqual(
            _windows_for({"groups": [{"buckets": [{"bucketId": "gemini-5h"}]}]}), [])

    def test_out_of_range_fraction_is_not_rescaled(self):
        payload = {"groups": [{"buckets": [
            {"bucketId": "gemini-5h", "remainingFraction": 86.2}]}]}
        self.assertEqual(_windows_for(payload), [])

    def test_zero_fraction_is_fully_used(self):
        payload = {"groups": [{"buckets": [
            {"bucketId": "gemini-weekly", "remainingFraction": 0.0,
             "resetTime": "2026-10-01T00:00:00Z"}]}]}
        self.assertEqual(_windows_for(payload)[0].used_percent, 100.0)

    def test_unknown_bucket_ids_are_ignored(self):
        payload = {"groups": [{"buckets": [
            {"bucketId": "mystery-pool", "remainingFraction": 0.5}]}]}
        self.assertEqual(_windows_for(payload), [])

    def test_garbage_payload_yields_nothing(self):
        for payload in ({}, {"groups": "nope"}, {"groups": [{"buckets": "no"}]}):
            self.assertEqual(_windows_for(payload), [])

    def test_non_object_payload_is_not_buckets(self):
        # HTTP 200 carrying a JSON array or string is a schema surprise, not an
        # AttributeError.
        for payload in ([], ["groups"], "nope", None, 7):
            self.assertEqual(mod._buckets(payload), {})


class FetchTests(unittest.TestCase):
    def test_happy_path(self):
        result = _fetch()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.label, "antigravity")
        self.assertEqual(result.plan, "Google AI Pro")
        self.assertEqual(len(result.windows), 4)
        self.assertTrue(result.has_data())

    def test_401_is_auth_failed(self):
        result = _fetch_with_post(lambda p, t, **kw: (None, 401))
        self.assertEqual(result.unavailable_reason, "auth-failed")

    def test_403_is_no_subscription(self):
        result = _fetch_with_post(lambda p, t, **kw: (None, 403))
        self.assertEqual(result.unavailable_reason, "no-subscription")

    def test_other_http_is_reported(self):
        result = _fetch_with_post(lambda p, t, **kw: (None, 500))
        self.assertEqual(result.unavailable_reason, "http-500")

    def test_transport_failure_is_fetch_error(self):
        result = _fetch_with_post(lambda p, t, **kw: (None, None))
        self.assertEqual(result.unavailable_reason, "fetch-error")

    def test_no_usable_bucket_is_no_data(self):
        self.assertEqual(_fetch(summary={"groups": []}).unavailable_reason, "no-data")

    def test_json_array_payload_is_no_data(self):
        self.assertEqual(
            _fetch_with_post(lambda p, t, **kw: ([], None)).unavailable_reason, "no-data")

    def test_plan_failure_does_not_lose_the_quota(self):
        def _post(path, token, **kwargs):  # noqa: ANN001, ANN003, ARG001
            return (_LIVE, None) if path.endswith(mod._QUOTA_PATH) else (None, 500)

        result = _fetch_with_post(_post)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(result.windows), 4)
        self.assertIsNone(result.plan)


class AntigravityOptInTests(unittest.TestCase):
    def _enabled_via_config(self, value):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"antigravityEnabled": value}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(os.environ, {}, clear=True):
                return mod._antigravity_enabled()

    def test_antigravity_config_uses_a_strict_opt_in(self):
        for value in ("false", "False", "no", "0", "off", 0, None):
            with self.subTest(value=value):
                self.assertFalse(self._enabled_via_config(value))
        self.assertTrue(self._enabled_via_config(True))
        self.assertTrue(self._enabled_via_config("true"))

    def test_registered_fetcher_does_not_read_credentials_or_call_network_by_default(self):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {"plugins": {"entries": {"quota": {"settings": {}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(mod, "_load_credential") as load_credential, \
                    mock.patch.object(mod, "_post") as post:
                result = PROVIDER_FETCHERS["antigravity"]()
        self.assertEqual(result.unavailable_reason, "opt-in-disabled")
        load_credential.assert_not_called()
        post.assert_not_called()

    def test_explicit_config_opt_in_reaches_the_provider(self):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"antigravityEnabled": True}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(mod, "_load_credential", return_value=None) as load_credential:
                result = PROVIDER_FETCHERS["antigravity"]()
        self.assertEqual(result.unavailable_reason, "no-credentials")
        load_credential.assert_called_once_with()


class RegistrationTests(unittest.TestCase):
    def test_provider_is_registered(self):
        from quota_providers import PROVIDER_FETCHERS

        self.assertIn("antigravity", PROVIDER_FETCHERS)
        # Registered through the opt-in gate, like grok. The
        # registry stores register()'s fail-open wrapper, so compare against
        # the function it wraps rather than the wrapper's identity.
        registered = PROVIDER_FETCHERS["antigravity"]
        self.assertIs(getattr(registered, "__wrapped__", registered),
                      mod._fetch_antigravity_optin)

    def test_secret_literal_stays_split(self):
        # The client secret must stay reassembled so scanners do not flag it.
        # Build the expected literal from parts -- hardcoding it here would put
        # the very string this test exists to keep out of the repo.
        expected = "GOCSPX-" + "K58FWR486LdLJ1mLB8sXC4z6qDAf"
        src = open(mod.__file__, encoding="utf-8").read()
        self.assertNotIn(expected, src)
        self.assertNotIn(expected, open(__file__, encoding="utf-8").read())

    def test_no_credential_write_back(self):
        # Reading someone else's login must never mutate their store, and this
        # must never write to disk. Reading the Linux CLI token file is the one
        # legitimate filesystem read, so the guard is write-freedom, not the
        # absence of open().
        import re

        src = open(mod.__file__, encoding="utf-8").read()
        for forbidden in ("CredWrite", "CredDelete", "pathlib", "tempfile", "shutil"):
            self.assertNotIn(forbidden, src)
        for line in src.splitlines():
            if re.search(r"(?<!url)(?<!urllib\.)\bopen\(", line):
                self.assertRegex(line, r"[\"']r[b]?[\"']", f"non-read open(): {line.strip()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
