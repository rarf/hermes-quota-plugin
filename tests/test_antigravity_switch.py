"""Offline tests for the single-key Antigravity switch.

`plugins.entries.quota.settings.antigravityEnabled` is the one authoritative
setting (default false). The Desktop switch and `hermes quota switch` read and
write that key; there is no second activation setting. These tests pin the
policy, the write path, the env override, and a real background sweep with every
agy boundary faked.

Run from the repo root:  python tests/test_antigravity_switch.py
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quota_providers import PROVIDER_FETCHERS, antigravity, antigravity_switch as gate  # noqa: E402
from quota_providers.base import QuotaResult, QuotaWindow  # noqa: E402


def _config_modules(settings: dict) -> dict:
    """Fake hermes_cli.config reading the mutable test settings dict.

    Both the switch helper and ``antigravity._antigravity_enabled`` import this
    module lazily, so one fake drives the UI state and the real fetcher gate.
    """
    config = types.ModuleType("hermes_cli.config")
    config.load_config_readonly = lambda: {
        "plugins": {"entries": {"quota": {"settings": dict(settings)}}}
    }
    cli = types.ModuleType("hermes_cli")
    cli.config = config
    return {"hermes_cli": cli, "hermes_cli.config": config}


def _usage_json(fraction: float = 0.9) -> bytes:
    """Minimal accepted ``agy -p /usage --output-format json`` payload."""
    return json.dumps({
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {"groups": [{"name": "Gemini Models", "buckets": [{
                "id": "gemini-5h",
                "remaining_fraction": fraction,
                "reset_time": "2026-11-03T12:00:00Z",
            }]}]},
        },
    }).encode("utf-8")


def _fake_cli(module, *, version: bytes = b"1.4.12\n", usage=None):
    """Fake ``_run_cli`` for ``module``, recording (path, args) per call.

    Answers the version probe first and the usage probe second, exactly like
    the real fetcher. ``module`` is passed in because the sweep tests import a
    private copy of the provider under a different package name.
    """
    payload = _usage_json() if usage is None else usage
    calls: list = []

    def _run(path, args, *, timeout, max_bytes=module._MAX_OUTPUT_BYTES,
             log_name=None):
        calls.append((path, tuple(args)))
        if tuple(args) == ("--version",):
            return module._CliRun(version, 0)
        return module._CliRun(payload, 0)

    return _run, calls


class _Isolated(unittest.TestCase):
    """No ambient HERMES_* env, no real config file and no real Hermes core."""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()
        self.settings: dict = {}
        self._sys = mock.patch.dict(sys.modules, _config_modules(self.settings))
        self._sys.start()

    def tearDown(self) -> None:
        self._sys.stop()
        self._env.stop()


class SingleKeyPolicyTests(_Isolated):
    def test_absent_key_is_off_by_default(self) -> None:
        state = gate.status()
        self.assertFalse(state["enabled"])
        self.assertFalse(state["config_explicit"])
        self.assertEqual(state["config"], None)
        self.assertEqual(state["source"], "default")
        self.assertEqual(state["setting"], "antigravityEnabled")
        self.assertFalse(gate.enabled())

    def test_explicit_config_true_enables_and_false_disables(self) -> None:
        self.settings[gate.SETTING] = True
        self.assertTrue(gate.enabled())
        state = gate.status()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["config_explicit"])
        self.assertEqual(state["config"], True)
        self.assertEqual(state["source"], "config")

        self.settings[gate.SETTING] = False
        self.assertFalse(gate.enabled())
        self.assertFalse(gate.status()["enabled"])

    def test_strict_parser_keeps_string_false_off(self) -> None:
        self.settings[gate.SETTING] = "false"
        self.assertFalse(gate.enabled())
        self.settings[gate.SETTING] = "true"
        self.assertTrue(gate.enabled())

    def test_env_true_overrides_config_false_and_explains_the_conflict(self) -> None:
        os.environ[gate.ENV] = "1"
        self.settings[gate.SETTING] = False
        state = gate.status()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["env_override"])
        self.assertEqual(state["source"], "env")
        self.assertEqual(state["config"], False)
        self.assertIn(gate.ENV, state["warning"])
        self.assertIn("config key says off", state["warning"])

    def test_env_false_overrides_config_true(self) -> None:
        os.environ[gate.ENV] = "0"
        self.settings[gate.SETTING] = True
        state = gate.status()
        self.assertFalse(state["enabled"])
        self.assertTrue(state["env_override"])
        self.assertIn("config key says on", state["warning"])

    def test_invalid_env_falls_through_to_the_config_key(self) -> None:
        os.environ[gate.ENV] = "maybe"
        self.settings[gate.SETTING] = True
        state = gate.status()
        self.assertTrue(state["enabled"])
        self.assertFalse(state["env_override"])
        self.assertEqual(state["source"], "config")

    def test_loader_exception_falls_closed_without_escaping(self) -> None:
        config = sys.modules["hermes_cli.config"]
        with mock.patch.object(config, "load_config_readonly", side_effect=OSError("gone")):
            self.assertFalse(gate.enabled())
            state = gate.status()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["source"], "default")


class SwitchWriteTests(_Isolated):
    def _fake_plugins(self, writer) -> dict:
        modules = _config_modules(self.settings)
        plugins = types.ModuleType("hermes_cli.plugins")
        plugins.save_plugin_setting = writer
        modules["hermes_cli"].plugins = plugins
        modules["hermes_cli.plugins"] = plugins
        return modules

    def test_switch_writes_the_one_existing_config_key(self) -> None:
        calls = []

        def writer(plugin_id, segments, value):
            calls.append((plugin_id, segments, value))
            self.settings[gate.SETTING] = value

        with mock.patch.dict(sys.modules, self._fake_plugins(writer)):
            payload = gate.set_switch("antigravity", True)
        self.assertEqual(calls, [("quota", ("antigravityEnabled",), True)])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["setting"], "antigravityEnabled")

    def test_switch_off_writes_false(self) -> None:
        calls = []

        def writer(plugin_id, segments, value):
            calls.append((plugin_id, segments, value))
            self.settings[gate.SETTING] = value

        with mock.patch.dict(sys.modules, self._fake_plugins(writer)):
            payload = gate.set_switch("antigravity", False)
        self.assertEqual(calls, [("quota", ("antigravityEnabled",), False)])
        self.assertFalse(payload["enabled"])

    def test_switch_refuses_non_bool_values_before_any_write(self) -> None:
        """``bool("false")`` is True, so only a real bool may reach the writer."""
        calls = []

        def writer(*args):
            calls.append(args)

        with mock.patch.dict(sys.modules, self._fake_plugins(writer)):
            for value in ("false", "true", "0", "on", 0, 1, 0.0, None, [], object()):
                with self.subTest(value=value):
                    with self.assertRaises(gate.SwitchError) as raised:
                        gate.set_switch("antigravity", value)  # type: ignore[arg-type]
                    self.assertIn("real bool", str(raised.exception))
        self.assertEqual(calls, [])

    def test_switch_command_aliases_still_encode_real_booleans(self) -> None:
        """CLI action words are not passed through to ``set_switch`` as strings."""
        for action in ("on", "true", "1", "enable", "enabled", " ON "):
            with self.subTest(action=action):
                with mock.patch.object(
                        gate, "set_switch", return_value={"enabled": True}) as setter:
                    payload = gate.switch_command("antigravity", action)
                self.assertEqual(payload, {"ok": True, "enabled": True})
                setter.assert_called_once_with("antigravity", True)
        for action in ("off", "false", "0", "disable", "disabled", " OFF "):
            with self.subTest(action=action):
                with mock.patch.object(
                        gate, "set_switch", return_value={"enabled": False}) as setter:
                    payload = gate.switch_command("antigravity", action)
                self.assertEqual(payload, {"ok": True, "enabled": False})
                setter.assert_called_once_with("antigravity", False)

    def test_env_override_keeps_precedence_after_a_write(self) -> None:
        os.environ[gate.ENV] = "0"

        def writer(_plugin_id, _segments, value):
            self.settings[gate.SETTING] = value

        with mock.patch.dict(sys.modules, self._fake_plugins(writer)):
            payload = gate.set_switch("antigravity", True)
        # The user's config choice is saved for later...
        self.assertEqual(self.settings[gate.SETTING], True)
        # ...but the environment remains authoritative and says so.
        self.assertFalse(payload["enabled"])
        self.assertTrue(payload["env_override"])
        self.assertIsNotNone(payload["warning"])

    def test_managed_write_failure_is_a_stable_error(self) -> None:
        def writer(*_args, **_kwargs):
            raise PermissionError("the Antigravity switch is administrator-managed")

        with mock.patch.dict(sys.modules, self._fake_plugins(writer)):
            with self.assertRaises(gate.SwitchError) as raised:
                gate.set_switch("antigravity", True)
        self.assertIn("administrator-managed", str(raised.exception))

    def test_unknown_provider_is_refused_before_any_write(self) -> None:
        with mock.patch.dict(sys.modules, self._fake_plugins(lambda *a, **k: None)):
            with self.assertRaises(gate.SwitchError):
                gate.set_switch("openrouter", True)
            with self.assertRaises(gate.SwitchError):
                gate.switch_command("openrouter", "on")

    def test_switch_command_shapes(self) -> None:
        status = gate.switch_command("antigravity", "status")
        self.assertTrue(status["ok"])
        self.assertFalse(status["enabled"])
        with mock.patch.object(gate, "set_switch", return_value={"enabled": True}) as setter:
            payload = gate.switch_command("antigravity", "on")
        self.assertEqual(payload, {"ok": True, "enabled": True})
        setter.assert_called_once_with("antigravity", True)
        with mock.patch.object(gate, "set_switch", return_value={"enabled": False}) as setter:
            payload = gate.switch_command("antigravity", "off")
        self.assertEqual(payload, {"ok": True, "enabled": False})
        setter.assert_called_once_with("antigravity", False)


class BackendGateTests(_Isolated):
    """The same key must gate the registered fetcher, not just the UI."""

    @staticmethod
    def _disabled_call() -> tuple[QuotaResult, Any, Any]:
        """Run the registered fetcher with the real launcher and discovery trapped.

        ``_run_cli`` is *not* faked here: if the gate regressed, the real
        launcher would reach the patched ``Popen`` and the assertion would fail.
        """
        with mock.patch.object(antigravity, "_agy_lookup") as path, \
                mock.patch.object(antigravity.subprocess, "Popen") as popen:
            result = cast(QuotaResult, PROVIDER_FETCHERS["antigravity"]())
        return result, path, popen

    def test_default_off_never_probes_the_cli_or_starts_a_process(self) -> None:
        result, path, popen = self._disabled_call()
        self.assertEqual(result.unavailable_reason, "opt-in-disabled")
        self.assertEqual(result.windows, [])
        # Not even ``shutil.which("agy")`` runs. The CLI subprocess is the only
        # outbound path left in the provider, so a never-started Popen is the
        # whole network proof as well.
        path.assert_not_called()
        popen.assert_not_called()

    def test_config_true_activates_the_real_cli_path(self) -> None:
        self.settings[gate.SETTING] = True
        run, calls = _fake_cli(antigravity)
        with mock.patch.object(antigravity, "_agy_lookup",
                               return_value=("/fake/agy", None)) as path, \
                mock.patch.object(antigravity, "_run_cli", side_effect=run):
            result = cast(QuotaResult, PROVIDER_FETCHERS["antigravity"]())
        path.assert_called_once_with()
        # Version first, then the quota command -- both through the resolved agy.
        self.assertEqual(calls, [
            ("/fake/agy", ("--version",)),
            ("/fake/agy", antigravity._USAGE_ARGS),
        ])
        self.assertIsNone(result.unavailable_reason)
        self.assertEqual([w.label for w in result.windows], ["Gemini · 5h"])
        self.assertEqual(result.windows[0].used_percent, 10.0)

    def test_config_true_returns_real_windows(self) -> None:
        self.settings[gate.SETTING] = True
        expected = QuotaResult(
            label="antigravity",
            windows=[QuotaWindow(label="Gemini · 5h", used_percent=10.0)],
        )
        with mock.patch.object(antigravity, "fetch_antigravity_quota",
                               return_value=expected) as fetch:
            result = PROVIDER_FETCHERS["antigravity"]()
        self.assertIs(result, expected)
        fetch.assert_called_once_with()

    def test_status_matches_the_fetcher_gate(self) -> None:
        """The switch's effective state must equal what the fetcher reads.

        This is the drift tripwire while the two parsers live in separate
        modules: every env/config precedence and boolean-string case must agree
        with ``antigravity._antigravity_enabled``.
        """
        cases = [
            ({}, {}),
            ({gate.SETTING: True}, {}),
            ({gate.SETTING: False}, {}),
            ({gate.SETTING: 1}, {}),
            ({gate.SETTING: 0}, {}),
            ({gate.SETTING: 2}, {}),
            ({gate.SETTING: 0.5}, {}),
            ({gate.SETTING: "yes"}, {}),
            ({gate.SETTING: "0"}, {}),
            ({gate.SETTING: "maybe"}, {}),
            ({gate.SETTING: None}, {}),
            ({gate.SETTING: False}, {gate.ENV: "1"}),
            ({gate.SETTING: True}, {gate.ENV: "0"}),
            ({gate.SETTING: True}, {gate.ENV: "maybe"}),
            ({gate.SETTING: False}, {gate.ENV: " TRUE "}),
            ({gate.SETTING: True}, {gate.ENV: "no"}),
            ({gate.SETTING: True}, {gate.ENV: ""}),
            ({gate.SETTING: True}, {gate.ENV: "   "}),
            ({gate.SETTING: "false"}, {}),
        ]
        for settings, env in cases:
            with self.subTest(settings=settings, env=env):
                self.settings.clear()
                self.settings.update(settings)
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(gate.enabled(), antigravity._antigravity_enabled())

    def test_env_false_blocks_even_when_config_is_true(self) -> None:
        os.environ[gate.ENV] = "0"
        self.settings[gate.SETTING] = True
        result, path, popen = self._disabled_call()
        self.assertEqual(result.unavailable_reason, "opt-in-disabled")
        path.assert_not_called()
        popen.assert_not_called()


class BackgroundSweepTests(unittest.TestCase):
    """One real refresh sweep through the registry, with a temporary home."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._pkg_name = "quota_switch_sweep_tests"
        cls.settings: dict = {}
        cls._config_modules = _config_modules(cls.settings)
        pkg = types.ModuleType(cls._pkg_name)
        pkg.__path__ = [str(ROOT)]
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(cls._tmp.name)
        with mock.patch.dict(sys.modules, {
                **cls._config_modules,
                cls._pkg_name: pkg,
                "hermes_constants": constants,
        }):
            cls.qc = importlib.import_module(cls._pkg_name + ".quota_cache")
            cls.antigravity = importlib.import_module(
                cls._pkg_name + ".quota_providers.antigravity")
            # mock.patch.dict removes every module added inside the block when
            # it exits, including the package's submodules. Keep the exact
            # module objects so a later refresh resolves the same QuotaResult
            # class instead of a fresh duplicate.
            cls._modules = {
                key: value for key, value in sys.modules.items()
                if key == cls._pkg_name or key.startswith(cls._pkg_name + ".")
            }
        cls._pkg = pkg
        cls._constants = constants

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def setUp(self) -> None:
        self.settings.clear()
        self._env = mock.patch.dict(os.environ, {}, clear=True)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def _sweep(self):
        fetchers = {"antigravity": self.qc.PROVIDER_FETCHERS["antigravity"]}
        run, calls = _fake_cli(self.antigravity)
        with mock.patch.dict(sys.modules, {
                **self._modules,
                **self._config_modules,
                "hermes_constants": self._constants,
        }), mock.patch.object(self.qc, "PROVIDER_FETCHERS", fetchers), \
                mock.patch.object(self.antigravity, "_agy_lookup",
                                  return_value=("/fake/agy", None)) as path, \
                mock.patch.object(self.antigravity, "_run_cli",
                                  side_effect=run), \
                mock.patch.object(self.antigravity.subprocess, "Popen") as popen:
            cache = self.qc.refresh_quota_cache(budget=1.0)
        return cache["providers"]["antigravity"], path, popen, calls

    def test_off_sweep_writes_the_reason_without_any_cli_access(self) -> None:
        record, path, popen, calls = self._sweep()
        self.assertEqual(record["unavailable_reason"], "opt-in-disabled")
        self.assertEqual(record["windows"], [])
        self.assertEqual(calls, [])
        path.assert_not_called()
        popen.assert_not_called()

    def test_on_sweep_reaches_the_cli_and_caches_real_windows(self) -> None:
        self.settings[gate.SETTING] = True
        record, path, popen, calls = self._sweep()
        self.assertIsNone(record["unavailable_reason"])
        self.assertEqual(
            [(w["label"], w["used_percent"]) for w in record["windows"]],
            [("Gemini · 5h", 10.0)],
        )
        self.assertEqual(record["windows"][0]["reset_at"], "2026-11-03T12:00:00Z")
        self.assertEqual(calls, [
            ("/fake/agy", ("--version",)),
            ("/fake/agy", self.antigravity._USAGE_ARGS),
        ])
        path.assert_called_once_with()
        # _run_cli is faked, so a real child would only come from a regression.
        popen.assert_not_called()

    def test_env_false_still_blocks_the_sweep(self) -> None:
        os.environ[gate.ENV] = "0"
        self.settings[gate.SETTING] = True
        record, path, popen, calls = self._sweep()
        self.assertEqual(record["unavailable_reason"], "opt-in-disabled")
        self.assertEqual(calls, [])
        path.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
