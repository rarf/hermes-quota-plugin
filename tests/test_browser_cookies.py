"""Offline unit tests for Chrome grok.com cookie import.

Run from the repo root:  python tests/test_browser_cookies.py
No live browser profile, Keychain, or network is touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover - environment-dependent
    _HAS_CRYPTO = False

_TEST_PASSWORD = "test-chrome-safe-storage"
_FUTURE_EXPIRES = 20_000_000_000_000_000  # Chrome epoch, far future
_EXPIRED_EXPIRES = 1


def _pbkdf2_key(password: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1003
    ).derive(password)


def _encrypt_v10_bytes(key: bytes, data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad] * pad)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + encryptor.update(data) + encryptor.finalize()


def _encrypt_v10(key: bytes, plaintext: str) -> bytes:
    return _encrypt_v10_bytes(key, plaintext.encode("utf-8"))


def _encrypt_v10_host_hash(key: bytes, host_key: str, plaintext: str) -> bytes:
    payload = hashlib.sha256(host_key.encode("utf-8")).digest() + plaintext.encode("utf-8")
    return _encrypt_v10_bytes(key, payload)


def _write_cookies_db(path: Path, rows: list[tuple[str, str, bytes, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE cookies "
            "(host_key TEXT, name TEXT, encrypted_value BLOB, expires_utc INTEGER)"
        )
        conn.executemany(
            "INSERT INTO cookies (host_key, name, encrypted_value, expires_utc) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class ChromeDiscoveryTests(unittest.TestCase):
    def test_prefers_network_cookies_then_legacy(self):
        from quota_providers import browser_cookies as bc

        with self._temp_chrome() as root:
            last = root / "Profile 2"
            (last / "Network").mkdir(parents=True)
            (last / "Network" / "Cookies").write_bytes(b"")
            (root / "Default").mkdir()
            (root / "Default" / "Cookies").write_bytes(b"")
            (root / "Local State").write_text(
                json.dumps({"profile": {"last_used": "Profile 2"}}),
                encoding="utf-8",
            )
            with mock.patch.object(bc, "_chrome_user_data_roots", return_value=[root]):
                dbs = bc.chrome_cookie_dbs()
        self.assertEqual(dbs[0], last / "Network" / "Cookies")
        self.assertIn(root / "Default" / "Cookies", dbs)

    def test_skips_missing_last_used_profile(self):
        from quota_providers import browser_cookies as bc

        with self._temp_chrome() as root:
            (root / "Default").mkdir()
            (root / "Default" / "Cookies").write_bytes(b"")
            (root / "Local State").write_text(
                json.dumps({"profile": {"last_used": "Person 1"}}),
                encoding="utf-8",
            )
            with mock.patch.object(bc, "_chrome_user_data_roots", return_value=[root]):
                dirs = bc.chrome_profile_dirs(root)
        self.assertEqual(dirs[0].name, "Default")

    def _temp_chrome(self):
        import tempfile

        class _Ctx:
            def __enter__(self_inner):
                self_inner._tmp = tempfile.TemporaryDirectory()
                root = Path(self_inner._tmp.name) / "Chrome"
                root.mkdir()
                return root

            def __exit__(self_inner, *exc):
                self_inner._tmp.cleanup()
                return False

        return _Ctx()


@unittest.skipUnless(_HAS_CRYPTO, "cryptography is required for Chrome decrypt tests")
class ChromeDecryptTests(unittest.TestCase):
    def test_decrypts_v10_cookie(self):
        from quota_providers.browser_cookies import decrypt_chrome_cookie_value

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        blob = _encrypt_v10(key, "s3cret-value")
        self.assertEqual(decrypt_chrome_cookie_value(key, blob), "s3cret-value")

    def test_rejects_v20_prefix(self):
        from quota_providers.browser_cookies import decrypt_chrome_cookie_value

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        with self.assertRaises(ValueError) as ctx:
            decrypt_chrome_cookie_value(key, b"v20" + b"\x00" * 32)
        msg = str(ctx.exception).lower()
        self.assertTrue("v20" in msg or "app-bound" in msg)

    def test_strips_chrome127_host_hash_prefix(self):
        from quota_providers.browser_cookies import decrypt_chrome_cookie_value

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        host = ".grok.com"
        blob = _encrypt_v10_host_hash(key, host, "s3cret-value")
        self.assertEqual(
            decrypt_chrome_cookie_value(key, blob, host_key=host), "s3cret-value"
        )

    def test_legacy_plaintext_still_works_with_host_key(self):
        from quota_providers.browser_cookies import decrypt_chrome_cookie_value

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        blob = _encrypt_v10(key, "s3cret-value")
        self.assertEqual(
            decrypt_chrome_cookie_value(key, blob, host_key=".grok.com"), "s3cret-value"
        )


@unittest.skipUnless(_HAS_CRYPTO, "cryptography is required for Chrome decrypt tests")
class ChromeLoaderTests(unittest.TestCase):
    def test_selects_only_unexpired_grok_hosts(self):
        from quota_providers import browser_cookies as bc

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        with self._db() as db:
            _write_cookies_db(
                db,
                [
                    (".grok.com", "sso", _encrypt_v10(key, "alpha"), _FUTURE_EXPIRES),
                    ("accounts.google.com", "SID", _encrypt_v10(key, "nope"), _FUTURE_EXPIRES),
                    ("grok.com", "stale", _encrypt_v10(key, "old"), _EXPIRED_EXPIRES),
                ],
            )
            with mock.patch.object(bc, "chrome_cookie_dbs", return_value=[db]), mock.patch.object(
                bc, "_chrome_safe_storage_password", return_value=_TEST_PASSWORD
            ):
                header = bc.load_chrome_grok_cookies()
        self.assertEqual(header, "sso=alpha")
        self.assertNotIn("nope", header)
        self.assertNotIn("SID", header)
        self.assertNotIn("stale", header)
        self.assertNotIn("old", header)

    def test_decrypts_chrome127_host_hash_prefixed_cookies(self):
        from quota_providers import browser_cookies as bc

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        with self._db() as db:
            _write_cookies_db(
                db,
                [
                    (
                        ".grok.com",
                        "sso",
                        _encrypt_v10_host_hash(key, ".grok.com", "alpha"),
                        _FUTURE_EXPIRES,
                    )
                ],
            )
            with mock.patch.object(bc, "chrome_cookie_dbs", return_value=[db]), mock.patch.object(
                bc, "_chrome_safe_storage_password", return_value=_TEST_PASSWORD
            ):
                header = bc.load_chrome_grok_cookies()
        self.assertEqual(header, "sso=alpha")

    def test_keeps_session_cookies_with_zero_expiry(self):
        from quota_providers import browser_cookies as bc

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        with self._db() as db:
            _write_cookies_db(
                db,
                [("grok.com", "sid", _encrypt_v10(key, "session"), 0)],
            )
            with mock.patch.object(bc, "chrome_cookie_dbs", return_value=[db]), mock.patch.object(
                bc, "_chrome_safe_storage_password", return_value=_TEST_PASSWORD
            ):
                header = bc.load_chrome_grok_cookies()
        self.assertEqual(header, "sid=session")

    def test_missing_keychain_is_typed_error(self):
        from quota_providers import browser_cookies as bc
        from quota_providers.browser_cookies import ChromeCookieError

        key = _pbkdf2_key(_TEST_PASSWORD.encode("utf-8"))
        with self._db() as db:
            _write_cookies_db(
                db,
                [("grok.com", "sso", _encrypt_v10(key, "alpha"), _FUTURE_EXPIRES)],
            )
            with mock.patch.object(bc, "chrome_cookie_dbs", return_value=[db]), mock.patch.object(
                bc, "_chrome_safe_storage_password", return_value=None
            ):
                with self.assertRaises(ChromeCookieError) as ctx:
                    bc.load_chrome_grok_cookies()
        self.assertEqual(ctx.exception.reason, "chrome-keychain-denied")

    def test_v20_blob_is_app_bound(self):
        from quota_providers import browser_cookies as bc
        from quota_providers.browser_cookies import ChromeCookieError

        with self._db() as db:
            _write_cookies_db(
                db,
                [("grok.com", "sso", b"v20" + b"\x00" * 32, _FUTURE_EXPIRES)],
            )
            with mock.patch.object(bc, "chrome_cookie_dbs", return_value=[db]), mock.patch.object(
                bc, "_chrome_safe_storage_password", return_value=_TEST_PASSWORD
            ):
                with self.assertRaises(ChromeCookieError) as ctx:
                    bc.load_chrome_grok_cookies()
        self.assertEqual(ctx.exception.reason, "chrome-app-bound")

    def test_permission_error_is_tcc_denied(self):
        from quota_providers import browser_cookies as bc
        from quota_providers.browser_cookies import ChromeCookieError

        with mock.patch.object(
            bc, "chrome_cookie_dbs", side_effect=PermissionError("Operation not permitted")
        ):
            with self.assertRaises(ChromeCookieError) as ctx:
                bc.load_chrome_grok_cookies()
        self.assertEqual(ctx.exception.reason, "chrome-tcc-denied")

    def _db(self):
        import tempfile

        class _Ctx:
            def __enter__(self_inner):
                self_inner._tmp = tempfile.TemporaryDirectory()
                return Path(self_inner._tmp.name) / "Cookies"

            def __exit__(self_inner, *exc):
                self_inner._tmp.cleanup()
                return False

        return _Ctx()


class GrokChromeWireTests(unittest.TestCase):
    def test_fetch_maps_tcc_to_unavailable(self):
        from quota_providers import grok
        from quota_providers.browser_cookies import ChromeCookieError

        with mock.patch.object(grok, "_SESSION_PATH", "/tmp/missing-grok-session.json"), mock.patch.object(
            grok, "load_firefox_grok_cookies", return_value=None
        ), mock.patch.object(
            grok,
            "load_chrome_grok_cookies",
            side_effect=ChromeCookieError("chrome-tcc-denied"),
        ):
            res = grok.fetch_grok_quota()
        self.assertEqual(res.unavailable_reason, "chrome-tcc-denied")
        self.assertEqual(res.windows, [])

    def test_firefox_still_wins_over_chrome(self):
        from quota_providers import grok

        with mock.patch.object(grok, "_SESSION_PATH", "/tmp/missing-grok-session.json"), mock.patch.object(
            grok, "load_firefox_grok_cookies", return_value="ff=1"
        ), mock.patch.object(
            grok, "load_chrome_grok_cookies", side_effect=AssertionError("chrome should not run")
        ):
            cookies = grok._load_cookies()
        self.assertEqual(cookies, "ff=1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
