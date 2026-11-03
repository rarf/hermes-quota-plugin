"""Offline unit tests for the CLI-backed Antigravity quota fetcher.

Run from the repo root:  python tests/test_antigravity.py
Every subprocess boundary is faked except in ``RunnerTests`` (which runs this
Python interpreter with synthetic programs) and ``LiveAgyTests`` (opt-in via
``HERMES_QUOTA_LIVE_AGY=1``).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quota_providers import PROVIDER_FETCHERS, antigravity as mod  # noqa: E402


def _payload(groups, *, status: Any = "SUCCESS", name="usage", response=""):
    return {
        "status": status,
        "response": response,
        "command": {"name": name, "data": {"groups": groups}},
    }


def _live_payload():
    """Captured shape from agy 1.4.12 (quota values are synthetic)."""
    return _payload([
        {
            "name": "Gemini Models",
            "buckets": [
                {"id": "gemini-weekly", "name": "Weekly Limit Remaining",
                 "window": "weekly", "remaining_fraction": 0.75,
                 "reset_time": "2026-11-10T12:37:08Z"},
                {"id": "gemini-5h", "name": "Five Hour Limit Remaining",
                 "window": "5h", "remaining_fraction": 0.5,
                 "reset_time": "2026-11-03T17:37:08Z"},
            ],
        },
        {
            "name": "Claude and GPT models",
            "buckets": [
                {"id": "3p-weekly", "name": "Weekly Limit Remaining",
                 "window": "weekly", "remaining_fraction": 1,
                 "reset_time": "2026-11-10T12:37:08Z"},
                {"id": "3p-5h", "name": "Five Hour Limit Remaining",
                 "window": "5h", "remaining_fraction": 0.9,
                 "reset_time": "2026-11-03T17:37:08Z"},
            ],
        },
        {
            "name": "Gemini 4 Argon",
            "buckets": [
                {"id": "gemini-argon-wtu-weekly",
                 "name": "Gemini 4 Argon Weekly Limit", "window": "weekly",
                 "remaining_fraction": 0.9990862607955933,
                 "reset_time": "2026-11-09T11:02:28Z"},
            ],
        },
    ], response="Gemini Models\tWeekly Limit Remaining\t75%\t2026-11-10T12:37:08Z\n")


def _run_fake(*, version=b"1.4.12\n", usage=None, version_rc=0, usage_rc=0,
              version_timed_out=False, usage_timed_out=False,
              version_overflow=False, usage_overflow=False,
              version_launch_error=False, usage_launch_error=False):
    """Fake ``_run_cli`` that records (args, timeout) and returns canned data."""
    calls = []
    usage = json.dumps(_live_payload()).encode("utf-8") if usage is None else usage

    def _run(path, args, *, timeout, max_bytes=mod._MAX_OUTPUT_BYTES,
             log_name=None):
        calls.append((path, tuple(args), timeout, max_bytes, log_name))
        if tuple(args) == ("--version",):
            return mod._CliRun(
                version, version_rc, version_timed_out, version_overflow,
                version_launch_error,
            )
        return mod._CliRun(
            usage, usage_rc, usage_timed_out, usage_overflow, usage_launch_error,
        )

    return _run, calls


def _fetch(*, path="/fake/agy", run=None, env=None, **kwargs):
    fake, calls = _run_fake(**kwargs)
    if run is not None:
        fake = run
    # Run with a clean ambient environment so a developer/CI proxy cannot turn
    # unrelated fetch tests into `agy-unsafe-proxy`.
    with mock.patch.dict(os.environ, {} if env is None else env, clear=True), \
            mock.patch.object(
                mod, "_agy_lookup", return_value=(path, None)), \
            mock.patch.object(mod, "_run_cli", side_effect=fake):
        result = mod.fetch_antigravity_quota()
    return result, calls


class VersionGateTests(unittest.TestCase):
    def test_missing_agy_is_reported_without_running_anything(self):
        with mock.patch.object(mod.shutil, "which", return_value=None), \
                mock.patch.object(mod, "_run_cli") as run:
            result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "agy-missing")
        self.assertIn("1.1.11", " ".join(result.details))
        run.assert_not_called()

    def test_found_but_unsafe_agy_is_not_reported_as_missing(self):
        # A present-but-hijackable executable is a permission problem, not an
        # installation problem, and must not get the "install agy" advice.
        with tempfile.TemporaryDirectory() as td:
            exe = _make_executable(td, 0o775)
            with mock.patch.object(mod.shutil, "which", return_value=str(exe)), \
                    mock.patch.object(mod, "_run_cli") as run:
                result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "agy-unsafe-path")
        self.assertNotIn("Install", " ".join(result.details))
        run.assert_not_called()

    def test_old_version_never_runs_usage(self):
        result, calls = _fetch(version=b"1.1.10\n")
        self.assertEqual(result.unavailable_reason, "agy-too-old")
        self.assertEqual([c[1] for c in calls], [("--version",)])
        self.assertIn("1.1.10", " ".join(result.details))

    def test_minimum_version_runs_usage(self):
        result, calls = _fetch(version=b"1.1.11\n")
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([c[1] for c in calls],
                         [("--version",), mod._USAGE_ARGS])

    def test_newer_version_runs_usage(self):
        result, _calls = _fetch(version=b"1.4.12\n")
        self.assertIsNone(result.unavailable_reason)

    def test_version_must_be_a_bare_semver(self):
        for text in (b"agy 1.4.12\n", b"1.4\n", b"1.4.12 extra\n", b"\xff"):
            with self.subTest(text=text):
                result, calls = _fetch(version=text)
                self.assertEqual(result.unavailable_reason, "agy-version-unknown")
                self.assertEqual(len(calls), 1)

    def test_prereleases_are_not_a_proven_minimum(self):
        # 1.1.11-rc does not prove the release has the structured payload.
        for text in (b"1.1.11-rc.1\n", b"1.1.11-rc\n", b"1.4.12-beta.2\n",
                     b"v1.1.11-alpha\n"):
            with self.subTest(text=text):
                result, calls = _fetch(version=text)
                self.assertEqual(result.unavailable_reason, "agy-version-unknown")
                self.assertEqual(len(calls), 1)

    def test_build_metadata_does_not_change_the_release(self):
        result, calls = _fetch(version=b"1.1.11+build.7\n")
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(calls), 2)

    def test_version_timeout_is_timeout_and_never_usage(self):
        result, calls = _fetch(version_timed_out=True)
        self.assertEqual(result.unavailable_reason, "timeout")
        self.assertEqual(len(calls), 1)

    def test_version_failure_is_an_error_and_never_usage(self):
        result, calls = _fetch(version_rc=1)
        self.assertEqual(result.unavailable_reason, "agy-error")
        self.assertEqual(len(calls), 1)

    def test_version_launch_failure_is_an_execution_error(self):
        # The path was accepted, so a spawn failure is not "not installed".
        result, calls = _fetch(version_launch_error=True)
        self.assertEqual(result.unavailable_reason, "agy-error")
        self.assertEqual(len(calls), 1)

    def test_usage_arguments_are_exact_and_read_only(self):
        _result, calls = _fetch()
        self.assertEqual(calls[0][1], ("--version",))
        self.assertEqual(calls[1][1], ("-p", "/usage", "--output-format", "json"))
        joined = " ".join(calls[1][1])
        self.assertNotIn("dangerously", joined)
        self.assertNotIn("permission", joined)
        self.assertNotIn("shell", joined)
        self.assertNotIn("--model", joined)
        self.assertNotIn("--agent", joined)

    def test_each_probe_confines_its_log_to_a_private_file(self):
        _result, calls = _fetch()
        self.assertEqual([call[4] for call in calls],
                         ["agy-version.log", "agy-usage.log"])

    def test_command_injects_the_log_file_before_the_probe_arguments(self):
        command = mod._command(
            "/fake/agy", mod._USAGE_ARGS, "/tmp/private", "agy-usage.log")
        self.assertEqual(command, [
            "/fake/agy", "--log-file", "/tmp/private/agy-usage.log",
            "-p", "/usage", "--output-format", "json",
        ])
        self.assertEqual(mod._command("/fake/agy", ("--version",), "/w", None),
                         ["/fake/agy", "--version"])


class ParseTests(unittest.TestCase):
    def test_live_shape_yields_five_windows_including_argon(self):
        result, _calls = _fetch()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.label for w in result.windows], [
            "Gemini · Weekly", "Gemini · 5h",
            "Claude/GPT · Weekly", "Claude/GPT · 5h",
            "Gemini 4 Argon · Weekly",
        ])
        self.assertAlmostEqual(result.windows[0].used_percent, 25.0, places=2)
        self.assertAlmostEqual(result.windows[1].used_percent, 50.0, places=2)
        self.assertEqual(result.windows[2].used_percent, 0.0)
        self.assertAlmostEqual(result.windows[3].used_percent, 10.0, places=2)
        self.assertAlmostEqual(result.windows[4].used_percent, 0.09, places=2)
        self.assertEqual(result.windows[4].reset_at, "2026-11-09T11:02:28Z")

    def test_weekly_windows_use_capitalized_weekly_label(self):
        # Cards read "Weekly", matching the other providers' weekly windows;
        # the CLI's lowercase "week" wording must not leak into display labels,
        # and the 5h labels stay untouched.
        result, _calls = _fetch()
        labels = [w.label for w in result.windows]
        self.assertEqual(
            [label for label in labels if label.endswith("· Weekly")],
            ["Gemini · Weekly", "Claude/GPT · Weekly",
             "Gemini 4 Argon · Weekly"],
        )
        self.assertFalse([label for label in labels if label.endswith("· week")])
        self.assertIn("Gemini · 5h", labels)
        self.assertIn("Claude/GPT · 5h", labels)

    def test_plan_is_never_invented(self):
        # The CLI payload has no plan field; response text must not be mined.
        result, _calls = _fetch()
        self.assertIsNone(result.plan)

    def test_missing_fraction_with_reset_is_unknown_not_exhausted(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "reset_time": "2026-11-03T17:37:08Z"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(result.windows), 1)
        self.assertIsNone(result.windows[0].used_percent)
        self.assertEqual(result.windows[0].reset_at, "2026-11-03T17:37:08Z")

    def test_bucket_with_no_fraction_and_no_reset_is_dropped(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_out_of_range_fraction_is_dropped_not_rescaled(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "remaining_fraction": 86.2,
             "reset_time": "2026-11-03T17:37:08Z"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_zero_fraction_is_fully_used(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-weekly", "remaining_fraction": 0.0,
             "reset_time": "2026-11-10T12:37:08Z"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.windows[0].used_percent, 100.0)

    def test_unknown_bucket_ids_are_ignored(self):
        payload = _payload([{"name": "Mystery", "buckets": [
            {"id": "mystery-pool", "remaining_fraction": 0.5}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_absent_argon_is_not_duplicated_from_the_gemini_bucket(self):
        # Argon is conditional.  A four-bucket response must stay four
        # windows; the provider must not derive a fifth from Gemini's pool.
        payload = _live_payload()
        payload["command"]["data"]["groups"] = [
            group for group in payload["command"]["data"]["groups"]
            if group["name"] != "Gemini 4 Argon"
        ]
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.label for w in result.windows], [
            "Gemini · Weekly", "Gemini · 5h",
            "Claude/GPT · Weekly", "Claude/GPT · 5h",
        ])
        self.assertNotIn("Gemini 4 Argon · Weekly",
                         [w.label for w in result.windows])

    def test_returned_argon_is_shown_once_as_the_fifth_window(self):
        result, _calls = _fetch()
        argon = [w for w in result.windows
                 if w.label == "Gemini 4 Argon · Weekly"]
        self.assertEqual(len(argon), 1)
        self.assertAlmostEqual(argon[0].used_percent or 0.0, 0.09, places=2)
        self.assertEqual(result.windows[-1].label, "Gemini 4 Argon · Weekly")
        self.assertEqual(len(result.windows), 5)

    def test_returned_argon_without_fraction_is_unknown_not_exhausted(self):
        payload = _payload([{"name": "Gemini 4 Argon", "buckets": [
            {"id": "gemini-argon-wtu-weekly",
             "reset_time": "2026-11-09T11:02:28Z"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(result.windows), 1)
        self.assertEqual(result.windows[0].label, "Gemini 4 Argon · Weekly")
        self.assertIsNone(result.windows[0].used_percent)

    def test_duplicate_bucket_id_uses_the_first(self):
        payload = _payload([
            {"name": "Gemini Models", "buckets": [
                {"id": "gemini-5h", "remaining_fraction": 0.25}]},
            {"name": "Duplicate", "buckets": [
                {"id": "gemini-5h", "remaining_fraction": 0.75}]},
        ])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.windows[0].used_percent, 75.0)

    def test_camel_case_fields_are_tolerated(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"bucketId": "gemini-5h", "remainingFraction": 0.4,
             "resetTime": "2026-11-03T17:37:08Z"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows[0].used_percent, 60.0)

    def test_malformed_reset_time_is_omitted_not_displayed(self):
        for reset in ("not-a-date", "2026-02-30T00:00:00Z",
                      "2026-11-03 17:37:08Z", "2026-11-03T17:37:08+24:00"):
            with self.subTest(reset=reset):
                payload = _payload([{"name": "Gemini Models", "buckets": [
                    {"id": "gemini-5h", "remaining_fraction": 0.5,
                     "reset_time": reset}]}])
                result, _calls = _fetch(usage=json.dumps(payload).encode())
                self.assertIsNone(result.unavailable_reason)
                self.assertEqual(result.windows[0].used_percent, 50.0)
                self.assertIsNone(result.windows[0].reset_at)

    def test_bucket_with_only_a_malformed_reset_is_dropped(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "reset_time": "soon"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_reset_time_is_length_capped(self):
        with mock.patch.object(mod, "_MAX_RESET_CHARS", 10):
            self.assertIsNone(mod._reset_time("2026-11-03T17:37:08Z"))

    def test_reset_time_with_a_numeric_offset_is_preserved(self):
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "remaining_fraction": 0.5,
             "reset_time": "2026-11-03T17:37:08.123+09:00"}]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows[0].reset_at,
                         "2026-11-03T17:37:08.123+09:00")

    def test_response_text_alone_is_not_scraped(self):
        payload = _payload([], response=(
            "Gemini Models\tWeekly Limit Remaining\t100%\t2026-11-10T12:37:08Z\n"))
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_non_object_groups_are_ignored(self):
        payload = _payload([None, "nope", {"buckets": "no"}, {"buckets": [None]}])
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "no-data")


class ErrorTests(unittest.TestCase):
    def test_invalid_json_is_reported(self):
        result, _calls = _fetch(usage=b"{not json")
        self.assertEqual(result.unavailable_reason, "agy-invalid-json")

    def test_non_object_json_is_bad_payload(self):
        result, _calls = _fetch(usage=b"[]")
        self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_non_success_status_is_refused(self):
        result, _calls = _fetch(usage=json.dumps(_payload([], status="ERROR")).encode())
        self.assertEqual(result.unavailable_reason, "agy-refused")

    def test_ok_status_from_older_release_is_accepted(self):
        # Older agy releases spell the successful command outcome "OK".
        payload = _payload([{"name": "Gemini Models", "buckets": [
            {"id": "gemini-5h", "remaining_fraction": 0.5,
             "reset_time": "2026-11-03T17:37:08Z"}]}], status="OK")
        result, calls = _fetch(usage=json.dumps(payload).encode())
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(result.windows[0].used_percent, 50.0)
        self.assertEqual(len(calls), 2)

    def test_success_statuses_are_matched_exactly(self):
        # No case folding, padding stripping or prefix inference: only the
        # two observed spellings confirm success.
        for status in ("ok", "Ok", "OK ", " OK", "SUCCESS ", " SUCCESS",
                       "success", "OKAY", "SUCCESSFUL"):
            with self.subTest(status=status):
                payload = _payload([], status=status)
                result, _calls = _fetch(usage=json.dumps(payload).encode())
                self.assertEqual(result.unavailable_reason, "agy-refused")

    def test_missing_status_with_valid_groups_is_not_inferred_as_ok(self):
        payload = _live_payload()
        del payload["status"]
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_missing_status_is_a_malformed_envelope(self):
        # No status is not the CLI refusing; it is not a usable envelope.
        payload = {"command": {"name": "usage", "data": {"groups": []}}}
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_non_string_status_is_a_malformed_envelope(self):
        for status in (None, 1, True, [], {}):
            with self.subTest(status=status):
                payload = _payload([], status=status)
                result, _calls = _fetch(usage=json.dumps(payload).encode())
                self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_wrong_command_is_bad_payload(self):
        result, _calls = _fetch(usage=json.dumps(_payload([], name="model")).encode())
        self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_missing_groups_is_bad_payload(self):
        payload = {"status": "SUCCESS", "command": {"name": "usage", "data": {}}}
        result, _calls = _fetch(usage=json.dumps(payload).encode())
        self.assertEqual(result.unavailable_reason, "agy-bad-json")

    def test_empty_groups_is_no_data(self):
        result, _calls = _fetch(usage=json.dumps(_payload([])).encode())
        self.assertEqual(result.unavailable_reason, "no-data")

    def test_usage_nonzero_exit_is_error(self):
        result, calls = _fetch(usage_rc=1)
        self.assertEqual(result.unavailable_reason, "agy-error")
        self.assertEqual(len(calls), 2)

    def test_usage_timeout_is_timeout(self):
        result, _calls = _fetch(usage_timed_out=True)
        self.assertEqual(result.unavailable_reason, "timeout")

    def test_oversized_usage_output_is_reported(self):
        result, _calls = _fetch(usage_overflow=True)
        self.assertEqual(result.unavailable_reason, "agy-output-too-large")

    def test_usage_launch_failure_is_error(self):
        result, _calls = _fetch(usage_launch_error=True)
        self.assertEqual(result.unavailable_reason, "agy-error")


class ProxyGateTests(unittest.TestCase):
    """A configured proxy must never be silently dropped into a direct run."""

    def test_unusable_proxy_fails_closed_before_running_agy(self):
        with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": "http://user:password@proxy.example:3128",
        }, clear=True), \
                mock.patch.object(mod, "_agy_lookup", return_value=("/fake/agy", None)), \
                mock.patch.object(mod, "_run_cli") as run:
            result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "agy-unsafe-proxy")
        details = " ".join(result.details)
        self.assertIn("HTTPS_PROXY", details)
        self.assertNotIn("password", details)
        run.assert_not_called()

    def test_validated_proxy_does_not_block(self):
        result, calls = _fetch(env={"HTTPS_PROXY": "http://proxy.example:3128"})
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(calls), 2)

    def test_proxy_urls_the_cli_would_ignore_fail_closed(self):
        cases = {
            "HTTPS_PROXY": "http://proxy.example:8080\t",
            "HTTP_PROXY": "http://proxy%zz:8080",
        }
        for key, value in cases.items():
            with self.subTest(key=key), \
                    mock.patch.dict(os.environ, {key: value}, clear=True), \
                    mock.patch.object(
                        mod, "_agy_lookup", return_value=("/fake/agy", None)), \
                    mock.patch.object(mod, "_run_cli") as run:
                result = mod.fetch_antigravity_quota()
            self.assertEqual(result.unavailable_reason, "agy-unsafe-proxy")
            self.assertIn(key, " ".join(result.details))
            run.assert_not_called()

    def test_malformed_no_proxy_with_a_proxy_fails_closed(self):
        # Once a proxy is configured, dropping a bad NO_PROXY changes routing.
        with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": "http://proxy.example:3128",
                "NO_PROXY": "localhost\n127.0.0.1",
        }, clear=True), \
                mock.patch.object(
                    mod, "_agy_lookup", return_value=("/fake/agy", None)), \
                mock.patch.object(mod, "_run_cli") as run:
            result = mod.fetch_antigravity_quota()
        self.assertEqual(result.unavailable_reason, "agy-unsafe-proxy")
        self.assertIn("NO_PROXY", " ".join(result.details))
        run.assert_not_called()


class EnvironmentTests(unittest.TestCase):
    def test_environment_is_an_allow_list_with_auto_update_disabled(self):
        hostile = {
            "HOME": "/home/test",
            "PATH": "/usr/bin:/bin",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
            "GEMINI_API_KEY": "secret-gemini",
            "GOOGLE_APPLICATION_CREDENTIALS": "/secret/adc.json",
            "AGY_ADC_AUTH": "1",
            "JETSKI_OAUTH_TOKEN": "secret-jetski",
            "AWS_SECRET_ACCESS_KEY": "secret-aws",
            "SOME_RANDOM_SECRET": "secret-random",
        }
        with mock.patch.dict(os.environ, hostile, clear=True):
            env = mod._cli_env()
        self.assertEqual(env["HOME"], "/home/test")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertIn("DBUS_SESSION_BUS_ADDRESS", env)
        self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        for key in ("GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
                    "AGY_ADC_AUTH", "JETSKI_OAUTH_TOKEN",
                    "AWS_SECRET_ACCESS_KEY", "SOME_RANDOM_SECRET"):
            self.assertNotIn(key, env)

    def test_standard_proxy_variables_are_forwarded(self):
        proxies = {
            "HTTP_PROXY": "http://proxy.example:3128",
            "HTTPS_PROXY": "https://proxy.example:8443/",
            "ALL_PROXY": "socks5://proxy.example:1080",
            "NO_PROXY": "localhost,127.0.0.1,.example",
            "http_proxy": "http://lower.example:8080",
        }
        with mock.patch.dict(os.environ, proxies, clear=True):
            env = mod._cli_env()
        for key, value in proxies.items():
            self.assertEqual(env.get(key), value)

    def test_common_no_proxy_spacing_is_accepted_and_trimmed(self):
        cases = (
            ("localhost, 127.0.0.1", "localhost,127.0.0.1"),
            ("localhost,\t127.0.0.1", "localhost,127.0.0.1"),
            ("  localhost  ,\t127.0.0.1\t", "localhost,127.0.0.1"),
            ("localhost,  [::1], .example", "localhost,[::1],.example"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(mod._proxy_value("NO_PROXY", value), expected)
                self.assertIsNone(mod._proxy_problem({"NO_PROXY": value}))
        with mock.patch.dict(os.environ,
                             {"NO_PROXY": "localhost, 127.0.0.1"}, clear=True):
            self.assertEqual(
                mod._cli_env()["NO_PROXY"], "localhost,127.0.0.1")

    def test_common_no_proxy_spacing_does_not_block_a_fetch(self):
        result, calls = _fetch(env={"NO_PROXY": "localhost, 127.0.0.1"})
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(calls), 2)

    def test_valid_ipv6_proxy_and_no_proxy_entries_are_forwarded(self):
        proxies = {
            "HTTPS_PROXY": "http://[::1]:8080",
            "ALL_PROXY": "socks5://[2001:db8::1]:1080",
            "NO_PROXY": "[::1],[2001:db8::1]:443,::1,10.0.0.0/8,localhost,.example",
        }
        with mock.patch.dict(os.environ, proxies, clear=True):
            env = mod._cli_env()
        for key, value in proxies.items():
            self.assertEqual(env.get(key), value)
        self.assertIsNone(mod._proxy_problem(proxies))

    def test_proxy_urls_with_whitespace_or_control_are_not_forwarded(self):
        values = (
            "http://proxy.example:8080 ",
            " http://proxy.example:8080",
            "http://pro xy.example:8080",
            "http://proxy.example:8080\t",
            "http://proxy.example:8080\r\n",
            "http://proxy.example:8080\x00",
            "http://proxy.example:8080\x1b",
            "http://proxy.example:8080\x7f",
            "http://proxy.example:8080\u00a0",
            "http://proxy.example:8080\u200b",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("HTTPS_PROXY", value))
                self.assertEqual(
                    mod._proxy_problem({"HTTPS_PROXY": value}), "HTTPS_PROXY")

    def test_no_proxy_control_characters_inside_entries_are_rejected(self):
        values = (
            "localhost\n",
            "localhost\n,127.0.0.1",
            "local\thost",
            "localhost,\x7f",
            "localhost,\x1b[31m",
            "localhost\u00a0",
            "localhost,\u200b",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("NO_PROXY", value))
                # With no proxy configured the bad list is withheld, not fatal:
                # it cannot turn a request direct when there is no proxy.
                self.assertIsNone(mod._proxy_problem({"NO_PROXY": value}))
                with mock.patch.dict(os.environ,
                                     {"NO_PROXY": value}, clear=True):
                    self.assertNotIn("NO_PROXY", mod._cli_env())
                # With a proxy configured, dropping it would change routing.
                self.assertEqual(
                    mod._proxy_problem({
                        "HTTPS_PROXY": "http://proxy.example:3128",
                        "NO_PROXY": value,
                    }),
                    "NO_PROXY",
                )

    def test_malformed_no_proxy_without_a_proxy_does_not_disable_the_fetch(self):
        result, calls = _fetch(env={"NO_PROXY": "localhost\n127.0.0.1"})
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(len(calls), 2)

    def test_invalid_percent_escapes_are_not_forwarded(self):
        values = (
            "http://proxy%zz:8080",
            "http://proxy%2:8080",
            "http://proxy%:8080",
            "http://proxy%0g:8080",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("HTTPS_PROXY", value))
                self.assertEqual(
                    mod._proxy_problem({"HTTPS_PROXY": value}), "HTTPS_PROXY")

    def test_percent_escaped_dns_host_is_rejected(self):
        # Only an IPv6 zone may use a valid escape (%25); a DNS/IPv4 host may not.
        values = (
            "http://proxy%41:8080",
            "http://proxy%2eexample:8080",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("HTTPS_PROXY", value))
                self.assertEqual(
                    mod._proxy_problem({"HTTPS_PROXY": value}), "HTTPS_PROXY")

    def test_malformed_ipv6_proxy_is_rejected(self):
        values = (
            "http://::1:8080",
            "http://[::1",
            "http://[not:ipv6]:8080",
            "http://[::1]suffix",
            "http://[::1]:",
            "http://[::1]:99999",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("HTTPS_PROXY", value))
                self.assertEqual(
                    mod._proxy_problem({"HTTPS_PROXY": value}), "HTTPS_PROXY")

    def test_malformed_no_proxy_entries_are_rejected(self):
        values = (
            "bad entry",
            "foo%zz",
            "host:0",
            "host:99999",
            "10.0.0.0/99",
            "a..b",
            "-bad.example",
            "*.example.com:bad",
            "[not:ipv6]:80",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(mod._proxy_value("NO_PROXY", value))
                self.assertIsNone(mod._proxy_problem({"NO_PROXY": value}))
                self.assertEqual(
                    mod._proxy_problem({
                        "HTTPS_PROXY": "http://proxy.example:3128",
                        "NO_PROXY": value,
                    }),
                    "NO_PROXY",
                )

    def test_proxy_urls_with_credentials_or_bad_shapes_are_not_forwarded(self):
        hostile = {
            "HTTP_PROXY": "http://user:password@proxy.example:3128",
            "HTTPS_PROXY": "ftp://proxy.example:21",
            "ALL_PROXY": "http://proxy.example:99999",
            "http_proxy": "http://proxy.example/path",
            "https_proxy": "http://proxy.example:8080\nGEMINI_API_KEY=oops",
            "no_proxy": "localhost\nHTTP_PROXY=http://evil",
        }
        with mock.patch.dict(os.environ, hostile, clear=True):
            env = mod._cli_env()
        self.assertFalse(set(hostile) & set(env))

    def test_present_but_unusable_proxy_is_identified(self):
        cases = {
            "HTTP_PROXY": "http://user:password@proxy.example:3128",
            "HTTPS_PROXY": "ftp://proxy.example:21",
            "ALL_PROXY": "http://proxy.example:99999",
            "http_proxy": "http://proxy.example/path",
            "https_proxy": "http://proxy.example:8080\nGEMINI_API_KEY=oops",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.assertEqual(mod._proxy_problem({key: value}), key)

    def test_absent_empty_or_valid_proxy_is_not_a_problem(self):
        self.assertIsNone(mod._proxy_problem({}))
        self.assertIsNone(mod._proxy_problem({"HTTPS_PROXY": ""}))
        self.assertIsNone(mod._proxy_problem({
            "HTTPS_PROXY": "http://proxy.example:3128",
            "NO_PROXY": "localhost,127.0.0.1,.example",
        }))

    def test_proxy_support_does_not_admit_other_secret_variables(self):
        environ = {
            "HTTPS_PROXY": "http://proxy.example:3128",
            "GEMINI_API_KEY": "secret-gemini",
            "SOME_RANDOM_SECRET": "secret-random",
        }
        with mock.patch.dict(os.environ, environ, clear=True):
            env = mod._cli_env()
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:3128")
        self.assertNotIn("GEMINI_API_KEY", env)
        self.assertNotIn("SOME_RANDOM_SECRET", env)

    def test_source_has_no_legacy_direct_credential_or_http_helpers(self):
        """Coarse source guard, not a scanner verdict or proof of absence.

        A hit means an old direct-read/HTTP marker survived the CLI rewrite;
        a miss alone is not evidence that the module is safe.  The real
        boundary is that no credential file, keyring or HTTP client is used.
        """
        source = Path(mod.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "_load_credential", "_access_token", "_refresh_exchange", "_post(",
            "retrieveUserQuotaSummary", "loadCodeAssist", "Authorization",
            "CredRead", "find-generic-password", "refresh_token",
            "client_secret", "_PUBLIC_CLIENT_ID", "urlopen",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("agy -p /usage --output-format json", source)
        self.assertIn("shell=False", source)
        self.assertNotIn("--dangerously-skip-permissions", source)

    def test_no_secret_reaches_argv(self):
        _result, calls = _fetch()
        rendered = " ".join(" ".join(call[1]) for call in calls)
        for needle in ("secret", "token", "GEMINI_API_KEY", "password"):
            self.assertNotIn(needle.lower(), rendered.lower())


def _make_executable(directory, mode=0o755, name="agy"):
    path = Path(directory) / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    path.chmod(mode)
    return path


class PathValidationTests(unittest.TestCase):
    """The executable lookup must fail closed on hijackable paths."""

    def test_relative_path_hits_are_rejected(self):
        for found in ("agy", "./agy", "bin/agy", ""):
            with self.subTest(found=found):
                with mock.patch.object(mod.shutil, "which",
                                       return_value=found or None):
                    self.assertIsNone(mod._agy_path())

    def test_absolute_safe_executable_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_executable(td)
            with mock.patch.object(mod.shutil, "which", return_value=str(exe)):
                self.assertEqual(mod._agy_path(), str(exe.resolve()))

    def test_group_or_world_writable_and_set_id_executable_is_rejected(self):
        for mode in (0o775, 0o757, 0o4755, 0o2755):
            with self.subTest(mode=oct(mode)), \
                    tempfile.TemporaryDirectory() as td:
                exe = _make_executable(td, mode)
                with mock.patch.object(mod.shutil, "which",
                                       return_value=str(exe)):
                    self.assertIsNone(mod._agy_path())

    def test_non_executable_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            exe = _make_executable(td, 0o644)
            with mock.patch.object(mod.shutil, "which", return_value=str(exe)):
                self.assertIsNone(mod._agy_path())

    def test_directory_named_agy_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "agy"
            directory.mkdir()
            with mock.patch.object(mod.shutil, "which",
                                   return_value=str(directory)):
                self.assertIsNone(mod._agy_path())

    def test_executable_in_a_writable_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            writable = Path(td) / "bin"
            writable.mkdir()
            writable.chmod(0o777)
            exe = _make_executable(writable)
            with mock.patch.object(mod.shutil, "which", return_value=str(exe)):
                self.assertIsNone(mod._agy_path())

    def test_symlink_to_a_safe_executable_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            target = _make_executable(td, name="agy-real")
            link = Path(td) / "agy"
            link.symlink_to(target)
            with mock.patch.object(mod.shutil, "which", return_value=str(link)):
                self.assertEqual(mod._agy_path(), str(target.resolve()))


_GRANDCHILD_SCRIPT = (
    "import subprocess, sys, time\n"
    "pidfile, mode = sys.argv[1], sys.argv[2]\n"
    "grand = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "with open(pidfile, 'w', encoding='ascii') as fh:\n"
    "    fh.write(str(grand.pid))\n"
    "if mode == 'hold':\n"
    "    time.sleep(60)\n"
    "else:\n"
    "    print('parent-exit', flush=True)\n"
)

# A descendant that does not hold stdout: normal-exit cleanup must still find
# it in the child's process group.
_DAEMON_CHILD_SCRIPT = (
    "import subprocess, sys, time\n"
    "daemon = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "with open(sys.argv[1], 'w', encoding='ascii') as fh:\n"
    "    fh.write(str(daemon.pid))\n"
    "print('parent-exit', flush=True)\n"
)

# A descendant that starts its own session: no signal to the child's process
# group can reach it, and it still holds the inherited stdout pipe.
_ESCAPED_CHILD_SCRIPT = (
    "import subprocess, sys\n"
    "escaped = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
    "    start_new_session=True)\n"
    "with open(sys.argv[1], 'w', encoding='ascii') as fh:\n"
    "    fh.write(str(escaped.pid))\n"
    "print('parent-exit', flush=True)\n"
)


def _run_with_watchdog(call, seconds):
    """Run ``call`` in a daemon thread so a deadlock fails instead of hanging."""
    box = {}

    def target():
        try:
            box["result"] = call()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return thread, box


def _reader_threads():
    """Live reader threads, so a cancelled reader leak is visible directly."""
    return [t for t in threading.enumerate()
            if t.name == "agy-stdout" and t.is_alive()]


def _read_pid(pidfile, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            text = Path(pidfile).read_text(encoding="ascii").strip()
        except OSError:
            text = ""
        if text:
            try:
                return int(text)
            except ValueError:
                return None
        time.sleep(0.01)
    return None


def _pid_is_dead(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # On Linux a killed but unreaped grandchild is a zombie; treat that as dead.
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        return stat_text[stat_text.rfind(")") + 2] == "Z"
    except (OSError, IndexError):
        return False


def _wait_dead(pid, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _pid_is_dead(pid):
            return True
        time.sleep(0.02)
    return _pid_is_dead(pid)


def _kill_pid(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class RunnerTests(unittest.TestCase):
    """Exercise the bounded subprocess helper with synthetic Python programs."""

    def test_captures_stdout_without_a_shell(self):
        run = mod._run_cli(
            sys.executable, ("-c", "import sys; sys.stdout.write('ok')"),
            timeout=10,
        )
        self.assertFalse(run.timed_out)
        self.assertFalse(run.output_too_large)
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout, b"ok")

    def test_runs_in_an_empty_private_directory(self):
        code = (
            "import os; print(os.getcwd()); "
            "print(oct(os.stat('.').st_mode & 0o777)); print(os.listdir('.'))"
        )
        run = mod._run_cli(sys.executable, ("-c", code), timeout=10)
        cwd, mode, entries = run.stdout.decode("utf-8").splitlines()
        self.assertEqual(entries, "[]")
        self.assertFalse(os.path.exists(cwd),
                         "the neutral working directory must be removed")
        self.assertNotEqual(os.path.realpath(cwd),
                            os.path.realpath(os.getcwd()))
        if os.name != "nt":
            self.assertEqual(mode, "0o700")

    @unittest.skipIf(os.name == "nt", "POSIX shebang executable")
    def test_cli_log_file_is_inside_the_workdir_and_removed(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "fake-agy"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, sys\n"
                "path = pathlib.Path(sys.argv[sys.argv.index('--log-file') + 1])\n"
                "path.write_text('vendor log', encoding='ascii')\n"
                "print(path)\n",
                encoding="ascii",
            )
            script.chmod(0o755)
            run = mod._run_cli(
                str(script), ("--version",), timeout=10,
                log_name="agy-version.log",
            )
        logged = Path(run.stdout.decode("utf-8").strip())
        self.assertEqual(logged.name, "agy-version.log")
        self.assertTrue(logged.parent.name.startswith("hermes-quota-agy-"))
        self.assertFalse(logged.exists(),
                         "the per-call log must be removed with the workdir")

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_child_is_a_session_leader(self):
        code = "import os; print(os.getpid()); print(os.getpgrp())"
        run = mod._run_cli(sys.executable, ("-c", code), timeout=10)
        pid, pgid = (int(value) for value in run.stdout.split())
        self.assertEqual(pid, pgid,
                         "child must lead its own group so descendants die")

    def test_zero_timeout_never_spawns(self):
        with mock.patch.object(mod.subprocess, "Popen") as popen:
            run = mod._run_cli("/fake/agy", ("--version",), timeout=0)
        self.assertTrue(run.timed_out)
        popen.assert_not_called()

    def test_output_cap_kills_the_child(self):
        run = mod._run_cli(
            sys.executable,
            ("-c", "import sys; sys.stdout.write('x' * 100000)"),
            timeout=10, max_bytes=128,
        )
        self.assertTrue(run.output_too_large)
        self.assertLessEqual(len(run.stdout), 128)

    def test_unbounded_output_is_capped_and_killed(self):
        """A writer that never stops must hit the byte cap, not the timeout."""
        code = (
            "import sys\n"
            "while True:\n"
            "    sys.stdout.write('x' * 4096)\n"
            "    sys.stdout.flush()\n"
        )
        started = time.monotonic()
        thread, box = _run_with_watchdog(
            lambda: mod._run_cli(
                sys.executable, ("-c", code), timeout=10, max_bytes=1024),
            5.0,
        )
        elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive(), "infinite output hung the reader")
        self.assertIn("result", box, box.get("error"))
        run = box["result"]
        self.assertTrue(run.output_too_large)
        self.assertLessEqual(len(run.stdout), 1024)
        self.assertIsNotNone(run.returncode)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(_reader_threads(), [],
                         "the cap must not leave a reader thread behind")

    def test_timeout_kills_the_child(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "child.pid")
            code = ("import os, sys, time\n"
                    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                    "time.sleep(30)\n")
            run = mod._run_cli(
                sys.executable, ("-c", code, pidfile), timeout=0.3)
            pid = _read_pid(pidfile)
        self.assertTrue(run.timed_out)
        self.assertIsNotNone(pid)
        self.assertTrue(_wait_dead(pid),
                        "a timed-out child must be killed and reaped")
        self.assertEqual(_reader_threads(), [],
                         "a timeout must not leave a reader thread behind")

    def test_timeout_has_one_wall_clock_bound(self):
        started = time.monotonic()
        run = mod._run_cli(
            sys.executable, ("-c", "import time; time.sleep(30)"),
            timeout=0.2,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(run.timed_out)
        self.assertLess(elapsed, 3.0,
                        "a timeout must not add a second full wait")

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_exited_child_with_grandchild_on_stdout_is_bounded_and_cleaned(self):
        """The child exits, but its child still holds the inherited pipe."""
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "grand.pid")
            started = time.monotonic()
            thread, box = _run_with_watchdog(
                lambda: mod._run_cli(
                    sys.executable,
                    ("-c", _GRANDCHILD_SCRIPT, pidfile, "exit"),
                    timeout=10,
                ),
                5.0,
            )
            elapsed = time.monotonic() - started
            pid = _read_pid(pidfile)
            try:
                if thread.is_alive() and pid is not None:
                    _kill_pid(pid)
                    thread.join(3.0)
                self.assertFalse(thread.is_alive(),
                                 "waiter deadlocked closing stdout")
                self.assertIn("result", box, box.get("error"))
                run = box["result"]
                self.assertFalse(run.timed_out)
                self.assertEqual(run.returncode, 0)
                self.assertLess(elapsed, 3.0,
                                "an exited child must not hold the call open")
                self.assertIsNotNone(pid)
                self.assertTrue(
                    _wait_dead(pid),
                    "the grandchild holding stdout must be killed",
                )
            finally:
                if pid is not None and not _pid_is_dead(pid):
                    _kill_pid(pid)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_escaped_descendant_does_not_leak_the_reader_or_an_fd(self):
        """A descendant in its own session still holds stdout after the child exits.

        No group signal can reach it, so the old reader blocked forever and
        leaked one thread plus one fd per refresh.  The bounded cancellable
        reader must give up and close its end without waiting for that process.
        """
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "escaped.pid")
            fd_dir = "/proc/self/fd"
            before_fds = (len(os.listdir(fd_dir))
                          if os.path.isdir(fd_dir) else None)
            started = time.monotonic()
            thread, box = _run_with_watchdog(
                lambda: mod._run_cli(
                    sys.executable,
                    ("-c", _ESCAPED_CHILD_SCRIPT, pidfile),
                    timeout=5,
                ),
                5.0,
            )
            elapsed = time.monotonic() - started
            pid = _read_pid(pidfile)
            try:
                if thread.is_alive() and pid is not None:
                    _kill_pid(pid)
                    thread.join(3.0)
                self.assertFalse(thread.is_alive(),
                                 "waiter blocked on an escaped descendant")
                self.assertIn("result", box, box.get("error"))
                run = box["result"]
                self.assertFalse(run.timed_out)
                self.assertEqual(run.returncode, 0)
                self.assertLess(elapsed, 3.0,
                                "the grace period must bound an escaped holder")
                self.assertIsNotNone(pid)
                self.assertFalse(_pid_is_dead(pid),
                                 "test setup: the descendant must escape")
                end = time.monotonic() + 1.0
                while _reader_threads() and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertEqual(_reader_threads(), [],
                                 "the cancelled reader thread must exit")
                if before_fds is not None:
                    self.assertLessEqual(
                        len(os.listdir(fd_dir)), before_fds,
                        "the reader must close the stdout descriptor",
                    )
            finally:
                if pid is not None and not _pid_is_dead(pid):
                    _kill_pid(pid)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_exited_child_cleans_a_descendant_that_does_not_hold_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "daemon.pid")
            started = time.monotonic()
            thread, box = _run_with_watchdog(
                lambda: mod._run_cli(
                    sys.executable,
                    ("-c", _DAEMON_CHILD_SCRIPT, pidfile),
                    timeout=10,
                ),
                5.0,
            )
            elapsed = time.monotonic() - started
            pid = _read_pid(pidfile)
            try:
                if thread.is_alive() and pid is not None:
                    _kill_pid(pid)
                    thread.join(3.0)
                self.assertFalse(thread.is_alive())
                self.assertIn("result", box, box.get("error"))
                run = box["result"]
                self.assertFalse(run.timed_out)
                self.assertEqual(run.returncode, 0)
                self.assertLess(elapsed, 3.0)
                self.assertIsNotNone(pid)
                self.assertTrue(
                    _wait_dead(pid),
                    "a normal exit must not leave a descendant running",
                )
            finally:
                if pid is not None and not _pid_is_dead(pid):
                    _kill_pid(pid)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_timeout_kills_grandchild_holding_stdout(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "grand.pid")
            started = time.monotonic()
            thread, box = _run_with_watchdog(
                lambda: mod._run_cli(
                    sys.executable,
                    ("-c", _GRANDCHILD_SCRIPT, pidfile, "hold"),
                    timeout=1.0,
                ),
                5.0,
            )
            elapsed = time.monotonic() - started
            pid = _read_pid(pidfile)
            try:
                if thread.is_alive() and pid is not None:
                    _kill_pid(pid)
                    thread.join(3.0)
                self.assertFalse(thread.is_alive(),
                                 "waiter deadlocked closing stdout")
                self.assertIn("result", box, box.get("error"))
                run = box["result"]
                self.assertTrue(run.timed_out)
                self.assertLess(elapsed, 3.0)
                self.assertIsNotNone(pid)
                self.assertTrue(
                    _wait_dead(pid),
                    "the timeout must clean up the whole process group",
                )
            finally:
                if pid is not None and not _pid_is_dead(pid):
                    _kill_pid(pid)


    def test_secret_environment_is_not_inherited_by_the_child(self):
        code = (
            "import os; "
            "print(os.environ.get('GEMINI_API_KEY', 'absent')); "
            "print(os.environ.get('AGY_CLI_DISABLE_AUTO_UPDATE', 'missing'))"
        )
        with mock.patch.dict(os.environ,
                             {"GEMINI_API_KEY": "secret-value", "PATH": os.environ.get("PATH", "")},
                             clear=False):
            run = mod._run_cli(sys.executable, ("-c", code), timeout=10)
        self.assertEqual(run.stdout, b"absent\ntrue\n")

    def test_validated_proxy_reaches_the_child_but_other_secrets_do_not(self):
        code = (
            "import os; "
            "print(os.environ.get('HTTPS_PROXY', 'absent')); "
            "print(os.environ.get('GEMINI_API_KEY', 'absent'))"
        )
        with mock.patch.dict(os.environ, {
                "HTTPS_PROXY": "http://proxy.example:3128",
                "GEMINI_API_KEY": "secret-value",
                "PATH": os.environ.get("PATH", ""),
        }, clear=False):
            run = mod._run_cli(sys.executable, ("-c", code), timeout=10)
        self.assertEqual(
            run.stdout, b"http://proxy.example:3128\nabsent\n")

    def test_stdin_is_closed_for_noninteractive_runs(self):
        code = "import sys; print(repr(sys.stdin.read()))"
        run = mod._run_cli(sys.executable, ("-c", code), timeout=10)
        self.assertEqual(run.stdout, b"''\n")

    def test_a_cancelled_reader_returns_without_touching_the_pipe(self):
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb", buffering=0)
        cancel = threading.Event()
        cancel.set()
        try:
            self.assertIsNone(mod._read_chunk(stream, cancel))
        finally:
            stream.close()
            os.close(write_fd)


class WindowsProcessTests(unittest.TestCase):
    """Mock-only Windows process handling; the Windows runtime is unverified."""

    def test_spawn_uses_a_new_process_group_and_no_console_window(self):
        expected = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        with mock.patch.object(mod.os, "name", "nt"):
            kwargs = mod._spawn_kwargs()
        self.assertEqual(kwargs, {"creationflags": expected})
        self.assertTrue(kwargs["creationflags"] & 0x08000000)

    def test_taskkill_tree_runs_before_the_parent_kill(self):
        events = []
        proc = mock.Mock(pid=4242)
        proc.kill.side_effect = lambda: events.append(("kill", None, None))

        def fake_run(argv, **kwargs):
            events.append(("taskkill", tuple(argv), kwargs))
            return mock.Mock(returncode=0)

        with mock.patch.object(mod.os, "name", "nt"), \
                mock.patch.object(mod.subprocess, "run", side_effect=fake_run):
            mod._kill_tree(proc)

        self.assertEqual([event[0] for event in events],
                         ["taskkill", "kill"],
                         "taskkill /T must enumerate children before exit")
        argv = events[0][1]
        self.assertTrue(str(argv[0]).endswith("taskkill.exe"))
        self.assertEqual(list(argv[1:]), ["/PID", "4242", "/T", "/F"])
        self.assertEqual(events[0][2]["creationflags"], 0x08000000)
        proc.kill.assert_called_once_with()

    def test_taskkill_failure_still_falls_back_to_killing_the_parent(self):
        proc = mock.Mock(pid=4242)
        with mock.patch.object(mod.os, "name", "nt"), \
                mock.patch.object(mod.subprocess, "run", side_effect=OSError):
            mod._kill_tree(proc)
        proc.kill.assert_called_once_with()


class OptInTests(unittest.TestCase):
    def _enabled_via_config(self, value):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"antigravityEnabled": value}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}):
            with mock.patch.dict(os.environ, {}, clear=True):
                return mod._antigravity_enabled()

    def test_config_uses_a_strict_opt_in(self):
        for value in ("false", "False", "no", "0", "off", 0, None):
            with self.subTest(value=value):
                self.assertFalse(self._enabled_via_config(value))
        for value in (True, "true", "yes", "on", 1):
            with self.subTest(value=value):
                self.assertTrue(self._enabled_via_config(value))

    def test_disabled_provider_never_runs_agy(self):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(mod, "_agy_lookup") as path, \
                mock.patch.object(mod, "_run_cli") as run:
            result = PROVIDER_FETCHERS["antigravity"]()
        self.assertEqual(result.unavailable_reason, "opt-in-disabled")
        path.assert_not_called()
        run.assert_not_called()

    def test_explicit_config_opt_in_reaches_the_cli(self):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"antigravityEnabled": True}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(mod, "_agy_lookup",
                                  return_value=("/fake/agy", None)), \
                mock.patch.object(mod, "_run_cli", side_effect=_run_fake()[0]) as run:
            result = PROVIDER_FETCHERS["antigravity"]()
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual(run.call_count, 2)

    def test_env_off_overrides_config_on(self):
        config = types.ModuleType("hermes_cli.config")
        config.load_config_readonly = lambda: {
            "plugins": {"entries": {"quota": {"settings": {"antigravityEnabled": True}}}}}
        cli = types.ModuleType("hermes_cli")
        cli.config = config
        with mock.patch.dict(sys.modules, {"hermes_cli": cli, "hermes_cli.config": config}), \
                mock.patch.dict(os.environ, {"HERMES_QUOTA_ANTIGRAVITY_ENABLED": "off"}):
            self.assertFalse(mod._antigravity_enabled())


class RegistrationTests(unittest.TestCase):
    def test_provider_is_registered_through_the_opt_in_gate(self):
        self.assertIn("antigravity", PROVIDER_FETCHERS)
        registered = PROVIDER_FETCHERS["antigravity"]
        self.assertIs(getattr(registered, "__wrapped__", registered),
                      mod._fetch_antigravity_optin)


@unittest.skipUnless(os.environ.get("HERMES_QUOTA_LIVE_AGY") == "1",
                     "set HERMES_QUOTA_LIVE_AGY=1 for the read-only live agy check")
class LiveAgyTests(unittest.TestCase):
    """One explicit, read-only fetch against the installed agy.

    The user authorized this on their own machine.  It prints labels, remaining
    percentages and reset times only; no raw CLI output, environment or
    credential material is displayed.  It does not change config or restart
    anything.
    """

    def test_live_usage(self):
        self.assertIsNotNone(shutil.which("agy"))
        result = mod.fetch_antigravity_quota()
        print("live antigravity:", result.unavailable_reason, "plan=", result.plan)
        for window in result.windows:
            print(" ", window.label, window.used_percent, window.reset_at)
        self.assertIsNone(result.unavailable_reason)
        self.assertIsNone(result.plan)
        labels = [window.label for window in result.windows]
        for expected in ("Gemini · Weekly", "Gemini · 5h",
                         "Claude/GPT · Weekly", "Claude/GPT · 5h"):
            self.assertIn(expected, labels)
        # Argon is conditional: the live account may or may not return it.
        # When present it must be the separate bucket, never fabricated.
        if "Gemini 4 Argon · Weekly" in labels:
            argon = [w for w in result.windows
                     if w.label == "Gemini 4 Argon · Weekly"]
            self.assertEqual(len(argon), 1)
            self.assertIsNotNone(argon[0].used_percent)
        for window in result.windows:
            self.assertIsNotNone(window.used_percent)
            self.assertGreaterEqual(window.used_percent or 0.0, 0.0)
            self.assertLessEqual(window.used_percent or 0.0, 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
