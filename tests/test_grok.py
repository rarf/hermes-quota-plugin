from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_commands import load_package


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"billing-response"


class GrokDebugTests(unittest.TestCase):
    def test_private_debug_writer_replaces_destination_atomically(self):
        load_package()
        from quota_plugin.quota_providers import grok

        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "grok_last_response.bin"
            debug_path.write_bytes(b"old")

            grok._write_private_debug(str(debug_path), b"new")

            self.assertEqual(debug_path.read_bytes(), b"new")
            self.assertEqual(list(Path(tmp).glob(".grok_last_response.bin.*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_private_debug_writer_uses_owner_only_permissions(self):
        load_package()
        from quota_plugin.quota_providers import grok

        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "grok_last_response.bin"
            previous_umask = os.umask(0o022)
            try:
                grok._write_private_debug(str(debug_path), b"private")
            finally:
                os.umask(previous_umask)

            self.assertEqual(debug_path.stat().st_mode & 0o777, 0o600)

    def test_raw_response_is_not_persisted_by_default(self):
        load_package()
        from quota_plugin.quota_providers import grok

        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "grok_last_response.bin"
            with (
                patch.object(grok, "_RAW_DEBUG_PATH", str(debug_path)),
                patch.object(grok, "_load_cookies", return_value="session=test"),
                patch.object(grok.urllib.request, "urlopen", return_value=_Response()),
                patch.object(
                    grok,
                    "_parse_grok_protobuf",
                    return_value=grok.QuotaResult(label="grok", windows=[]),
                ),
                patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("HERMES_QUOTA_DEBUG", None)
                grok.fetch_grok_quota()

            self.assertFalse(debug_path.exists())

    def test_raw_response_is_persisted_when_debug_is_explicitly_enabled(self):
        load_package()
        from quota_plugin.quota_providers import grok

        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "grok_last_response.bin"
            with (
                patch.object(grok, "_RAW_DEBUG_PATH", str(debug_path)),
                patch.object(grok, "_load_cookies", return_value="session=test"),
                patch.object(grok.urllib.request, "urlopen", return_value=_Response()),
                patch.object(
                    grok,
                    "_parse_grok_protobuf",
                    return_value=grok.QuotaResult(label="grok", windows=[]),
                ),
                patch.dict(os.environ, {"HERMES_QUOTA_DEBUG": "1"}, clear=False),
            ):
                grok.fetch_grok_quota()

            self.assertEqual(debug_path.read_bytes(), b"billing-response")


if __name__ == "__main__":
    unittest.main()
