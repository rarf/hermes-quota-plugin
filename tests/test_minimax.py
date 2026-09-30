"""Offline tests for the MiniMax Token Plan fetcher (stdlib only, no network)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quota_providers import PROVIDER_FETCHERS, minimax  # noqa: E402

# ``quota_cache`` reads its path through hermes_constants and uses relative
# imports, so it is loaded as a package member with that boundary stubbed —
# importing it must never touch the user's real HERMES_HOME. The stub honours
# HERMES_HOME so a later test that points it at a temp home still works.
try:  # pragma: no cover - Hermes is absent in a standalone checkout
    import hermes_constants  # noqa: F401
except ModuleNotFoundError:
    _stub = types.ModuleType("hermes_constants")
    _stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME") or tempfile.gettempdir())
    sys.modules.setdefault("hermes_constants", _stub)
_pkg = types.ModuleType("minimax_test_pkg")
_pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("minimax_test_pkg", _pkg)
from minimax_test_pkg.quota_cache import REFRESH_BUDGET_S  # noqa: E402

_CANONICAL = "https://www.minimax.io"
_MIRROR = "https://api.minimax.io"

_HAPPY = {
    "model_remains": [
        {
            "model_name": "general",
            "current_interval_total_count": 10000,
            "current_interval_usage_count": 400,
            "current_interval_remaining_percent": 96,
            "current_weekly_total_count": 100000,
            "current_weekly_usage_count": 1000,
            "current_weekly_remaining_percent": 99,
            "end_time": 1773000000,
            "weekly_end_time": 1773600000,
        },
        {"model_name": "video", "current_interval_remaining_percent": 100, "current_weekly_remaining_percent": 100},
    ],
    "base_resp": {"status_code": 0, "status_msg": "success"},
}


class _Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self, size=-1):
        if size is None or size < 0:
            return self.body
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _routes(canonical=_HAPPY):
    """A `_urlopen` stand-in that answers per host, raising what it is given."""
    def _urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        value = canonical if request.full_url.startswith(_CANONICAL) else _HAPPY
        if isinstance(value, Exception):
            raise value
        return _Resp(value)

    return _urlopen


def _called_urls(opener_mock):
    return [call.args[0].full_url if call.args else call.kwargs["url"] for call in opener_mock.call_args_list]


class MiniMaxParseTests(unittest.TestCase):
    def test_registered(self):
        # register() stores a fail-open wrapper (see registry.register), so
        # compare against the function it wraps rather than by identity.
        registered = PROVIDER_FETCHERS["minimax"]
        self.assertIs(getattr(registered, "__wrapped__", registered),
                      minimax.fetch_minimax_quota)

    def test_budget_stays_inside_the_sweep(self):
        # A provider slower than the sweep is recorded as `timeout` and loses
        # its previous value, so the two hosts share one bounded deadline.
        self.assertLess(minimax._DEADLINE_S, REFRESH_BUDGET_S)
        self.assertLessEqual(minimax._REQUEST_TIMEOUT_S, minimax._DEADLINE_S)

    def parse(self, payload, *, video=False):
        env = {"HERMES_QUOTA_MINIMAX_VIDEO_ENABLED": "1" if video else "0"}
        with mock.patch.dict(os.environ, env):
            return minimax.parse_quota_payload(payload)

    def test_happy_payload_reports_used_percent_from_remaining(self):
        result = self.parse(_HAPPY)
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNone(result.plan)
        self.assertEqual([(w.label, w.used_percent) for w in result.windows],
                         [("Session", 4.0), ("Weekly", 1.0)])
        self.assertTrue(all(w.reset_at.endswith("+00:00") for w in result.windows))
        self.assertEqual([w.label for w in result.windows if w.label.startswith("Video")], [])

    def test_remaining_percent_falls_back_to_usage_counts(self):
        payload = {"model_remains": [{
            "model_name": "general",
            "current_interval_usage_count": 40,
            "current_interval_total_count": 100,
            "current_weekly_usage_count": 1,
            "current_weekly_total_count": 4,
        }], "base_resp": {"status_code": 0}}
        self.assertEqual([w.used_percent for w in self.parse(payload).windows], [40.0, 25.0])

    def test_no_denominator_is_not_a_fabricated_ratio(self):
        payload = {"model_remains": [{"model_name": "general", "current_interval_usage_count": 5}],
                   "base_resp": {"status_code": 0}}
        result = self.parse(payload)
        self.assertEqual(result.unavailable_reason, "no-data")
        self.assertEqual(result.windows, [])

    def test_pay_as_you_go_key_is_no_subscription(self):
        payload = {"base_resp": {"status_code": 1004, "status_msg": "plan not found for this key"}}
        self.assertEqual(self.parse(payload).unavailable_reason, "no-subscription")

    def test_non_plan_error_is_no_data(self):
        payload = {"base_resp": {"status_code": 500, "status_msg": "internal error"}}
        self.assertEqual(self.parse(payload).unavailable_reason, "no-data")

    def test_garbage_payloads_never_raise(self):
        for payload in ([], {}, {"model_remains": None}, {"model_remains": "x"},
                        {"model_remains": []}, {"model_remains": ["nope", 3]}, "text", 7):
            with self.subTest(payload=payload):
                self.assertEqual(self.parse(payload).unavailable_reason, "no-data")

    def test_video_bucket_is_opt_in(self):
        # Was `self.parse(...).windows and [...]`, which returns the falsy
        # left side when there are no windows and parses the payload twice.
        default_windows = self.parse(_HAPPY).windows
        self.assertEqual([w.label for w in default_windows], ["Session", "Weekly"])
        opted_in = self.parse(_HAPPY, video=True)
        self.assertEqual([w.label for w in opted_in.windows],
                         ["Session", "Weekly", "Video Session", "Video Weekly"])

    def test_unknown_model_family_keeps_a_distinguishable_label(self):
        payload = {"model_remains": [{
            "model_name": "Speech",
            "current_interval_remaining_percent": 50,
            "current_weekly_remaining_percent": 50,
        }], "base_resp": {"status_code": 0}}
        self.assertEqual([w.label for w in self.parse(payload).windows],
                         ["Speech · 5h", "Speech · Weekly"])

    def test_duplicate_labels_render_once(self):
        entry = {"model_name": "general", "current_interval_remaining_percent": 50,
                 "current_weekly_remaining_percent": 50}
        payload = {"model_remains": [entry, dict(entry, current_interval_remaining_percent=10)],
                   "base_resp": {"status_code": 0}}
        self.assertEqual([(w.label, w.used_percent) for w in self.parse(payload).windows],
                         [("Session", 50.0), ("Weekly", 50.0)])

    def test_reset_accepts_seconds_and_milliseconds(self):
        self.assertEqual(minimax._parse_reset(1773000000), "2026-03-08T20:00:00+00:00")
        self.assertEqual(minimax._parse_reset(1773000000000), "2026-03-08T20:00:00+00:00")
        for bad in (None, "later", 0, -5, float("nan")):
            self.assertIsNone(minimax._parse_reset(bad))


class MiniMaxFetchTests(unittest.TestCase):
    def fetch(self, opener, *, bearer="SYNTHETIC_KEY"):
        with mock.patch.object(minimax, "_urlopen", opener), \
             mock.patch.object(minimax, "resolve_bearer", return_value=bearer):
            return minimax.fetch_minimax_quota()

    def test_no_credentials(self):
        self.assertEqual(self.fetch(_routes(), bearer=None).unavailable_reason, "no-credentials")

    def test_happy_path_sends_the_bearer_to_the_canonical_host(self):
        opener = mock.Mock(side_effect=_routes())
        result = self.fetch(opener)
        self.assertIsNone(result.unavailable_reason)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, f"{_CANONICAL}{minimax._QUOTA_PATH}")
        self.assertEqual(request.get_header("Authorization"), "Bearer SYNTHETIC_KEY")
        self.assertNotIn("SYNTHETIC_KEY", repr(result))

    def test_404_on_the_canonical_host_falls_back_to_the_mirror(self):
        opener = mock.Mock(side_effect=_routes(urllib.error.HTTPError(_CANONICAL, 404, "Not Found", {}, None)))
        result = self.fetch(opener)
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([c.args[0].full_url.split("/v1/")[0] for c in opener.call_args_list],
                         [_CANONICAL, _MIRROR])

    def test_both_hosts_missing_is_host_not_found(self):
        opener = mock.Mock(side_effect=urllib.error.HTTPError(_CANONICAL, 404, "Not Found", {}, None))
        self.assertEqual(self.fetch(opener).unavailable_reason, "host-not-found")

    def test_auth_and_http_errors_are_reported_and_not_retried(self):
        for code, reason in ((401, "auth-failed"), (403, "auth-failed"), (429, "http-429")):
            with self.subTest(code=code):
                opener = mock.Mock(side_effect=urllib.error.HTTPError(_CANONICAL, code, "x", {}, None))
                self.assertEqual(self.fetch(opener).unavailable_reason, reason)
                self.assertEqual(opener.call_count, 1)

    def test_malformed_and_oversized_bodies_are_safe(self):
        for body, reason in ((b"not json", "bad-json"), (b"x" * (minimax._MAX_BYTES + 1), "response-too-large")):
            with self.subTest(reason=reason):
                opener = mock.Mock(side_effect=lambda request, timeout=None, body=body: _Resp(body))
                self.assertEqual(self.fetch(opener).unavailable_reason, reason)

    def test_timeout_is_reported(self):
        opener = mock.Mock(side_effect=TimeoutError("took too long"))
        self.assertEqual(self.fetch(opener).unavailable_reason, "timeout")

    def test_unexpected_errors_never_escape(self):
        opener = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(self.fetch(opener).unavailable_reason, "fetch-error")

    def test_expired_deadline_does_not_start_a_second_request(self):
        opener = mock.Mock(side_effect=_routes())
        with mock.patch.object(minimax, "_urlopen", opener), \
             mock.patch.object(minimax, "resolve_bearer", return_value="K"), \
             mock.patch.object(minimax.time, "monotonic", side_effect=[0.0, minimax._DEADLINE_S + 1]):
            result = minimax.fetch_minimax_quota()
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertEqual(opener.call_count, 0)

    def test_redirects_are_never_followed(self):
        from quota_providers.base import NoRedirectHandler
        handler = NoRedirectHandler()
        request = minimax.urllib.request.Request("https://www.minimax.io/v1/token_plan/remains")
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example"))


class MiniMaxCredentialTests(unittest.TestCase):
    def _hermes(self, resolved):
        """Fake hermes_cli.auth whose registry holds the three MiniMax providers."""
        calls = []

        def resolver(provider_id, config):  # noqa: ANN001, ARG001
            calls.append(provider_id)
            return resolved.get(provider_id), "pool"

        registry = {name: type("C", (), {"auth_type": "api_key"})()
                    for name in ("minimax", "minimax-cn", "minimax-oauth")}
        auth = type("M", (), {"PROVIDER_REGISTRY": registry,
                              "_resolve_api_key_provider_secret": staticmethod(resolver)})
        package = type("M", (), {"auth": auth})
        return calls, mock.patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.auth": auth}), auth

    def test_core_resolver_wins_then_env(self):
        calls, modules, _auth = self._hermes({"minimax-cn": "POOL_KEY"})
        with modules, mock.patch.dict(os.environ, {"MINIMAX_API_KEY": "ENV_KEY"}):
            self.assertEqual(minimax.resolve_bearer(), "POOL_KEY")
        self.assertEqual(calls, ["minimax", "minimax-cn"])

    def test_oauth_provider_token_is_accepted(self):
        calls, modules, _auth = self._hermes({"minimax-oauth": "OAUTH_TOKEN"})
        with modules, mock.patch.dict(os.environ, {"MINIMAX_API_KEY": "ENV_KEY"}):
            self.assertEqual(minimax.resolve_bearer(), "OAUTH_TOKEN")
        self.assertEqual(calls, ["minimax", "minimax-cn", "minimax-oauth"])

    def test_env_fallback_when_core_is_absent(self):
        with mock.patch.dict(sys.modules, {"hermes_cli": None}), \
             mock.patch.dict(os.environ, {"MINIMAX_CN_API_KEY": "  ENV_KEY  "}):
            self.assertEqual(minimax.resolve_bearer(), "ENV_KEY")

    def test_nothing_configured_resolves_to_none(self):
        _calls, modules, _auth = self._hermes({})
        with modules, mock.patch.dict(os.environ, {}, clear=False):
            for name in minimax._ENV_KEYS:
                os.environ.pop(name, None)
            self.assertIsNone(minimax.resolve_bearer())


if __name__ == "__main__":
    unittest.main()
