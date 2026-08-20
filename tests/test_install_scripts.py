from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_WINDOWS_GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/usr/bin/bash.exe"
BASH = Path(
    os.environ.get("HERMES_TEST_BASH")
    or (str(_WINDOWS_GIT_BASH) if _WINDOWS_GIT_BASH.exists() else shutil.which("bash") or "bash")
)


class InstallScriptTests(unittest.TestCase):
    def _environment(self, base: Path) -> tuple[dict[str, str], Path]:
        fake_bin = base / "bin"
        fake_bin.mkdir(parents=True)
        log = base / "hermes.log"
        hermes = fake_bin / "hermes"
        hermes.write_text(
            "#!/usr/bin/env bash\n"
            "case \"${HERMES_HOME:-}\" in *profiles*) exit 9 ;; esac\n"
            "printf '%s\\n' \"$*\" >> \"$HERMES_TEST_LOG\"\n"
            "if [ -n \"${HERMES_FAKE_FAIL_MATCH:-}\" ] && [[ \"$*\" == *\"$HERMES_FAKE_FAIL_MATCH\"* ]]; then exit 7; fi\n"
            "if [ \"${HERMES_FAKE_KEYS_UNSET:-0}\" = 1 ] && [[ \" $* \" == *' config get '* ]]; then printf 'Config key not set: %s\\n' \"${*: -2:1}\" >&2; exit 1; fi\n"
            "case \" $* \" in\n"
            "  *' config get plugins.enabled --json '*) printf '[\"quota\", \"other\"]\\n' ;;\n"
            "  *' config get plugins.disabled --json '*) printf '[\"quota\", \"blocked\"]\\n' ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        hermes.chmod(0o755)

        real_mv = BASH.parent / ("mv.exe" if os.name == "nt" else "mv")
        if not real_mv.exists():
            resolved = shutil.which("mv")
            if not resolved:
                self.fail("mv is required for installer tests")
            real_mv = Path(resolved)
        fake_mv = fake_bin / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'mv %s\\n' \"$*\" >> \"$HERMES_TEST_LOG\"\n"
            "if [ -n \"${HERMES_TEST_FAIL_MV_CALL:-}\" ]; then\n"
            "  count=0; [ ! -f \"$HERMES_TEST_MV_COUNT\" ] || count=$(cat \"$HERMES_TEST_MV_COUNT\")\n"
            "  count=$((count + 1)); printf '%s' \"$count\" > \"$HERMES_TEST_MV_COUNT\"\n"
            "  if [ \"$count\" = \"$HERMES_TEST_FAIL_MV_CALL\" ]; then exit 8; fi\n"
            "fi\n"
            "exec \"$HERMES_REAL_MV\" \"$@\"\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
        env["HERMES_TEST_LOG"] = str(log)
        env["HERMES_REAL_MV"] = str(real_mv)
        env["HERMES_TEST_MV_COUNT"] = str(base / "mv-count")
        return env, log

    @staticmethod
    def _seed_old_install(home: Path) -> None:
        plugin = home / "plugins" / "quota"
        desktop = home / "desktop-plugins" / "quota"
        plugin.mkdir(parents=True)
        desktop.mkdir(parents=True)
        (plugin / "old-backend.txt").write_text("old backend", encoding="utf-8")
        (desktop / "old-widget.txt").write_text("old widget", encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows path normalization")
    def test_profile_scoped_native_windows_home_is_normalized_to_shared_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = os.environ.copy()
            shared = base / "local" / "hermes"
            env["HERMES_HOME"] = str(shared / "profiles" / "bot")
            result = subprocess.run(
                [str(BASH), "-lc", "source scripts/hermes-home.sh; resolve_hermes_root"],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            expected = subprocess.run(
                [str(BASH), "-lc", f"cygpath -m '{shared}'"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(result.stdout.strip(), expected)

    def test_installer_falls_back_to_working_python3(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, _ = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_REAL_PYTHON"] = sys.executable
            fake_bin = base / "bin"
            broken_python = fake_bin / "python"
            broken_python.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
            broken_python.chmod(0o755)
            working_python3 = fake_bin / "python3"
            working_python3.write_text(
                "#!/usr/bin/env bash\nexec \"$HERMES_REAL_PYTHON\" \"$@\"\n",
                encoding="utf-8",
            )
            working_python3.chmod(0o755)

            subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, check=True, capture_output=True)

            self.assertTrue((home / "plugins" / "quota" / "plugin.yaml").exists())

    def test_profile_scoped_home_is_rebased_before_running_hermes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, _ = self._environment(base)
            shared = base / "hermes-home"
            env["HERMES_HOME"] = str(shared / "profiles" / "bot")
            subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertTrue((shared / "plugins" / "quota" / "plugin.yaml").exists())

    def test_reinstall_removes_files_that_are_no_longer_shipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, _ = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, check=True, capture_output=True)
            stale = home / "plugins" / "quota" / "removed-in-new-version.py"
            stale.write_text("stale", encoding="utf-8")
            subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertFalse(stale.exists())
            self.assertTrue((home / "plugins" / "quota" / "plugin.yaml").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "plugin.js").exists())

    def test_install_aborts_without_writing_when_profile_config_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_FAIL_MATCH"] = "-p alpha config get plugins.enabled"
            (home / "profiles" / "alpha").mkdir(parents=True)
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((home / "plugins" / "quota" / "old-backend.txt").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "old-widget.txt").exists())
            self.assertNotIn("config set", log.read_text(encoding="utf-8"))

    def test_install_rolls_back_files_when_second_directory_swap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, _ = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_TEST_FAIL_MV_CALL"] = "3"
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((home / "plugins" / "quota" / "old-backend.txt").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "old-widget.txt").exists())

    def test_install_rolls_back_config_when_profile_update_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_FAIL_MATCH"] = "-p alpha config set plugins.enabled"
            (home / "profiles" / "alpha").mkdir(parents=True)
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((home / "plugins" / "quota" / "old-backend.txt").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "old-widget.txt").exists())
            calls = log.read_text(encoding="utf-8")
            self.assertIn("config set plugins.enabled [\"quota\", \"other\"]", calls)
            self.assertIn("config set plugins.disabled [\"quota\", \"blocked\"]", calls)

    def test_uninstall_aborts_without_writing_when_config_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_FAIL_MATCH"] = "config get plugins.enabled"
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "uninstall.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((home / "plugins" / "quota" / "old-backend.txt").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "old-widget.txt").exists())
            self.assertNotIn("config set", log.read_text(encoding="utf-8"))

    def test_uninstall_rolls_back_files_and_config_when_profile_update_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_FAIL_MATCH"] = "-p alpha config set plugins.enabled"
            (home / "profiles" / "alpha").mkdir(parents=True)
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "uninstall.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((home / "plugins" / "quota" / "old-backend.txt").exists())
            self.assertTrue((home / "desktop-plugins" / "quota" / "old-widget.txt").exists())
            calls = log.read_text(encoding="utf-8")
            self.assertIn("config set plugins.enabled [\"quota\", \"other\"]", calls)
            self.assertIn("config set plugins.disabled [\"quota\", \"blocked\"]", calls)

    def test_install_rollback_restores_absent_config_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_KEYS_UNSET"] = "1"
            env["HERMES_TEST_FAIL_MV_CALL"] = "3"
            self._seed_old_install(home)

            result = subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, capture_output=True)

            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("config unset plugins.enabled", calls)
            self.assertIn("config unset plugins.disabled", calls)
            self.assertNotIn("config set plugins.enabled []", calls)
            self.assertNotIn("config set plugins.disabled []", calls)

    def test_uninstall_with_absent_keys_does_not_create_empty_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            home = base / "hermes-home"
            env["HERMES_HOME"] = str(home)
            env["HERMES_FAKE_KEYS_UNSET"] = "1"
            self._seed_old_install(home)

            subprocess.run([str(BASH), "uninstall.sh"], cwd=ROOT, env=env, check=True, capture_output=True)

            calls = log.read_text(encoding="utf-8")
            self.assertNotIn("config set", calls)
            self.assertNotIn("config unset", calls)

    def test_uninstall_uses_localappdata_on_windows_and_cleans_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env, log = self._environment(base)
            env.pop("HERMES_HOME", None)
            env["LOCALAPPDATA"] = str(base / "local")
            env["HOME"] = str(base / "home")
            hermes_root = Path(env["LOCALAPPDATA"]) / "hermes"
            (hermes_root / "profiles" / "alpha").mkdir(parents=True)
            subprocess.run([str(BASH), "install.sh"], cwd=ROOT, env=env, check=True, capture_output=True)
            log.write_text("", encoding="utf-8")
            subprocess.run([str(BASH), "uninstall.sh"], cwd=ROOT, env=env, check=True, capture_output=True)
            self.assertFalse((hermes_root / "plugins" / "quota").exists())
            self.assertFalse((hermes_root / "desktop-plugins" / "quota").exists())
            calls = log.read_text(encoding="utf-8")
            self.assertIn("-p alpha config set plugins.enabled [\"other\"]", calls)


if __name__ == "__main__":
    unittest.main()
